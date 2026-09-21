# -*- coding: utf-8 -*-
"""
PostgreSQL 数据访问层（架构升级：JSON文件存储 → PostgreSQL，多人并发访问）。

职责：
  - 连接管理：psycopg2 线程池，DSN 只从环境变量读取（DATABASE_URL 或
    POSTGRES_HOST/PORT/USER/PASSWORD/DB 组合），绝不硬编码账密；
  - Schema 初始化：幂等 CREATE TABLE IF NOT EXISTS，含审计表防篡改触发器；
  - 统一取数口径：query() 返回 dict 列表，JSONB 自动落为 Python 对象。

表结构总览（正式口径，JSON 文件存储已废弃为迁移前备份）：
  batches              批次（批次编号唯一、来源 web/mobile_app、风险等级/总分、
                       verification JSONB 完整快照保证渲染保真）
  documents            单据（所属批次、单据类型、实例序号、识别字段JSON、核验状态）
  verification_issues  核验问题（所属单据、问题字段、FAIL/WARNING、规则版本、修改建议）
  users                用户（bcrypt 哈希、角色、状态，见 auth_service）
  audit_logs           操作日志（只增不改：应用层只 INSERT/SELECT，
                       数据库层触发器拒绝一切 UPDATE/DELETE/TRUNCATE）

本地/测试环境约定：项目根目录可放 .env（已被 .gitignore 排除），
格式 KEY=VALUE 一行一条，环境变量优先于 .env。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

_PROJECT_ROOT = Path(__file__).parent

# ---------------------------------------------------------------- .env 装载（环境变量优先）


def load_dotenv(path: Path | None = None) -> dict:
    """读取 .env 中未在环境变量里出现的键（不覆盖已有环境变量）。返回实际生效的键。"""
    env_file = path or (_PROJECT_ROOT / ".env")
    applied: dict = {}
    if not env_file.exists():
        return applied
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                applied[key] = value
    except OSError:
        pass
    return applied


load_dotenv()


def dsn() -> str:
    """数据库连接串：优先 DATABASE_URL，否则由 POSTGRES_* 变量拼装；缺失即报错。"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    host = os.environ.get("POSTGRES_HOST", "").strip()
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "")
    database = os.environ.get("POSTGRES_DB", "").strip()
    if host and user and database:
        port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
        return (f"postgresql://{user}:{password}@{host}:{port}/{database}")
    raise RuntimeError(
        "数据库连接未配置：请设置 DATABASE_URL（或 POSTGRES_HOST/POSTGRES_USER/"
        "POSTGRES_PASSWORD/POSTGRES_DB），参考 .env.example。账密不允许硬编码进代码。")


_POOL: psycopg2.pool.ThreadedConnectionPool | None = None
_POOL_LOCK = threading.Lock()
_MIN_CONN, _MAX_CONN = 1, 8


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = psycopg2.pool.ThreadedConnectionPool(
                    _MIN_CONN, _MAX_CONN, dsn())
    return _POOL


def reset_pool():
    """关闭连接池（测试隔离/重连用；下次访问自动重建）。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.closeall()
            except Exception:
                pass
        _POOL = None


@contextmanager
def connection():
    """从池中取连接：正常提交、异常回滚并归还。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """执行 SELECT（或带 RETURNING 的写语句），返回 dict 列表（JSONB 已解码）。"""
    with connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
    return [dict(r) for r in rows]


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """执行写语句，返回受影响行数（审计表只允许经由 audit.py 的 INSERT）。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def jsonb(value) -> psycopg2.extras.Json:
    """Python 对象 → JSONB 适配（None/缺省安全）。"""
    return psycopg2.extras.Json(value if value is not None else None)


def ping(timeout_hint: str = "") -> bool:
    """连通性探测（start.sh/诊断用）。"""
    try:
        return query("SELECT 1 AS ok")[0]["ok"] == 1
    except Exception:
        return False


# ---------------------------------------------------------------- Schema（幂等）

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(16)  NOT NULL CHECK (role IN ('business', 'finance', 'admin')),
    status        VARCHAR(16)  NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled')),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS batches (
    id                   SERIAL PRIMARY KEY,
    batch_id             VARCHAR(64)  NOT NULL UNIQUE,
    batch_name           VARCHAR(255) NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by           VARCHAR(64),
    source               VARCHAR(16)  NOT NULL DEFAULT 'mobile_app',
    risk_grade           VARCHAR(8)   NOT NULL DEFAULT 'low',
    risk_level           VARCHAR(8)   NOT NULL DEFAULT 'green',
    risk_score           INTEGER      NOT NULL DEFAULT 0,
    risk_label           VARCHAR(32)  NOT NULL DEFAULT '',
    one_line             TEXT         NOT NULL DEFAULT '',
    doc_count            INTEGER      NOT NULL DEFAULT 0,
    doc_types            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    identity_numbers     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    declared_composition JSONB,
    rule_version         VARCHAR(64)  NOT NULL DEFAULT '',
    data_version         VARCHAR(64)  NOT NULL DEFAULT '',
    verification         JSONB        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batches_created_by ON batches (created_by);

CREATE TABLE IF NOT EXISTS documents (
    id        SERIAL PRIMARY KEY,
    batch_id  VARCHAR(64)  NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    doc_index INTEGER      NOT NULL,
    doc_type  VARCHAR(64)  NOT NULL,
    doc_id    VARCHAR(128) NOT NULL,
    title     VARCHAR(255) NOT NULL DEFAULT '',
    fields    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status    VARCHAR(16)  NOT NULL DEFAULT 'PASS'
              CHECK (status IN ('PASS', 'WARNING', 'FAIL')),
    UNIQUE (batch_id, doc_id)
);
-- P0任务书A1：字段四状态证据（增量列，兼容v2.0已有库）
ALTER TABLE documents ADD COLUMN IF NOT EXISTS field_meta JSONB;
CREATE INDEX IF NOT EXISTS idx_documents_batch ON documents (batch_id);

CREATE TABLE IF NOT EXISTS verification_issues (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     VARCHAR(64)  NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    doc_id       VARCHAR(128),
    check_id     VARCHAR(32)  NOT NULL DEFAULT '',
    check_name   VARCHAR(128) NOT NULL DEFAULT '',
    category     VARCHAR(32)  NOT NULL DEFAULT '',
    level        VARCHAR(8)   NOT NULL CHECK (level IN ('FAIL', 'WARNING')),
    field        VARCHAR(128),
    detail       TEXT         NOT NULL DEFAULT '',
    suggestion   TEXT         NOT NULL DEFAULT '',
    rule_version VARCHAR(64)  NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_issues_batch ON verification_issues (batch_id);
CREATE INDEX IF NOT EXISTS idx_issues_doc ON verification_issues (batch_id, doc_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    username     VARCHAR(64)  NOT NULL,
    action       VARCHAR(32)  NOT NULL,
    object_type  VARCHAR(32),
    object_id    VARCHAR(128),
    detail       JSONB,
    before_value JSONB,
    after_value  JSONB,
    ip           VARCHAR(64),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_logs (username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_logs (action, created_at DESC);
"""

# 审计不可篡改：数据库层兜底——任何角色（含表 owner）的 UPDATE/DELETE/TRUNCATE 一律拒绝。
# 应用层配合约束见 audit.py（只提供 INSERT 与 SELECT，无任何修改/删除入口）。
AUDIT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION audit_logs_no_mutation() RETURNS trigger AS $fn$
BEGIN
    RAISE EXCEPTION 'audit_logs 为只增审计表，禁止 % 操作（审计不可篡改）', TG_OP;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs;
CREATE TRIGGER audit_logs_immutable
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_no_mutation();

DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs;
CREATE TRIGGER audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_no_mutation();
"""


def init_schema() -> None:
    """建表 + 审计防篡改触发器（幂等，服务启动/测试初始化时调用）。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(AUDIT_GUARD_SQL)
