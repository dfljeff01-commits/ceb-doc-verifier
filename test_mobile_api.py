# -*- coding: utf-8 -*-
"""App专用端点测试（/mobile/quick-check、/mobile/batch、/mobile/lookup）。

产品定位"电脑端是大脑、手机端是触手"的接口契约：
  - quick-check 只返回轻量摘要（风险等级红黄绿 + 一句话关键问题 + 批次编号），
    绝不泄露核验明细/字段/分数构成/AI建议；
  - 完整报告持久化在服务端，供电脑端按批次编号复查（include_full=true）；
  - 现场速查可按运单号/单证编号/批次编号命中历史结论。

OCR为外部依赖：测试用假 process_image 替身（不依赖 tesseract）；
存储目录用临时目录隔离（mobile_store 支持环境变量覆盖，在导入前设置）。
运行：pytest test_mobile_api.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 存储目录必须在 mobile_store / api 导入前指向临时目录（模块级常量）
_TMP_STORE = tempfile.mkdtemp(prefix="mobile_store_test_")
os.environ["MOBILE_RESULTS_DIR"] = _TMP_STORE

from fastapi.testclient import TestClient  # noqa: E402

import mobile_store  # noqa: E402
import pdf_ingest  # noqa: E402
from api import app  # noqa: E402

client = TestClient(app)


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


def _post_photos(n=1, name="photo.jpg"):
    return client.post(
        "/mobile/quick-check",
        files=[("files", (f"{i}_{name}", b"\xff\xd8fakejpeg", "image/jpeg"))
               for i in range(n)])


# ---------------------------------------------------------------- 轻量摘要契约

def test_quick_check_returns_lite_summary_only(fake_ocr):
    """上传后只返回一句话摘要：风险等级+关键问题+批次编号，不含任何明细。"""
    resp = _post_photos(1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 必备字段
    assert body["batch_id"].startswith("MB-")
    assert body["risk_level"] in ("green", "yellow", "red")
    assert body["risk_grade"] in ("low", "medium", "high")
    assert body["one_line"]
    assert "电脑端" in body["detail_hint"] and body["batch_id"] in body["detail_hint"]
    assert body["doc_count"] == 1
    # 轻量契约：不允许携带完整明细/字段/建议
    for forbidden in ("results", "documents", "fields", "suggestions",
                      "breakdown", "document_groups", "field_confidence"):
        assert forbidden not in body, f"轻量摘要不应包含 {forbidden}"


def test_quick_check_multi_photos_and_persists_full_report(fake_ocr):
    """多张照片组装一个批次；完整报告持久化在服务端，App响应里没有。"""
    fake_ocr["results"] = [
        FakeIngestResult(doc_type="invoice"),
        FakeIngestResult(doc_type="packing_list"),
        FakeIngestResult(doc_type="railway_waybill"),
    ]
    resp = _post_photos(3)
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_count"] == 3
    assert set(body["doc_types"]) == {"invoice", "packing_list", "railway_waybill"}
    # 服务端持久化了完整记录
    record = mobile_store.get_batch(body["batch_id"])
    assert record is not None
    assert len(record["documents"]) == 3
    assert record["verification"]["summary"]["total"] > 0


def test_quick_check_risk_level_mapping(fake_ocr):
    """引擎grade映射为App红黄绿：单张invoice缺大量契约字段时至少有WARNING，
    单发票批次必然缺单证（DOC-001 FAIL）→ red。"""
    resp = _post_photos(1)
    body = resp.json()
    assert body["risk_level"] == "red"      # 单张发票缺其余4类单证 → FAIL
    assert body["risk_grade"] == "high"


def test_quick_check_failure_gives_placeholder_doc(fake_ocr):
    """单张识别异常时以unknown占位进入核验，用户仍得到一句话反馈而非报错中断。"""
    fake_ocr["results"] = [
        FakeIngestResult(doc_type="invoice"),
        FakeIngestResult(error="OCR失败", fields={}),
    ]
    resp = _post_photos(2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_count"] == 2
    assert "unknown" in body["doc_types"]


def test_quick_check_ocr_unavailable_returns_503(fake_ocr):
    fake_ocr["ocr"] = False
    fake_ocr["results"] = [FakeIngestResult(error="未安装tesseract")]
    resp = _post_photos(1)
    assert resp.status_code == 503
    assert "tesseract" in resp.json()["detail"]


def test_quick_check_rejects_empty_and_oversize(fake_ocr):
    resp = client.post("/mobile/quick-check", files=[])
    assert resp.status_code == 422
    big = b"x" * (10 * 1024 * 1024 + 1)
    resp = client.post("/mobile/quick-check",
                       files=[("files", ("big.jpg", big, "image/jpeg"))])
    assert resp.status_code == 413


# ---------------------------------------------------------------- 按批次编号查询

def test_get_batch_lite_by_default_full_on_demand(fake_ocr):
    batch_id = _post_photos(1).json()["batch_id"]
    lite = client.get(f"/mobile/batch/{batch_id}")
    assert lite.status_code == 200
    lite_body = lite.json()
    assert lite_body["batch_id"] == batch_id
    assert "verification" not in lite_body and "documents" not in lite_body
    assert "waybill_no" in str(lite_body["identity_numbers"]) or lite_body["identity_numbers"]

    full = client.get(f"/mobile/batch/{batch_id}?include_full=true")
    assert full.status_code == 200
    full_body = full.json()
    assert full_body["verification"]["summary"]
    assert len(full_body["documents"]) >= 1


def test_get_batch_unknown_returns_404(fake_ocr):
    assert client.get("/mobile/batch/MB-20990101-000000-FFFF").status_code == 404
    # 路径注入防护：非法字符一律按未找到处理
    assert client.get("/mobile/batch/..%2F..%2Fsecret").status_code in (404, 422)


# ---------------------------------------------------------------- 现场速查

def test_lookup_by_identity_number_and_batch_id(fake_ocr):
    """现场速查核心场景：运单号/批次编号都能命中历史核验结论。"""
    fake_ocr["results"] = [FakeIngestResult(doc_type="railway_waybill", fields={
        "waybill_no": "SMU/T/2026-09", "consignor_name": "甲公司",
        "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
        "gross_weight_kg": 12300, "container_no": "MSKU8765432"})]
    batch_id = _post_photos(1).json()["batch_id"]

    # 按运单号查
    by_wb = client.get("/mobile/lookup", params={"q": "SMU/T/2026-09"}).json()
    assert by_wb["count"] >= 1
    assert any(m["batch_id"] == batch_id for m in by_wb["matches"])
    hit = next(m for m in by_wb["matches"] if m["batch_id"] == batch_id)
    assert hit["risk_level"] in ("green", "yellow", "red")
    assert hit["one_line"]
    assert "verification" not in hit   # 速查同样只回轻量视图

    # 不区分大小写/空格
    sloppy = client.get("/mobile/lookup", params={"q": " smu/t/2026-09 "}).json()
    assert sloppy["count"] >= 1

    # 按批次编号查
    by_id = client.get("/mobile/lookup", params={"q": batch_id}).json()
    assert any(m["batch_id"] == batch_id for m in by_id["matches"])

    # 查无记录
    none = client.get("/mobile/lookup", params={"q": "NO-SUCH-NUMBER-999"}).json()
    assert none["count"] == 0 and none["matches"] == []


def test_lookup_requires_query(fake_ocr):
    assert client.get("/mobile/lookup", params={"q": "  "}).status_code == 422


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


def test_store_roundtrip_and_atomic_files(fake_ocr):
    batch_id = _post_photos(1).json()["batch_id"]
    own = Path(_TMP_STORE) / f"{batch_id}.json"
    assert own.exists(), "批次记录应持久化为一个JSON文件"
    assert not list(Path(_TMP_STORE).glob("*.tmp")), "写入应原子完成，不留临时文件"
    record = mobile_store.get_batch(batch_id)
    assert record["lite"]["one_line"]
    assert mobile_store.get_batch("MB-NO-SUCH") is None  # 不存在的编号不抛异常
