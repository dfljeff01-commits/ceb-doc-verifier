# -*- coding: utf-8 -*-
"""多运单上传流程重构的验收测试（任务书问题一/二/三/四）。

覆盖：
  1. PDF单据边界识别与拆分（3份运单混合PDF → 3份独立单据；边界不确定 → 提示人工确认）；
  2. 声明构成（declared_composition）：同类型多份不再判重复、超申报仍判重复、
     实收与申报不符告警（DOC-004）；
  3. 单据级规则知识库（doc_rules.yaml）：运单缺收货人=FAIL+修改建议、
     发票缺金额=FAIL、规则禁用不生效、配置错误显式上报；
  4. 分层结果结构：document_groups 按单据实例分组、批次级问题归组、
     字段级问题（field/field_issues）与修改建议可追溯；
  5. API 契约：declared_composition 进 /verify，分组结构在响应中；
  6. 回归：未申报构成时 DOC-003 维持原口径。
"""

import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import doc_rules
import pdf_ingest
from verification_engine import (STATUS_FAIL, STATUS_PASS, STATUS_WARNING,
                                 build_instance_labels, run_verification)

PDF_DIR = Path(__file__).parent / "sample_pdfs"


# ---------------------------------------------------------------- 构造工具


def _waybill_page(no: str, consignee: str = "BUYER GMBH", packages: str = "160") -> bytes:
    """单页文本型运单（reportlab直出；中文必须用内置CID字体，否则字形丢失）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = BytesIO()
    c = canvas.Canvas(buf)
    lines = [
        "国际铁路运单 RAILWAY CONSIGNMENT NOTE",
        f"WAYBILL NO(运单号): {no}",
        "WAYBILL TYPE(运单类型): CIM/SMGS统一运单",
        "SHIPPER(发货人): 甲公司",
        f"CONSIGNEE(收货人): {consignee}",
        "FROM(发站): 中国 西安新筑站",
        "TO(到站): 德国 杜伊斯堡站",
        "VIA(经由): 中国、哈萨克斯坦、俄罗斯、白俄罗斯、波兰、德国",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        f"TOTAL PACKAGES(件数): {packages}",
        "GROSS WEIGHT(毛重): 12300 KG",
        "CONTAINER NO(箱号): MSKU8765432",
    ]
    obj = c.beginText(40, 760)
    obj.setFont("STSong-Light", 11)
    for line in lines:
        obj.textLine(line)
    c.drawText(obj)
    c.save()
    return buf.getvalue()


def _merge_pdfs(pages: list) -> bytes:
    import pymupdf as fitz
    out = fitz.open()
    for data in pages:
        src = fitz.open(stream=data, filetype="pdf")
        out.insert_pdf(src)
        src.close()
    merged = out.tobytes()
    out.close()
    return merged


def base_documents() -> list:
    """一套字段齐全的单证（不含运单，运单由用例自行添加）。"""
    return [
        {"doc_type": "invoice", "doc_id": "INV-T1", "title": "商业发票",
         "fields": {"invoice_no": "INV-T1", "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                    "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                    "gross_weight_kg": 12300, "total_amount": 86400.00, "currency": "USD"}},
        {"doc_type": "packing_list", "doc_id": "PL-T1", "title": "装箱单",
         "fields": {"packing_list_no": "PL-T1", "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
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


def make_waybill(doc_id: str, no: str, **kw) -> dict:
    doc = {"doc_type": "railway_waybill", "doc_id": doc_id, "title": "铁路运单",
           "fields": {"waybill_no": no, "waybill_type": "SMGS国际货协运单",
                      "consignor_name": "甲公司", "consignee_name": "BUYER GMBH",
                      "route_countries": ["中国", "哈萨克斯坦", "俄罗斯", "白俄罗斯", "波兰", "德国"],
                      "goods_description": "陶瓷卫浴洁具", "total_packages": 480,
                      "gross_weight_kg": 12300, "container_no": "MSKU8765432"}}
    doc["fields"].update(kw)
    return doc


DECLARED_FULL = {"invoice": 1, "packing_list": 1, "railway_waybill": 3,
                 "export_customs_declaration": 1, "certificate_of_origin": 1}


def result_of(verification: dict, check_id: str) -> dict:
    return next(r for r in verification["results"] if r["check_id"] == check_id)


# ---------------------------------------------------------------- 问题一：边界识别与拆分


def test_multi_waybill_pdf_splits_into_three():
    """3份运单混合PDF：拆成3份独立单据，各自有独立编号/件数/毛重。"""
    data = _merge_pdfs([_waybill_page("SMU/A/2026", packages="100"),
                        _waybill_page("SMU/B/2026", packages="200"),
                        _waybill_page("SMU/C/2026", packages="180")])
    sr = pdf_ingest.split_pdf(data, "waybills_x3.pdf")
    assert sr.error is None
    assert not sr.needs_confirmation
    assert len(sr.groups) == 3
    nos = {g.fields.get("waybill_no") for g in sr.groups}
    assert nos == {"SMU/A/2026", "SMU/B/2026", "SMU/C/2026"}
    pkgs = sorted(g.fields.get("total_packages") for g in sr.groups)
    assert pkgs == [100.0, 180.0, 200.0]
    # 拆分后的文档 doc_id 互不相同（可并入同一批次）
    doc_ids = {g.to_document(doc_id_suffix=f"P{g.pages[0].page_no}")["doc_id"]
               for g in sr.groups}
    assert len(doc_ids) == 3


def test_single_waybill_untouched():
    """单份运单PDF：一组、无需人工确认（行为与拆分前一致）。"""
    sr = pdf_ingest.split_pdf(_waybill_page("SMU/ONE/2026"), "waybill.pdf")
    assert len(sr.groups) == 1
    assert not sr.needs_confirmation
    assert sr.groups[0].doc_type == "railway_waybill"


def test_same_waybill_two_pages_is_one_document():
    """同一份运单的正页+无编号附页：默认视为一份延续，但标记需人工确认边界。"""
    page2 = BytesIO()
    c = canvas.Canvas(page2)
    obj = c.beginText(40, 760)
    obj.setFont("STSong-Light", 11)
    for line in ("RAILWAY CONSIGNMENT NOTE 附加页",
                 "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具 附件清单"):
        obj.textLine(line)
    c.drawText(obj)
    c.save()
    sr = pdf_ingest.split_pdf(_merge_pdfs([_waybill_page("SMU/CONT/2026"),
                                           page2.getvalue()]), "wb_2p.pdf")
    assert len(sr.groups) == 1
    # 附页无单据编号，无法排除是多份单据漏拆 → 必须请用户确认，不擅自往下走
    assert sr.needs_confirmation


def test_split_results_flow_through_engine_per_document():
    """拆分结果逐份进引擎：缺收货人的那一份被单独点名（问题定位到份）。"""
    bad_page = _waybill_page("SMU/BAD/2026", consignee="")
    sr = pdf_ingest.split_pdf(
        _merge_pdfs([_waybill_page("SMU/OK1/2026"), bad_page]), "wb_bad.pdf")
    assert len(sr.groups) == 2
    docs = [g.to_document(doc_id_suffix=f"P{g.pages[0].page_no}") for g in sr.groups]
    batch = {"batch_id": "t", "documents": base_documents() + docs,
             "declared_composition": {**DECLARED_FULL, "railway_waybill": 2}}
    v = run_verification(batch)
    bad = next(d for d in docs if d["fields"].get("waybill_no") == "SMU/BAD/2026")
    good = next(d for d in docs if d["fields"].get("waybill_no") == "SMU/OK1/2026")
    rule_bad = next(r for r in v["results"]
                    if r["check_id"] == "DOC-101" and r.get("doc_id") == bad["doc_id"])
    rule_good = next(r for r in v["results"]
                     if r["check_id"] == "DOC-101" and r.get("doc_id") == good["doc_id"])
    assert rule_bad["status"] == STATUS_FAIL
    assert "收货人" in rule_bad["detail"]
    assert rule_good["status"] == STATUS_PASS
    # 分组视图：缺收货人的规范问题只落在坏运单的组里
    g_bad = next(g for g in v["document_groups"] if g["doc_id"] == bad["doc_id"])
    g_good = next(g for g in v["document_groups"] if g["doc_id"] == good["doc_id"])
    assert any(i["check_id"] == "DOC-101" for i in g_bad["issues"])
    assert not any(i["check_id"] == "DOC-101" for i in g_good["issues"])
    assert g_good["fail_count"] == 0 or all(i["check_id"] != "DOC-101"
                                            for i in g_good["issues"])


# ---------------------------------------------------------------- 问题二：声明构成


def test_declared_multi_waybill_not_duplicate():
    """申报运单3份：3份运单不再判重复单证，且每份独立参与路线核验。"""
    docs = (base_documents()[:2]
            + [make_waybill(f"WB-{i}", f"SMU/{i}/2026") for i in (1, 2, 3)]
            + base_documents()[2:])
    v = run_verification({"batch_id": "t", "documents": docs,
                          "declared_composition": DECLARED_FULL})
    assert result_of(v, "DOC-003")["status"] == STATUS_PASS
    assert "与申报构成一致" in result_of(v, "DOC-003")["detail"]
    # 每份运单各产出一条路线核验结果（可逐份追溯）
    assert sum(1 for r in v["results"] if r["check_id"] == "ROUTE-001") == 3
    # 分组标签按类型+序号（运单#1/#2/#3）
    labels = [g["label"] for g in v["document_groups"]]
    assert labels.count("运单#1") == labels.count("运单#2") == labels.count("运单#3") == 1


def test_over_declared_duplicates_still_flagged():
    """申报3份实传4份：超出申报份数的重复仍按现有口径判FAIL/WARNING。"""
    docs = (base_documents()[:2]
            + [make_waybill(f"WB-{i}", f"SMU/{i}/2026") for i in (1, 2, 3, 4)]
            + base_documents()[2:])
    v = run_verification({"batch_id": "t", "documents": docs,
                          "declared_composition": DECLARED_FULL})
    assert result_of(v, "DOC-003")["status"] in (STATUS_FAIL, STATUS_WARNING)
    assert result_of(v, "DOC-003")["involved_docs"], "重复单证结果应归到具体单据"


def test_undeclared_composition_keeps_old_behavior():
    """不申报构成：与旧行为完全一致（重复即判，不产出申报核对结论）。"""
    docs = (base_documents()[:2]
            + [make_waybill("WB-1", "SMU/1/2026"), make_waybill("WB-2", "SMU/2/2026")]
            + base_documents()[2:])
    v = run_verification({"batch_id": "t", "documents": docs})
    assert result_of(v, "DOC-003")["status"] in (STATUS_FAIL, STATUS_WARNING)
    # 未申报 → 不产出 DOC-004 结果（而非 PASS 占位）
    assert "DOC-004" not in {r["check_id"] for r in v["results"]}
    assert v["declared_composition"] is None


def test_composition_mismatch_warns_doc004():
    """实收与申报不符（少传产地证、多传运单）：DOC-004 显式告警且归批次级。"""
    docs = base_documents() + [make_waybill(f"WB-{i}", f"SMU/{i}/2026") for i in (1, 2)]
    declared = {**DECLARED_FULL, "railway_waybill": 2, "certificate_of_origin": 0}
    v = run_verification({"batch_id": "t", "documents": docs,
                          "declared_composition": declared})
    r = result_of(v, "DOC-004")
    assert r["status"] == STATUS_WARNING
    assert "原产地证书" in r["detail"]
    # DOC-004 无法归属单份单据 → 批次级
    assert any(i["check_id"] == "DOC-004" for i in v["batch_level_issues"])


# ---------------------------------------------------------------- 问题三：单据级规则知识库


def test_waybill_missing_consignee_is_hard_fail_with_suggestion():
    """验收场景：运单缺少收货人 → FAIL（非待复核）+ 具体修改建议。"""
    docs = base_documents() + [make_waybill("WB-X", "SMU/X/2026", consignee_name=" ")]
    v = run_verification({"batch_id": "t", "documents": docs,
                          "declared_composition": DECLARED_FULL})
    r = next(x for x in v["results"] if x["check_id"] == "DOC-101"
             and x.get("doc_id") == "WB-X")
    assert r["status"] == STATUS_FAIL
    assert "收货人" in r["suggestion"] and "补充" in r["suggestion"]
    fi = r["field_issues"][0]
    assert fi["field"] == "consignee_name" and fi["severity"] == "fail"
    assert v["risk"]["score"] >= 25    # 硬性不规范进入风险评分


def test_invoice_missing_amount_is_fail():
    """发票缺金额 → 该份发票的单据规范检查 FAIL。"""
    docs = base_documents()
    docs[0]["fields"].pop("total_amount")
    v = run_verification({"batch_id": "t", "documents": docs})
    r = next(x for x in v["results"] if x["check_id"] == "DOC-101"
             and x.get("doc_id") == "INV-T1")
    assert r["status"] == STATUS_FAIL
    assert any(i["field"] == "total_amount" for i in r["field_issues"])


def test_disabled_rule_not_enforced():
    """enabled:false 的规则（如箱单缺毛重模板）不产生任何结果——与冻结评估集兼容。"""
    assert doc_rules.check_document({
        "doc_type": "packing_list",
        "fields": {"packing_list_no": "PL", "consignor_name": "甲", "consignee_name": "乙",
                   "goods_description": "陶瓷", "total_packages": 10,
                   "container_no": "MSKU1234567"},
    })["status"] == doc_rules.STATUS_PASS


def test_rule_config_problems_reported():
    """配置写错（非法正则）：显式上报问题并跳过该规则，不让引擎崩溃。"""
    rules, problems = doc_rules._parse_rules(
        {"rules": [{"rule_id": "bad_regex", "doc_type": "invoice", "field": "currency",
                    "rule_type": "format", "pattern": "([bad", "severity": "warning"}]},
        "test")
    assert rules == [] and problems and "正则" in problems[0]


def test_rules_version_present_in_verification():
    v = run_verification({"batch_id": "t", "documents": base_documents()})
    assert v["doc_rules_version"].startswith("doc-rules ")


# ---------------------------------------------------------------- 问题四：分层结构


def test_document_groups_structure_and_attribution():
    """分组结构完整：每份单据一组，交叉问题按 involved_docs 归组，缺单证归批次级。"""
    docs = base_documents() + [make_waybill("WB-1", "SMU/1/2026")]
    docs = (docs[:1] + [make_waybill("WB-1", "SMU/1/2026")] + docs[1:])
    docs[2]["fields"]["gross_weight_kg"] = 12500     # 装箱单毛重偏离基准 → 交叉FAIL
    del docs[3]                                       # 缺报关单 → 批次级 DOC-001
    v = run_verification({"batch_id": "t", "documents": docs})
    ids = [g["doc_id"] for g in v["document_groups"]]
    assert set(ids) == {d["doc_id"] for d in docs}
    # 交叉比对问题按 involved_docs 落在偏差单据的组里（发票为比对基准）
    pl_group = next(g for g in v["document_groups"] if g["doc_id"] == "PL-T1")
    assert any(i["check_id"] == "CONS-003" for i in pl_group["issues"])
    # 缺报关单是批次级问题（不能归到任何一份单据）
    assert any(i["check_id"] == "DOC-001" for i in v["batch_level_issues"])
    # 交叉问题带字段标注与修改建议
    issue = next(i for i in pl_group["issues"] if i["check_id"] == "CONS-003")
    assert issue["field"] == "gross_weight_kg"
    assert issue["suggestion"]


def test_instance_labels_are_stable_and_numbered():
    labels = build_instance_labels(
        [make_waybill("A", "SMU/1"), make_waybill("B", "SMU/2"),
         {"doc_type": "invoice", "doc_id": "C", "fields": {}}])
    assert labels == {"A": "运单#1", "B": "运单#2", "C": "发票#1"}


# ---------------------------------------------------------------- API 契约


def _asgi_post(body: dict):
    import api

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
    return start["status"], json.loads(payload.decode("utf-8")) if payload else {}


def test_api_accepts_declared_composition_and_returns_groups():
    docs = (base_documents()[:2]
            + [make_waybill(f"WB-{i}", f"SMU/{i}/2026") for i in (1, 2, 3)]
            + base_documents()[2:])
    status, body = _asgi_post({"batch_id": "t", "documents": docs,
                               "declared_composition": DECLARED_FULL})
    assert status == 200
    assert body["declared_composition"] == DECLARED_FULL
    assert len(body["document_groups"]) == len(docs)
    waybill_groups = [g for g in body["document_groups"] if g["doc_type"] == "railway_waybill"]
    assert [g["label"] for g in waybill_groups] == ["运单#1", "运单#2", "运单#3"]
    assert "batch_level_issues" in body and "doc_rules_version" in body


def test_api_rejects_bad_composition_type():
    docs = base_documents() + [make_waybill("WB-1", "SMU/1/2026")]
    status, body = _asgi_post({"batch_id": "t", "documents": docs,
                               "declared_composition": {"railway_waybill": "many"}})
    assert 400 <= status < 500
    assert "declared_composition" in json.dumps(body, ensure_ascii=False)
