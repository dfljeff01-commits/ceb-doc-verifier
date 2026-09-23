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

-- ---------------------------------------------------------------- 数据核对模块v1（班列统一编号 + 联运结算/补贴对账）

-- 缩写代码字典（任务书 §一：可维护配置，不硬编码；站点/口岸/目的地后续可增删）
CREATE TABLE IF NOT EXISTS train_code_dict (
    category   VARCHAR(16)  NOT NULL CHECK (category IN ('station', 'port', 'dest')),
    code       VARCHAR(16)  NOT NULL,
    name       VARCHAR(64)  NOT NULL,
    sort       INTEGER      NOT NULL DEFAULT 0,
    active     BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_by VARCHAR(64),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (category, code)
);

-- 运行配置（三方对账告警阈值等；KISS 单表 KV）
CREATE TABLE IF NOT EXISTS app_config (
    key         VARCHAR(64)  PRIMARY KEY,
    value       JSONB        NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    updated_by  VARCHAR(64),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 班列主档：trip_no 即正式编号（任务书 §二"以班列编号为主键"）；
-- UNIQUE 五元组+序号 是兜底去重的数据库侧兜底（应用层先查后插，冲突自动加 -01/-02）
CREATE TABLE IF NOT EXISTS train_trips (
    trip_no        TEXT         PRIMARY KEY,
    dep_date       DATE         NOT NULL,
    station_code   VARCHAR(16)  NOT NULL,
    port_code      VARCHAR(16)  NOT NULL,
    dest_code      VARCHAR(16)  NOT NULL,
    train_type     VARCHAR(1)   NOT NULL CHECK (train_type IN ('L', 'T')),
    suffix         SMALLINT     NOT NULL DEFAULT 0,
    route_label    VARCHAR(128) NOT NULL DEFAULT '',
    wagon_count    INTEGER,
    container_40hd INTEGER,
    container_20hd INTEGER,
    teu_total      NUMERIC(10,1),
    goods_name     VARCHAR(128) NOT NULL DEFAULT '',
    remark         TEXT         NOT NULL DEFAULT '',
    created_by     VARCHAR(64),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by     VARCHAR(64),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (dep_date, station_code, port_code, dest_code, train_type, suffix)
);
CREATE INDEX IF NOT EXISTS idx_trips_dep_date ON train_trips (dep_date);

-- 预估编号及映射留痕（任务书 §一：预估编号→正式编号映射须留痕，不覆盖删除）
CREATE TABLE IF NOT EXISTS train_est_numbers (
    est_no       TEXT        PRIMARY KEY,
    dep_date_est DATE        NOT NULL,
    station_code VARCHAR(16) NOT NULL,
    port_code    VARCHAR(16) NOT NULL,
    dest_code    VARCHAR(16) NOT NULL,
    train_type   VARCHAR(1)  NOT NULL CHECK (train_type IN ('L', 'T')),
    official_no  TEXT        UNIQUE REFERENCES train_trips (trip_no),
    locked_at    TIMESTAMPTZ,
    locked_by    VARCHAR(64),
    created_by   VARCHAR(64),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_est_official ON train_est_numbers (official_no);

-- 联运费用结算明细（1:1；四状态沿用单据核对思路，字段允许暂缺不阻断）
CREATE TABLE IF NOT EXISTS train_settlements (
    trip_no        TEXT PRIMARY KEY REFERENCES train_trips (trip_no) ON DELETE CASCADE,
    rail_freight   NUMERIC(14,2),
    customs_fee    NUMERIC(14,2),
    service_fee    NUMERIC(14,2),
    other_fee      NUMERIC(14,2),
    settle_total   NUMERIC(14,2),
    actual_freight NUMERIC(14,2),
    actual_paid_at DATE,
    diff_reason    TEXT        NOT NULL DEFAULT '',
    record_status  VARCHAR(20) NOT NULL DEFAULT 'pending'
                   CHECK (record_status IN ('confirmed', 'pending', 'not_found', 'business_missing')),
    field_status   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_by     VARCHAR(64),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 预付款记录（1:N；发运日期未定时可挂预估编号下，故 trip_no 不设外键——
-- 锁定为正式编号时由 train_store 单事务批量改挂，est_ref 保留原始编号留痕）
CREATE TABLE IF NOT EXISTS train_prepayments (
    id         BIGSERIAL    PRIMARY KEY,
    trip_no    TEXT         NOT NULL,
    est_ref    TEXT,
    paid_at    DATE,
    amount     NUMERIC(14,2),
    remark     TEXT         NOT NULL DEFAULT '',
    created_by VARCHAR(64),
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prepays_trip ON train_prepayments (trip_no);

-- 补贴测算（1:1；review_status=存疑标记，anomalies=系统检测异常快照）
CREATE TABLE IF NOT EXISTS train_subsidies (
    trip_no          TEXT PRIMARY KEY REFERENCES train_trips (trip_no) ON DELETE CASCADE,
    dt_supply_100    NUMERIC(14,2),
    dt_supply_70     NUMERIC(14,2),
    ly_advance_100   NUMERIC(14,2),
    ly_recover_70    NUMERIC(14,2),
    auth_confirm_100 NUMERIC(14,2),
    auth_advance_70  NUMERIC(14,2),
    auth_remain_30   NUMERIC(14,2),
    forecast_diff    NUMERIC(14,2),
    diff_reason      TEXT        NOT NULL DEFAULT '',
    review_status    VARCHAR(16) NOT NULL DEFAULT 'normal'
                     CHECK (review_status IN ('normal', 'suspect', 'reviewed')),
    suspect_note     TEXT        NOT NULL DEFAULT '',
    anomalies        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    record_status    VARCHAR(20) NOT NULL DEFAULT 'pending'
                     CHECK (record_status IN ('confirmed', 'pending', 'not_found', 'business_missing')),
    field_status     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_by       VARCHAR(64),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
