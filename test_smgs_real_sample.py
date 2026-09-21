# -*- coding: utf-8 -*-
"""真实СМГС运单回归（TKRU4625794-260921-085933，补充修复指令要求）。

样本：真实文件的受控本地副本（sample_pdfs/_real/，已被 .gitignore 排除，
绝不提交——含客户敏感信息）。缺失时跳过（不阻断公共测试）。
路径解析顺序：环境变量 CEBS_REAL_SMGS_PDF → sample_pdfs/_real/ 下的唯一PDF。

基准说明（诚实口径，与任务书基准的差异见各断言注释）：
  - 真实文件栏位4收货人为 ООО "СТС-ЛОГИСТИКА"（STS-LOGISTIKA）；
    任务书基准中的 ООО "АВТОЗАВОД АГР" 仅出现在栏位25（与承运人无关的
    备注栏），不是收货人栏的值——以真实文件为准；
  - 真实文件全文无『净重/нетто』标签，3650.0 出现在栏位20/21"标记重量"
    区域，无法证明净重口径 → 按任务书A4要求 honest 判 needs_review，
    不因任务书给出基准值而强行识别。
运行：pytest test_smgs_real_sample.py -v
"""

import os
from pathlib import Path

import pytest

import doc_contract
import pdf_ingest

_PROJECT = Path(__file__).parent
_LOCAL_DIR = _PROJECT / "sample_pdfs" / "_real"


def _find_real_pdf() -> Path | None:
    env = os.environ.get("CEBS_REAL_SMGS_PDF", "").strip()
    if env and Path(env).exists():
        return Path(env)
    if _LOCAL_DIR.exists():
        pdfs = sorted(_LOCAL_DIR.glob("*.pdf"))
        if pdfs:
            return pdfs[0]
    return None


@pytest.fixture(scope="module")
def real_result():
    path = _find_real_pdf()
    if path is None:
        pytest.skip("真实样本不在本机（受控文件不入库）；"
                    "放置于 sample_pdfs/_real/ 或设置 CEBS_REAL_SMGS_PDF 后运行")
    return pdf_ingest.process_pdf(path.read_bytes(), path.name)


def test_document_type_is_smgs(real_result):
    assert real_result.doc_type == "smgs_rail_waybill"


def test_consignor_extracted(real_result):
    ev = real_result.field_evidence["consignor_name"]
    assert ev["status"] == "recognized"
    assert ev["value"] == "芜湖德菲图汽车技术有限公司"
    # 地址/税号分离，不并入名称（任务书A3）
    assert "91340200MAE14NKT2R" not in ev["value"]
    assert "91340200MAE14NKT2R" in ev["note"]


def test_consignee_is_full_russian_name_not_truncated(real_result):
    """收货人不得截断为ООО；真实文件值为 СТС-ЛОГИСТИКА（非АВТОЗАВОД АГР）。"""
    ev = real_result.field_evidence["consignee_name"]
    assert ev["status"] == "recognized"
    assert ev["value"] == 'ООО "СТС-ЛОГИСТИКА"'
    assert "АВТОЗАВОД" not in ev["value"]   # 该名称只出现在栏位25备注，不是收货人
    assert ev["note"]                        # 规范化说明留痕（拉丁OOO→西里尔）


def test_container_goods_packages_seal_stations(real_result):
    ev = real_result.field_evidence
    assert ev["container_no"]["value"] == "TKRU4625794"
    assert ev["container_no"]["status"] == "recognized"
    assert "45G1" in ev["container_no"]["note"]          # 箱型附加值保留
    assert ev["goods_description"]["value"] == "汽车成套散件 COOLING FAN"
    assert ev["total_packages"]["value"] == 25
    assert ev["seal_no"]["value"] == "261918"
    assert ev["departure_station"]["value"] == "大同 DATONG"
    assert ev["destination_station"]["value"] == "谢利亚季诺 SELYATINO"
    assert ev["destination_country"]["value"] == "俄罗斯"
    assert ev["cargo_value"]["value"] == 332433.16
    assert ev["currency"]["value"] == "CNY"


def test_gross_weight_recognized_with_honest_note(real_result):
    """毛重5629.84：栏位18口径识别，但页内无毛/净标注——
    置信度必须降为0.8并保留说明，不冒充高置信。"""
    ev = real_result.field_evidence["gross_weight_kg"]
    assert ev["status"] == "recognized"
    assert ev["value"] == 5629.84
    assert ev["confidence"] == 0.8
    assert "毛/净" in ev["note"]


def test_net_weight_is_needs_review_not_recognized(real_result):
    """核心诚实口径（补充修复指令第22行）：真实文件无『净重/нетто』标签，
    3650.0 无法证明净重口径 → 必须 needs_review 且不填值，
    不能因任务书基准值强行标 recognized。"""
    ev = real_result.field_evidence["net_weight_kg"]
    assert ev["status"] == "needs_review"
    assert ev["value"] is None
    assert "3650.0" in ev["raw_text"]


def test_waybill_no_not_found_is_not_business_missing(real_result):
    """not_found ≠ business_missing：核验不得因未提取到运单号判FAIL。"""
    ev = real_result.field_evidence["waybill_no"]
    assert ev["status"] == doc_contract.FIELD_NOT_FOUND
    doc = real_result.to_document()
    verification = __import__("verification_engine").run_verification(
        {"batch_id": "REAL-SMGS", "documents": [doc]})
    r101 = next(r for r in verification["results"] if r["check_id"] == "DOC-101")
    assert r101["status"] != "FAIL"


def test_every_recognized_field_has_evidence(real_result):
    """每个 recognized 字段必须带 页码/栏位/方法/置信度（任务书A1）。"""
    for field, e in real_result.field_evidence.items():
        if e["status"] != "recognized":
            continue
        assert e["page"] is not None, field
        assert e["region"] and e["method"], field
        assert e["confidence"] is not None, field
