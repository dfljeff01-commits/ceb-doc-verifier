# -*- coding: utf-8 -*-
"""操作日志（审计留痕）测试（架构升级任务书 §3 验收）。

覆盖：关键操作记录（内容含操作人/时间/类型/对象/前后值）、筛选查询、
以及"只增不改"双重保障——应用层无修改删除入口 + 数据库触发器拒绝
UPDATE/DELETE/TRUNCATE。
运行：pytest test_audit.py -v
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import audit
import db


def _marker():
    return "t" + uuid.uuid4().hex[:10]


def test_record_and_query_with_filters():
    name = _marker()
    rid = audit.record(name, audit.EDIT_FIELD, "field", "B1/d1/gross_weight_kg",
                       before=12300, after=12500, ip="10.0.0.9")
    assert isinstance(rid, int)

    # 按用户筛
    rows = audit.query(username=name)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "EDIT_FIELD"
    assert r["object_id"] == "B1/d1/gross_weight_kg"
    assert r["before_value"] == 12300 and r["after_value"] == 12500
    assert r["ip"] == "10.0.0.9"
    assert r["created_at"] is not None

    # 按类型筛
    assert audit.query(username=name, action=audit.LOGIN) == []
    assert len(audit.query(username=name, action=audit.EDIT_FIELD)) == 1

    # 按时间范围筛（未来时间窗应查不到）
    future = datetime.now(timezone.utc) + timedelta(days=2)
    assert audit.query(username=name, since=future) == []
    assert audit.count(username=name, since=future) == 0
    past = datetime.now(timezone.utc) - timedelta(days=2)
    assert audit.count(username=name, since=past, until=future) == 1


def test_record_json_detail_roundtrip():
    name = _marker()
    audit.record(name, audit.UPLOAD_DOCS, "batch", "MB-X",
                 detail={"photos": 3, "risk_grade": "low"})
    row = audit.query(username=name)[0]
    assert row["detail"] == {"photos": 3, "risk_grade": "low"}


def test_audit_table_rejects_update_delete_truncate():
    """数据库层兜底：任何 UPDATE/DELETE/TRUNCATE（即使拿到DB连接）一律报错。"""
    name = _marker()
    audit.record(name, audit.LOGIN, "user", name)
    rid = audit.query(username=name)[0]["id"]

    with pytest.raises(Exception, match="禁止 UPDATE"):
        db.execute("UPDATE audit_logs SET username='hacker' WHERE id=%s", (rid,))
    with pytest.raises(Exception, match="禁止 DELETE"):
        db.execute("DELETE FROM audit_logs WHERE id=%s", (rid,))
    with pytest.raises(Exception, match="禁止 TRUNCATE"):
        db.execute("TRUNCATE audit_logs")
    # 记录原样保留
    assert audit.query(username=name)[0]["username"] == name


def test_verify_action_records_summary_from_api():
    """核验动作经 API 层自动留痕（username+batch+summary）。"""
    from fastapi.testclient import TestClient
    import auth_service
    from api import app

    c = TestClient(app)
    name = _marker()
    auth_service.create_user(name, "Passw0rd1", "business")
    tok = c.post("/auth/login",
                 json={"username": name, "password": "Passw0rd1"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    resp = c.post("/verify", json={
        "batch_id": "AUDIT-BATCH-1", "batch_name": "审计样例",
        "documents": [{"doc_type": "invoice", "doc_id": "d1", "title": "发票",
                       "fields": {"invoice_no": "INV-9"}}]}, headers=h)
    assert resp.status_code == 200
    rows = audit.query(username=name, action=audit.VERIFY)
    assert len(rows) == 1
    assert rows[0]["object_id"] == "AUDIT-BATCH-1"
    assert rows[0]["detail"]["summary"]["total"] >= 1
