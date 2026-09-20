# -*- coding: utf-8 -*-
"""pytest 共享夹具：测试数据库准备与用例间隔离。

架构升级后 pytest 依赖 PostgreSQL（存储层即核心路径，不做静默跳过）：
  - DATABASE_URL 环境变量（或项目根 .env）指定测试库；
  - 本地快速起步（与 .env.example 一致）：
      docker run -d --name ceb-dev-pg -e POSTGRES_USER=ceb \
        -e POSTGRES_PASSWORD=ceb_dev_pw -e POSTGRES_DB=ceb_verify \
        -p 127.0.0.1:15433:5432 postgres:16-alpine
  - 库不可达时明确报错退出，提示如何准备。

隔离口径：每个用例结束后清空业务表（batches/documents/verification_issues/users）；
audit_logs 为只增不改表（触发器禁止 DELETE/TRUNCATE），测试统一使用
唯一用户名与时间过滤进行隔离，不做清理。
"""

import uuid

import pytest

import db


@pytest.fixture(scope="session", autouse=True)
def _require_db():
    """整个会话要求数据库可达；不可达给出可操作的提示并退出。"""
    try:
        if not db.ping():
            raise RuntimeError("ping 失败")
    except Exception as exc:
        pytest.exit(
            "测试需要 PostgreSQL：请设置 DATABASE_URL 或准备本地测试库，例如\n"
            "  docker run -d --name ceb-dev-pg -e POSTGRES_USER=ceb \\\n"
            "    -e POSTGRES_PASSWORD=ceb_dev_pw -e POSTGRES_DB=ceb_verify \\\n"
            "    -p 127.0.0.1:15433:5432 postgres:16-alpine\n"
            f"当前连接错误：{exc}", returncode=1)
    db.init_schema()
    yield


@pytest.fixture(autouse=True)
def _clean_business_tables():
    yield
    db.execute("DELETE FROM verification_issues")
    db.execute("DELETE FROM documents")
    db.execute("DELETE FROM batches")
    db.execute("DELETE FROM users")


@pytest.fixture
def unique_name() -> str:
    """每个用例唯一的名字片段（用户名等），保证审计只增表下的测试隔离。"""
    return "t" + uuid.uuid4().hex[:10]
