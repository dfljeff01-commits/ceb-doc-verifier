# -*- coding: utf-8 -*-
"""SMGS国际货协运单端到端回归（P0任务书A4）。

样本为受控夹具（sample_pdfs/smgs_waybill_tkru.pdf，由
generate_smgs_sample.py 按СМГС栏位版式生成，不含真实客户数据）；字段基准
来自任务书人工确认值。覆盖：
  - 类型正确判定为 smgs_rail_waybill；
  - 必抽字段值正确（含俄文多词收货人不截断）；
  - not_found 不被误判为 business_missing/FAIL；
  - 无口径重量进 needs_review（反例样本）。
运行：pytest test_smgs_regression.py -v
"""

from pathlib import Path

import pytest

import doc_contract
import pdf_ingest
import verification_engine

SAMPLES = Path(__file__).parent / "sample_pdfs"
MAIN_SAMPLE = SAMPLES / "smgs_waybill_tkru.pdf"
WEIGHT_SAMPLE = SAMPLES / "smgs_weight_unlabeled.pdf"

BASELINE = {
    "consignor_name": "芜湖德菲图汽车技术有限公司",
    "consignee_name": 'ООО "АВТОЗАВОД АГР"',
    "container_no": "TKRU4625794",
    "goods_description": "汽车成套散件 COOLING FAN",
    "gross_weight_kg": 5629.84,
    "net_weight_kg": 3650.0,
    "total_packages": 25,
    "departure_station": "大同 DATONG",
    "destination_station": "谢利亚季诺 SELYATINO",
    "destination_country": "俄罗斯",
    "cargo_value": 332433.16,
    "currency": "CNY",
    "packing_type": "托盘",
    "seal_no": "261918",
}

# 任务书要求至少正确抽取并标准化的字段（A4 验收）
REQUIRED_HITS = [
    "consignor_name", "consignee_name", "departure_station",
    "destination_station", "container_no", "goods_description",
    "packing_type", "total_packages", "gross_weight_kg",
    "seal_no", "currency", "cargo_value",
]


@pytest.fixture(scope="module")
def ingest_result():
    if not MAIN_SAMPLE.exists():
        pytest.skip("受控夹具未生成：python sample_pdfs/generate_smgs_sample.py")
    data = MAIN_SAMPLE.read_bytes()
    return pdf_ingest.process_pdf(data, MAIN_SAMPLE.name)


def test_document_type_is_smgs(ingest_result):
    assert ingest_result.doc_type == "smgs_rail_waybill"


def test_required_fields_extracted_correctly(ingest_result):
    ev = ingest_result.field_evidence
    for field in REQUIRED_HITS:
        assert field in ev, f"{field} 未抽取"
        assert ev[field]["status"] in ("recognized", "needs_review"), \
            f"{field} 状态异常: {ev[field]['status']}"
        assert ev[field]["value"] == BASELINE[field], \
            f"{field}: got {ev[field]['value']!r}, expect {BASELINE[field]!r}"


def test_consignee_russian_name_not_truncated(ingest_result):
    """收货人不得截断为 ООО（任务书A3/A4）：完整多词俄文企业名称。"""
    name = ingest_result.field_evidence["consignee_name"]["value"]
    assert name == 'ООО "АВТОЗАВОД АГР"'
    assert name.count(" ") >= 1 and "АВТОЗАВОД АГР" in name
    assert name != "ООО"


def test_every_candidate_has_evidence(ingest_result):
    """每个候选必须带 页码/栏位/方法/置信度（任务书A1）。"""
    for field, e in ingest_result.field_evidence.items():
        if e["status"] == "not_found":
            continue
        assert e["page"] is not None and e["region"]
        assert e["method"] and e["confidence"] is not None


def test_weight_fields_have_source_evidence(ingest_result):
    """重量字段保留来源栏位/原文（任务书A4：多重量不猜测）。"""
    for f in ("gross_weight_kg", "net_weight_kg"):
        e = ingest_result.field_evidence[f]
        assert e["value"] == BASELINE[f]
        assert "18" in e["region"] and e["raw_text"]


def test_not_found_is_not_business_missing(ingest_result):
    """核心反误判（任务书A1/A4）：未找到候选（waybill_no 无栏位编号）
    不得判 business_missing，核验也不得因此 FAIL。"""
    ev = ingest_result.field_evidence
    assert "waybill_no" in ev
    assert ev["waybill_no"]["status"] == doc_contract.FIELD_NOT_FOUND

    # 走引擎核验：DOC-101 不得因 not_found 判 FAIL
    doc = ingest_result.to_document()
    batch = {"batch_id": "SMGS-REG", "documents": [doc]}
    verification = verification_engine.run_verification(batch)
    r101 = next(r for r in verification["results"] if r["check_id"] == "DOC-101")
    assert r101["status"] != "FAIL", r101["detail"]
    # DOC-002 措辞中性（不是"业务缺失"）
    r002 = next(r for r in verification["results"] if r["check_id"] == "DOC-002")
    assert "业务缺失" not in r002["detail"]


def test_business_missing_only_after_human_confirmation(ingest_result):
    """business_missing 只能由人工确认产生（任务书A1）。"""
    doc = ingest_result.to_document()
    meta = dict(doc.get("field_meta") or {})
    meta["waybill_no"] = {"status": "business_missing", "value": None,
                          "method": "人工确认"}
    doc["field_meta"] = meta
    verification = verification_engine.run_verification(
        {"batch_id": "SMGS-REG2", "documents": [doc]})
    r101 = next(r for r in verification["results"] if r["check_id"] == "DOC-101")
    assert r101["status"] == "FAIL"
    assert "运单号" in r101["detail"]


def test_unlabeled_weight_sample_goes_needs_review():
    """反例样本：栏位18重量未标毛/净口径（任务书A4）——
    首值按栏位口径暂记毛重（低置信+说明），净重候选转 needs_review 不静默判定。"""
    if not WEIGHT_SAMPLE.exists():
        pytest.skip("反例夹具未生成")
    r = pdf_ingest.process_pdf(WEIGHT_SAMPLE.read_bytes(), WEIGHT_SAMPLE.name)
    e = r.field_evidence["gross_weight_kg"]
    assert e["status"] == "recognized" and e["confidence"] == 0.8
    assert "毛/净" in e["note"]
    net = r.field_evidence["net_weight_kg"]
    assert net["status"] == "needs_review" and net["value"] is None
    assert "3650.0" in net["raw_text"]
