# -*- coding: utf-8 -*-
"""verification_engine 单元测试（升级任务书·工程化加固）。

覆盖：字段一致、字段不一致、数值超差、必需单证缺失、语义相似度分级边界。
运行：pytest test_verification_engine.py -v
"""

import copy

import pytest

import doc_contract
from verification_engine import (STATUS_FAIL, STATUS_PASS, STATUS_WARNING,
                                 run_verification)

# ---------------------------------------------------------------- 构造工具


def base_documents() -> list:
    """一份字段齐全、彼此一致的最小单证组。"""
    return [
        {"doc_type": "invoice", "doc_id": "INV-T1", "title": "商业发票",
         "fields": {"invoice_no": "INV-T1", "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "total_amount": 86400.00, "currency": "USD"}},
        {"doc_type": "packing_list", "doc_id": "PL-T1", "title": "装箱单",
         "fields": {"packing_list_no": "PL-T1", "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "container_no": "MSKU8765432"}},
        {"doc_type": "railway_waybill", "doc_id": "SMU-T1", "title": "铁路运单",
         "fields": {"waybill_no": "SMU/T/2026", "waybill_type": "SMGS国际货协运单",
                    "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "route_countries": ["中国", "哈萨克斯坦", "俄罗斯", "白俄罗斯", "波兰", "德国"],
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "container_no": "MSKU8765432"}},
        {"doc_type": "export_customs_declaration", "doc_id": "DEC-T1", "title": "出口报关单",
         "fields": {"declaration_no": "DEC-T1", "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
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


# ---------------------------------------------------------------- F01：非法数值基准与币种（审查反例）


@pytest.mark.parametrize("bad_weight", [0, -1, "NaN"])
def test_illegal_invoice_weight_not_pass(bad_weight):
    """发票毛重 0 / -1 / NaN：不得输出全绿（修复前为 11 PASS、风险 0）。"""
    v = run_on(lambda docs: edit(docs, "invoice", "gross_weight_kg", bad_weight))
    r = result_of(v, "CONS-003")
    assert r["status"] == STATUS_FAIL
    assert v["summary"]["fail"] >= 1 and v["risk"]["score"] > 0


def test_weight_with_unit_string_flags_review_not_crash():
    """发票毛重 "12300kg"（数字带单位）：核验结果为"待复核"，不触发异常（F08反例）。"""
    v = run_on(lambda docs: edit(docs, "invoice", "gross_weight_kg", "12300kg"))
    r = result_of(v, "CONS-003")
    assert r["status"] == STATUS_WARNING
    assert "待人工复核" in r["detail"]


def test_zero_invoice_amount_is_fail():
    """发票金额改成 0 而报关金额仍 86400：金额检查必须 FAIL。"""
    v = run_on(lambda docs: edit(docs, "invoice", "total_amount", 0))
    assert result_of(v, "CONS-008")["status"] == STATUS_FAIL


def test_currency_conflict_is_fail():
    """报关币种改 EUR、发票保持 USD（数字相同）：币种矛盾必须 FAIL，不得全绿。"""
    v = run_on(lambda docs: edit(docs, "export_customs_declaration", "currency", "EUR"))
    r = result_of(v, "CONS-008")
    assert r["status"] == STATUS_FAIL
    assert "币种矛盾" in r["detail"]


def test_currency_missing_is_warning():
    """币种缺失：无法证明金额口径一致 → 待复核，不得 PASS。"""
    def drop_currency(docs):
        for d in docs:
            d["fields"].pop("currency", None)
    v = run_on(drop_currency)
    assert result_of(v, "CONS-008")["status"] == STATUS_WARNING


def test_decimal_packages_flagged_review():
    """计件数 480.5（F04 反例延续到引擎）：不得静默截断为480，须待复核。"""
    v = run_on(lambda docs: edit(docs, "packing_list", "total_packages", 480.5))
    r = result_of(v, "CONS-002")
    assert r["status"] == STATUS_WARNING
    assert "待人工复核" in r["detail"]


def test_negative_packages_is_fail():
    v = run_on(lambda docs: edit(docs, "packing_list", "total_packages", -5))
    assert result_of(v, "CONS-002")["status"] == STATUS_FAIL


# ---------------------------------------------------------------- F02：空单证与重复单证


@pytest.mark.parametrize("doc_type", ["packing_list", "certificate_of_origin"])
def test_empty_document_not_all_pass(doc_type):
    """fields={} 的单证：不得全 PASS、风险 0（修复前为全绿）。"""
    def empty_fields(docs):
        next(d for d in docs if d["doc_type"] == doc_type)["fields"] = {}
    v = run_on(empty_fields)
    r = result_of(v, "DOC-002")
    assert r["status"] == STATUS_FAIL
    assert v["risk"]["score"] > 0


def test_second_contradictory_customs_not_all_pass():
    """追加第二份报关单（金额1、运单号WRONG、运抵国UNKNOWN）：必须非全绿（修复前全绿）。"""
    def add_second(docs):
        extra = copy.deepcopy(next(d for d in docs if d["doc_type"] == "export_customs_declaration"))
        extra["doc_id"] = "SECOND-CUSTOMS"
        extra["fields"].update(declared_value=1, waybill_no="WRONG", destination_country="UNKNOWN")
        docs.append(extra)
    v = run_on(add_second)
    assert result_of(v, "DOC-003")["status"] == STATUS_FAIL
    assert v["summary"]["fail"] >= 1 and v["risk"]["score"] > 0


def test_duplicate_identical_documents_warns():
    """同类型完全相同的重复单证：提示去重（WARNING），不静默放行。"""
    def add_twin(docs):
        twin = copy.deepcopy(next(d for d in docs if d["doc_type"] == "packing_list"))
        twin["doc_id"] = "PL-T1-COPY"
        docs.append(twin)
    v = run_on(add_twin)
    r = result_of(v, "DOC-003")
    assert r["status"] == STATUS_WARNING
    assert "重复" in r["detail"]


# ---------------------------------------------------------------- F03：语义归一化不得抹掉关键规格


def test_numeric_spec_contradiction_is_fail():
    """钢板厚度1.5mm vs 15mm：修复前去标点后相同（相似度1.0、全绿），必须 FAIL。"""
    import semantic
    assert semantic.similarity("钢板厚度1.5mm", "钢板厚度15mm") < 1.0
    cmp = semantic.compare("钢板厚度1.5mm", "钢板厚度15mm")
    assert cmp["guard"] == "numeric_spec" and cmp["grade"] == "mismatch"

    def set_desc(docs):
        for d in docs:
            if "goods_description" in d["fields"]:
                d["fields"]["goods_description"] = "钢板厚度1.5mm"
        edit(docs, "export_customs_declaration", "goods_description", "钢板厚度15mm")
    v = run_on(set_desc)
    r = result_of(v, "CONS-001")
    assert r["status"] == STATUS_FAIL
    assert "关键实体矛盾" in r["detail"]


def test_battery_capacity_contradiction_is_fail():
    """电池容量 5000mAh vs 500mAh：规格数字矛盾必须 FAIL（审查发现的容量反例）。"""
    def set_desc(docs):
        for d in docs:
            if "goods_description" in d["fields"]:
                d["fields"]["goods_description"] = "锂电池组 5000mAh"
        edit(docs, "export_customs_declaration", "goods_description", "锂电池组 500mAh")
    v = run_on(set_desc)
    r = result_of(v, "CONS-001")
    assert r["status"] == STATUS_FAIL


def test_negation_contradiction_is_fail():
    """否定词差异（含木质包装 vs 不含木质包装）不得因字面相似被判一致。"""
    import semantic
    cmp = semantic.compare("陶瓷卫浴洁具 含木质包装", "陶瓷卫浴洁具 不含木质包装")
    assert cmp["guard"] == "negation" and cmp["grade"] == "mismatch"


def test_benign_restatement_still_suspect():
    """守卫不误伤：换序重述（无数字/无否定差异）仍按相似度分级走灰色区。"""
    import semantic
    cmp = semantic.compare("陶瓷卫浴洁具", "卫浴洁具陶瓷制品")
    assert cmp["guard"] is None and cmp["grade"] == "suspect"


# ---------------------------------------------------------------- F07：运单类型枚举与路线覆盖


def test_air_waybill_type_unsupported():
    """AIR WAYBILL：必须标记"类型不支持/需人工复核"，不得判类型/路线匹配。"""
    v = run_on(lambda docs: edit(docs, "railway_waybill", "waybill_type", "AIR WAYBILL"))
    r = result_of(v, "ROUTE-001")
    assert r["status"] == STATUS_FAIL
    assert "不在受支持枚举" in r["detail"]
    assert v["risk"]["score"] > 0


def test_route_with_out_of_coverage_country_not_pass():
    """CIM/SMGS统一运单 + 中国→美国→德国：美国不在覆盖集合，必须触发告警不得全绿。"""
    def set_route(docs):
        edit(docs, "railway_waybill", "waybill_type", "CIM/SMGS统一运单")
        edit(docs, "railway_waybill", "route_countries", ["中国", "美国", "德国"])
    v = run_on(set_route)
    r = result_of(v, "ROUTE-001")
    assert r["status"] in (STATUS_FAIL, STATUS_WARNING)
    assert "美国" in r["detail"]


def test_route_results_carry_rule_version_and_coverage():
    """每条路线判断结果必须附带规则版本号与覆盖范围说明字段（含WARNING/PASS路径）。"""
    for modifier in (None,
                     lambda docs: edit(docs, "railway_waybill", "waybill_type", "AIR WAYBILL"),
                     lambda docs: edit(docs, "railway_waybill", "route_countries",
                                       ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"])):
        v = run_on(modifier)
        for check_id in ("ROUTE-001", "ROUTE-002"):
            r = result_of(v, check_id)
            assert r.get("rule_version"), f"{check_id} 缺少 rule_version"
            assert r.get("rule_coverage"), f"{check_id} 缺少 rule_coverage"
            assert "规则版本" in r["detail"] and "适用" in r["detail"]


def test_composite_waybill_union_coverage():
    """统一运单按 SMGS∪CIM 并集判断：经停国分别属于两个集合也应 PASS。"""
    def set_route(docs):
        edit(docs, "railway_waybill", "waybill_type", "CIM/SMGS统一运单")
        edit(docs, "railway_waybill", "route_countries",
             ["中国", "哈萨克斯坦", "俄罗斯", "波兰", "德国", "法国"])
    v = run_on(set_route)
    assert result_of(v, "ROUTE-001")["status"] == STATUS_PASS


def test_route_as_string_flags_review_not_crash():
    """路线被改成字符串（历史移动端缺陷形态）：引擎显式告警，不崩溃不放行。"""
    v = run_on(lambda docs: edit(docs, "railway_waybill", "route_countries",
                                 "[中国, 俄罗斯, 德国]"))
    r = result_of(v, "ROUTE-001")
    assert r["status"] == STATUS_WARNING
    assert "格式异常" in r["detail"]


def test_unsupported_type_risk_is_medium():
    """类型不支持为硬错误（FAIL 25分 → 中风险），不允许 0 风险蒙混。"""
    v = run_on(lambda docs: edit(docs, "railway_waybill", "waybill_type", "AIR WAYBILL"))
    assert v["risk"]["score"] == 25 and v["risk"]["grade"] == "medium"


# ---------------------------------------------------------------- F05：人工编辑契约（Web/App 共用）


def test_edited_number_keeps_type():
    """编辑后的数值必须保持数值类型；非法输入保留原文交引擎判待复核。"""
    assert doc_contract.parse_edited_number("480", as_int=True) == 480
    assert isinstance(doc_contract.parse_edited_number("480", as_int=True), int)
    assert doc_contract.parse_edited_number("12300.5") == 12300.5
    assert doc_contract.parse_edited_number("12300kg") == "12300kg"
    assert doc_contract.parse_edited_number("  ") is None


def test_edited_route_keeps_list():
    """经停国家编辑后必须保持字符串列表（修复手机端"列表变字符串"）。"""
    assert doc_contract.split_route_text("中国、俄罗斯、德国") == ["中国", "俄罗斯", "德国"]
    assert doc_contract.split_route_text("中国,德国") == ["中国", "德国"]
    assert doc_contract.split_route_text("中国->哈萨克斯坦→德国") == \
        ["中国", "哈萨克斯坦", "德国"]
    assert doc_contract.split_route_text("  ") is None


def test_supported_waybill_type_enum_shared():
    """Web编辑下拉与引擎枚举同源：AIR WAYBILL 不可能从合法选项进入数据。"""
    assert doc_contract.SUPPORTED_WAYBILL_TYPES == [
        "SMGS国际货协运单", "CIM国际铁路运单", "CIM/SMGS统一运单"]
    assert doc_contract.classify_waybill_type("AIR WAYBILL") is None
    assert doc_contract.classify_waybill_type("SMGS国际货协运单") == "smgs"
    assert doc_contract.classify_waybill_type("CIM/SMGS统一运单") == "composite"


# ---------------------------------------------------------------- F08：API 请求契约（ASGI 直连，修复"结构错误变500"）


def _asgi_post(body: dict):
    """直接以 ASGI 调用 api.app（不依赖 httpx/TestClient），返回 (status, body)。"""
    import asyncio
    import json as _json

    import api

    data = _json.dumps(body, ensure_ascii=False).encode("utf-8")
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/verify",
             "raw_path": b"/verify", "query_string": b"",
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(data)).encode())],
             "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}

    async def call():
        await api.app(scope, receive, send)

    asyncio.run(call())
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], _json.loads(payload.decode("utf-8")) if payload else {}


def test_api_clean_batch_is_200():
    status, body = _asgi_post({"batch_id": "t", "documents": base_documents()})
    assert status == 200
    assert body["summary"]["total"] >= 11


def test_api_null_document_returns_422_not_500():
    """documents=[null]：必须返回 4xx 与可读错误，不是 500（修复前 500）。"""
    status, body = _asgi_post({"documents": [None]})
    assert 400 <= status < 500 and status != 500
    assert "documents" in _json_dumps(body)


def test_api_invalid_fields_type_returns_422_not_500():
    """fields="bad"：必须返回 4xx 且指明 fields 字段问题（修复前 500）。"""
    status, body = _asgi_post({"documents": [{"doc_type": "invoice", "fields": "bad"}]})
    assert 400 <= status < 500
    assert "fields" in _json_dumps(body)


def test_api_missing_documents_returns_422():
    status, _ = _asgi_post({"batch_id": "x"})
    assert status == 422


def test_api_empty_documents_returns_422():
    status, _ = _asgi_post({"documents": []})
    assert status == 422


def test_api_weight_with_unit_no_500():
    """"看起来正常但格式错误"的基准字段（12300kg）经 API：返回待复核而非异常。"""
    batch = {"documents": base_documents()}
    edit(batch["documents"], "invoice", "gross_weight_kg", "12300kg")
    status, body = _asgi_post(batch)
    assert status == 200
    r = next(x for x in body["results"] if x["check_id"] == "CONS-003")
    assert r["status"] == STATUS_WARNING


def _json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- 输入结构契约（F08 引擎侧）


def test_structural_garbage_does_not_crash_and_flags():
    """documents 元素为 None / fields 为字符串：引擎不崩溃，SYS-001 显式 FAIL。"""
    v = run_verification({"batch_id": "t", "documents": [None]})
    assert result_of(v, "SYS-001")["status"] == STATUS_FAIL
    v2 = run_verification({"batch_id": "t", "documents": [
        {"doc_type": "invoice", "fields": "bad"}]})
    assert result_of(v2, "SYS-001")["status"] == STATUS_FAIL
