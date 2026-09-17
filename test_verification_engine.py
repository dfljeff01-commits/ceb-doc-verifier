# -*- coding: utf-8 -*-
"""verification_engine 单元测试（升级任务书·工程化加固）。

覆盖：字段一致、字段不一致、数值超差、必需单证缺失、语义相似度分级边界。
运行：pytest test_verification_engine.py -v
"""

import copy

import pytest

from verification_engine import (STATUS_FAIL, STATUS_PASS, STATUS_WARNING,
                                 run_verification)

# ---------------------------------------------------------------- 构造工具


def base_documents() -> list:
    """一份字段齐全、彼此一致的最小单证组。"""
    return [
        {"doc_type": "invoice", "doc_id": "INV-T1", "title": "商业发票",
         "fields": {"consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "total_amount": 86400.00, "currency": "USD"}},
        {"doc_type": "packing_list", "doc_id": "PL-T1", "title": "装箱单",
         "fields": {"consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "container_no": "MSKU8765432"}},
        {"doc_type": "railway_waybill", "doc_id": "SMU-T1", "title": "铁路运单",
         "fields": {"waybill_no": "SMU/T/2026", "waybill_type": "SMGS国际货协运单",
                    "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "route_countries": ["中国", "哈萨克斯坦", "俄罗斯", "白俄罗斯", "波兰", "德国"],
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "container_no": "MSKU8765432"}},
        {"doc_type": "export_customs_declaration", "doc_id": "DEC-T1", "title": "出口报关单",
         "fields": {"consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "declared_value": 86400.00,
                    "currency": "USD", "waybill_no": "SMU/T/2026",
                    "container_no": "MSKU8765432", "departure_country": "中国",
                    "destination_country": "德国"}},
        {"doc_type": "certificate_of_origin", "doc_id": "COO-T1", "title": "原产地证书",
         "fields": {"co_no": "CCPIT-T1", "consignor_name": "甲公司",
                    "consignee_name": "BUYER GMBH", "goods_description": "陶瓷卫浴洁具",
                    "total_packages": 480}},
    ]


def run_on(modifier=None) -> dict:
    docs = base_documents()
    if modifier:
        modifier(docs)
    return run_verification({"batch_id": "t", "documents": docs})


def result_of(verification: dict, check_id: str) -> dict:
    return next(r for r in verification["results"] if r["check_id"] == check_id)


def edit(docs: list, doc_type: str, field: str, value):
    doc = next(d for d in docs if d["doc_type"] == doc_type)
    doc["fields"][field] = value


# ---------------------------------------------------------------- 基准：一致数据不误报


def test_clean_batch_all_pass():
    v = run_on()
    assert v["summary"]["fail"] == 0
    assert v["summary"]["warning"] == 0
    assert all(r["status"] == STATUS_PASS for r in v["results"])


# ---------------------------------------------------------------- 字段一致性


def test_goods_description_mismatch_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration",
                                 "goods_description", "卫浴陶瓷制品"))
    r = result_of(v, "CONS-001")
    assert r["status"] == STATUS_FAIL
    assert "陶瓷卫浴洁具" in r["detail"] and "卫浴陶瓷制品" in r["detail"]


def test_description_semantic_suspect_is_warning():
    """换序重述对相似度约0.78，落入0.6-0.85存疑区 → WARNING 而非 FAIL。"""
    v = run_on(lambda docs: edit(docs, "export_customs_declaration",
                                 "goods_description", "卫浴洁具陶瓷制品"))
    r = result_of(v, "CONS-001")
    assert r["status"] == STATUS_WARNING
    score = r["similarities"][0]["score"]
    assert 0.6 <= score < 0.85


def test_package_count_mismatch_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration", "total_packages", 475))
    r = result_of(v, "CONS-002")
    assert r["status"] == STATUS_FAIL
    assert "475" in r["detail"]


def test_gross_weight_within_tolerance_passes():
    """1%容差内（12300±123）应通过：装箱单 12390（偏离0.73%）→ PASS。"""
    v = run_on(lambda docs: edit(docs, "packing_list", "gross_weight_kg", 12390))
    assert result_of(v, "CONS-003")["status"] == STATUS_PASS


def test_gross_weight_beyond_tolerance_is_fail():
    """超1%容差：装箱单 12500（偏离1.63%）→ FAIL。"""
    v = run_on(lambda docs: edit(docs, "packing_list", "gross_weight_kg", 12500))
    assert result_of(v, "CONS-003")["status"] == STATUS_FAIL


def test_gross_weight_at_exact_tolerance_boundary():
    """边界：偏离恰好1.0%（12423）不超容差 → PASS；1.01%（12424.5≈12425）→ FAIL。"""
    v = run_on(lambda docs: edit(docs, "packing_list", "gross_weight_kg", 12423))
    assert result_of(v, "CONS-003")["status"] == STATUS_PASS
    v2 = run_on(lambda docs: edit(docs, "packing_list", "gross_weight_kg", 12425))
    assert result_of(v2, "CONS-003")["status"] == STATUS_FAIL


def test_consignee_mismatch_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration",
                                 "consignee_name", "BUYER HANDELS GMBH"))
    assert result_of(v, "CONS-005")["status"] == STATUS_FAIL


def test_waybill_no_crossref_mismatch_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration", "waybill_no", "SMU/WRONG/2026"))
    assert result_of(v, "CONS-006")["status"] == STATUS_FAIL


def test_declared_amount_beyond_tolerance_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration", "declared_value", 85000.00))
    assert result_of(v, "CONS-008")["status"] == STATUS_FAIL


def test_declared_amount_within_tolerance_passes():
    """金额容差0.5%：低报0.4%（86054.4）→ PASS。"""
    v = run_on(lambda docs: edit(docs, "export_customs_declaration",
                                 "declared_value", 86054.40))
    assert result_of(v, "CONS-008")["status"] == STATUS_PASS


# ---------------------------------------------------------------- 单证齐全性


def test_missing_coo_is_fail():
    v = run_on(lambda docs: docs.pop())
    r = result_of(v, "DOC-001")
    assert r["status"] == STATUS_FAIL
    assert "原产地证书" in r["detail"]
    assert r["missing_docs_count"] == 1


def test_missing_multiple_docs_counts_them():
    def remove_two(docs):
        docs.pop()    # 原产地证书
        docs.pop(1)   # 装箱单
    v = run_on(remove_two)
    r = result_of(v, "DOC-001")
    assert r["status"] == STATUS_FAIL
    assert r["missing_docs_count"] == 2


# ---------------------------------------------------------------- 路线合规


def test_smgs_with_turkey_is_warning_not_fail():
    v = run_on(lambda docs: edit(docs, "railway_waybill", "route_countries",
                                 ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]))
    r = result_of(v, "ROUTE-001")
    assert r["status"] == STATUS_WARNING
    assert "土耳其" in r["detail"]


def test_composite_waybill_with_turkey_passes():
    v = run_on(lambda docs: edit(docs, "railway_waybill", "waybill_type", "CIM/SMGS统一运单") or
               edit(docs, "railway_waybill", "route_countries",
                    ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]))
    assert result_of(v, "ROUTE-001")["status"] == STATUS_PASS


def test_route_ends_mismatch_is_fail():
    v = run_on(lambda docs: edit(docs, "export_customs_declaration",
                                 "destination_country", "荷兰"))
    assert result_of(v, "ROUTE-002")["status"] == STATUS_FAIL


# ---------------------------------------------------------------- OCR脏数据鲁棒性


def test_non_numeric_weight_does_not_crash():
    v = run_on(lambda docs: edit(docs, "packing_list", "gross_weight_kg", "12,300kg（约）"))
    r = result_of(v, "CONS-003")
    assert r["status"] in (STATUS_FAIL, STATUS_PASS, STATUS_WARNING)  # 任何状态都不应抛异常


def test_blank_field_degrades_to_warning_not_crash():
    v = run_on(lambda docs: edit(docs, "packing_list", "goods_description", "  "))
    assert result_of(v, "CONS-001")["status"] in (STATUS_PASS, STATUS_WARNING)


def test_full_pipeline_smoke():
    """引擎主入口冒烟：结构完整、风险分存在且在0-100。"""
    v = run_on()
    assert {"results", "summary", "risk", "suggestions"} <= set(v.keys())
    assert 0 <= v["risk"]["score"] <= 100
