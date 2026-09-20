# -*- coding: utf-8 -*-
"""App专用端点测试（/mobile/quick-check、/mobile/batch、/mobile/lookup）。

产品定位"电脑端是大脑、手机端是触手"的接口契约：
  - quick-check 只返回轻量摘要（风险等级红黄绿 + 一句话关键问题 + 批次编号），
    绝不泄露核验明细/字段/分数构成/AI建议；
  - 完整报告持久化在服务端 PostgreSQL，供电脑端按批次编号复查（include_full=true）；
  - 现场速查可按运单号/单证编号/批次编号命中历史结论。

架构升级后：
  - 存储为 PostgreSQL（表结构见 db.py），roundtrip 测试改为库内重建比对；
  - 全部业务端点要求登录（Bearer JWT），用例统一走 /auth/login 获取令牌；
  - OCR为外部依赖：测试用假 process_image 替身（不依赖 tesseract）。
运行：pytest test_mobile_api.py -v
"""

import pytest

from fastapi.testclient import TestClient

import audit
import auth_service
import db
import mobile_store
import pdf_ingest
from api import app

client = TestClient(app)


# ---------------------------------------------------------------- 登录与令牌

@pytest.fixture
def mobile_user(unique_name):
    """注册一个业务账号并登录，返回带令牌的请求头。"""
    password = "Passw0rd1"
    auth_service.create_user(unique_name, password, "business")
    resp = client.post("/auth/login",
                       json={"username": unique_name, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "name": unique_name}


def _require_auth_headers(headers):
    assert "Authorization" in headers
    return {"Authorization": headers["Authorization"]}


# ---------------------------------------------------------------- 假OCR替身

class FakeIngestResult:
    """模拟 pdf_ingest.process_image 的返回对象（字段口径同真实现）。"""

    def __init__(self, doc_type="invoice", fields=None, error=None,
                 confidence=None, filename="photo.jpg"):
        self.filename = filename
        self.doc_type = doc_type
        self.type_score = 0.9
        self.fields = fields or {"invoice_no": "INV-100", "total_packages": 480,
                                 "gross_weight_kg": 12300, "currency": "USD",
                                 "total_amount": 86400}
        self.field_confidence = confidence or {k: "high" for k in self.fields}
        self.needs_review = False
        self.warnings = []
        self.elapsed_seconds = 0.01
        self.error = error

    def to_document(self):
        return {"doc_type": self.doc_type, "doc_id": "", "title": "测试单证",
                "fields": dict(self.fields)}


@pytest.fixture
def fake_ocr(monkeypatch):
    """把OCR替换为可控假实现，记录调用并按需注入结果。"""
    state = {"results": [FakeIngestResult()], "ocr": True, "calls": []}

    def fake_process(data, filename):
        state["calls"].append(filename)
        idx = min(len(state["calls"]) - 1, len(state["results"]) - 1)
        r = state["results"][idx]
        return FakeIngestResult(doc_type=r.doc_type, fields=r.fields,
                                error=r.error, confidence=r.field_confidence,
                                filename=filename)

    monkeypatch.setattr(pdf_ingest, "process_image", fake_process)
    monkeypatch.setattr(pdf_ingest, "ocr_available", lambda: state["ocr"])
    return state


def _post_photos(n=1, name="photo.jpg", headers=None):
    return client.post(
        "/mobile/quick-check",
        files=[("files", (f"{i}_{name}", b"\xff\xd8fakejpeg", "image/jpeg"))
               for i in range(n)],
        headers=headers or {})


# ---------------------------------------------------------------- 鉴权门禁

def test_quick_check_requires_login(fake_ocr):
    """未携带令牌的业务端点一律 401（架构升级：内部系统登录鉴权）。"""
    resp = _post_photos(1)
    assert resp.status_code == 401
    assert client.get("/mobile/recent").status_code == 401
    assert client.get("/mobile/lookup", params={"q": "SMU"}).status_code == 401


def test_login_failure_is_401_and_audited(unique_name):
    resp = client.post("/auth/login",
                       json={"username": unique_name, "password": "whatever1"})
    assert resp.status_code == 401
    rows = audit.query(username=unique_name, action=audit.LOGIN_FAILED)
    assert len(rows) == 1


# ---------------------------------------------------------------- 轻量摘要契约

def test_quick_check_returns_lite_summary_only(fake_ocr, mobile_user):
    """上传后只返回一句话摘要：风险等级+关键问题+批次编号，不含任何明细。"""
    resp = _post_photos(1, headers=_require_auth_headers(mobile_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 必备字段
    assert body["batch_id"].startswith("MB-")
    assert body["risk_level"] in ("green", "yellow", "red")
    assert body["risk_grade"] in ("low", "medium", "high")
    assert body["one_line"]
    assert "电脑端" in body["detail_hint"] and body["batch_id"] in body["detail_hint"]
    assert body["doc_count"] == 1
    assert body["created_by"] == mobile_user["name"]   # 记录上传人
    # 轻量契约：不允许携带完整明细/字段/建议
    for forbidden in ("results", "documents", "fields", "suggestions",
                      "breakdown", "document_groups", "field_confidence"):
        assert forbidden not in body, f"轻量摘要不应包含 {forbidden}"


def test_quick_check_multi_photos_and_persists_full_report(fake_ocr, mobile_user):
    """多张照片组装一个批次；完整报告持久化在 PostgreSQL，App响应里没有。"""
    fake_ocr["results"] = [
        FakeIngestResult(doc_type="invoice"),
        FakeIngestResult(doc_type="packing_list"),
        FakeIngestResult(doc_type="railway_waybill"),
    ]
    resp = _post_photos(3, headers=_require_auth_headers(mobile_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_count"] == 3
    assert set(body["doc_types"]) == {"invoice", "packing_list", "railway_waybill"}
    # 服务端持久化了完整记录（PostgreSQL）
    record = mobile_store.get_batch(body["batch_id"])
    assert record is not None
    assert len(record["documents"]) == 3
    assert record["verification"]["summary"]["total"] > 0
    assert record["created_by"] == mobile_user["name"]
    # 正式表结构行数一致（batches/documents/verification_issues）
    counts = mobile_store.counts()
    assert counts["batches"] == 1
    assert counts["documents"] == 3
    assert counts["issues"] >= 1   # 单发票批次必然有缺单证FAIL/告警


def test_quick_check_risk_level_mapping(fake_ocr, mobile_user):
    """引擎grade映射为App红黄绿：单发票批次必然缺单证（DOC-001 FAIL）→ red。"""
    resp = _post_photos(1, headers=_require_auth_headers(mobile_user))
    body = resp.json()
    assert body["risk_level"] == "red"
    assert body["risk_grade"] == "high"


def test_quick_check_failure_gives_placeholder_doc(fake_ocr, mobile_user):
    """单张识别异常时以unknown占位进入核验，用户仍得到一句话反馈而非报错中断。"""
    fake_ocr["results"] = [
        FakeIngestResult(doc_type="invoice"),
        FakeIngestResult(error="OCR失败", fields={}),
    ]
    resp = _post_photos(2, headers=_require_auth_headers(mobile_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_count"] == 2
    assert "unknown" in body["doc_types"]


def test_quick_check_ocr_unavailable_returns_503(fake_ocr, mobile_user):
    fake_ocr["ocr"] = False
    fake_ocr["results"] = [FakeIngestResult(error="未安装tesseract")]
    resp = _post_photos(1, headers=_require_auth_headers(mobile_user))
    assert resp.status_code == 503
    assert "tesseract" in resp.json()["detail"]


def test_quick_check_rejects_empty_and_oversize(fake_ocr, mobile_user):
    h = _require_auth_headers(mobile_user)
    resp = client.post("/mobile/quick-check", files=[], headers=h)
    assert resp.status_code == 422
    big = b"x" * (10 * 1024 * 1024 + 1)
    resp = client.post("/mobile/quick-check",
                       files=[("files", ("big.jpg", big, "image/jpeg"))], headers=h)
    assert resp.status_code == 413


# ---------------------------------------------------------------- 按批次编号查询

def test_get_batch_lite_by_default_full_on_demand(fake_ocr, mobile_user):
    h = _require_auth_headers(mobile_user)
    batch_id = _post_photos(1, headers=h).json()["batch_id"]
    lite = client.get(f"/mobile/batch/{batch_id}", headers=h)
    assert lite.status_code == 200
    lite_body = lite.json()
    assert lite_body["batch_id"] == batch_id
    assert "verification" not in lite_body and "documents" not in lite_body
    assert "waybill_no" in str(lite_body["identity_numbers"]) or lite_body["identity_numbers"]

    full = client.get(f"/mobile/batch/{batch_id}?include_full=true", headers=h)
    assert full.status_code == 200
    full_body = full.json()
    assert full_body["verification"]["summary"]
    assert len(full_body["documents"]) >= 1
    # 查看完整报告留痕（操作日志）
    rows = audit.query(username=mobile_user["name"], action=audit.VIEW_REPORT)
    assert any(r["object_id"] == batch_id for r in rows)


def test_get_batch_unknown_returns_404(fake_ocr, mobile_user):
    h = _require_auth_headers(mobile_user)
    assert client.get("/mobile/batch/MB-20990101-000000-FFFF",
                      headers=h).status_code == 404
    # 路径注入防护：非法字符一律按未找到处理
    resp = client.get("/mobile/batch/..%2F..%2Fsecret", headers=h)
    assert resp.status_code in (404, 422)


# ---------------------------------------------------------------- 现场速查

def test_lookup_by_identity_number_and_batch_id(fake_ocr, mobile_user):
    """现场速查核心场景：运单号/批次编号都能命中历史核验结论（PostgreSQL检索）。"""
    h = _require_auth_headers(mobile_user)
    fake_ocr["results"] = [FakeIngestResult(doc_type="railway_waybill", fields={
        "waybill_no": "SMU/T/2026-09", "consignor_name": "甲公司",
        "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
        "gross_weight_kg": 12300, "container_no": "MSKU8765432"})]
    batch_id = _post_photos(1, headers=h).json()["batch_id"]

    # 按运单号查
    by_wb = client.get("/mobile/lookup", params={"q": "SMU/T/2026-09"},
                       headers=h).json()
    assert by_wb["count"] >= 1
    assert any(m["batch_id"] == batch_id for m in by_wb["matches"])
    hit = next(m for m in by_wb["matches"] if m["batch_id"] == batch_id)
    assert hit["risk_level"] in ("green", "yellow", "red")
    assert hit["one_line"]
    assert "verification" not in hit   # 速查同样只回轻量视图

    # 不区分大小写/空格
    sloppy = client.get("/mobile/lookup", params={"q": " smu/t/2026-09 "},
                        headers=h).json()
    assert sloppy["count"] >= 1

    # 按批次编号查
    by_id = client.get("/mobile/lookup", params={"q": batch_id},
                       headers=h).json()
    assert any(m["batch_id"] == batch_id for m in by_id["matches"])

    # 查无记录
    none = client.get("/mobile/lookup", params={"q": "NO-SUCH-NUMBER-999"},
                      headers=h).json()
    assert none["count"] == 0 and none["matches"] == []


def test_lookup_requires_query(fake_ocr, mobile_user):
    h = _require_auth_headers(mobile_user)
    assert client.get("/mobile/lookup", params={"q": "  "},
                      headers=h).status_code == 422


# ---------------------------------------------------------------- 摘要器单元

def test_summarize_picks_most_severe_issue():
    verification = {
        "risk": {"grade": "high", "grade_label": "高风险", "score": 88},
        "results": [
            {"check_name": "必需单证齐全性检查", "status": "FAIL",
             "detail": "缺少必需单证：装箱单、铁路运单"},
            {"check_name": "件数一致性", "status": "WARNING", "detail": "件数存疑"},
        ],
    }
    lite = mobile_store.summarize([], verification)
    assert lite["risk_level"] == "red"
    assert "缺少必需单证" in lite["one_line"]


def test_summarize_all_pass():
    verification = {"risk": {"grade": "low", "grade_label": "低风险", "score": 0},
                    "results": [{"check_name": "x", "status": "PASS", "detail": ""}]}
    lite = mobile_store.summarize([], verification)
    assert lite["risk_level"] == "green"
    assert "全部检查项通过" in lite["one_line"]


def test_store_roundtrip_in_postgres(fake_ocr, mobile_user):
    """架构升级后的持久化契约：PostgreSQL 落库 → 回读重建记录完整可用，
    幂等重复保存不产生重复行。"""
    h = _require_auth_headers(mobile_user)
    batch_id = _post_photos(1, headers=h).json()["batch_id"]
    record = mobile_store.get_batch(batch_id)
    assert record["lite"]["one_line"]
    assert mobile_store.get_batch("MB-NO-SUCH") is None  # 不存在的编号不抛异常
    # 正式表逐行存在
    assert db.query("SELECT count(*) AS c FROM documents WHERE batch_id=%s",
                    (batch_id,))[0]["c"] == 1
    assert db.query("SELECT count(*) AS c FROM verification_issues"
                    " WHERE batch_id=%s", (batch_id,))[0]["c"] >= 1
    # 幂等重存（同批次）不产生重复行
    mobile_store.save_batch(record["documents"], record["verification"],
                            source=record["source"],
                            created_by=record["created_by"])
    assert db.query("SELECT count(*) AS c FROM batches")[0]["c"] == 1
    assert db.query("SELECT count(*) AS c FROM documents")[0]["c"] == 1
