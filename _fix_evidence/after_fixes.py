# -*- coding: utf-8 -*-
"""
整改后反例复跑脚本（交付证据用）：逐项复现 CTO 审查 F01-F10 的反例，
输出"修复后"实际结果，与 _cto_review/2026-09-17/evidence.json 中的"修复前"记录对照。

运行（离线环境，项目根目录）：
  python _fix_evidence/after_fixes.py
"""

import asyncio
import copy
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pdf_ingest
from verification_engine import run_verification

base = json.loads((ROOT / "sample_data/batch_clean.json").read_text(encoding="utf-8"))
issues = json.loads((ROOT / "sample_data/batch_with_issues.json").read_text(encoding="utf-8"))


def modified(doc_type, key, value):
    batch = copy.deepcopy(base)
    next(d for d in batch["documents"] if d["doc_type"] == doc_type)["fields"][key] = value
    return batch


def engine_summary(name, batch):
    r = run_verification(batch)
    statuses = {x["check_id"]: x["status"] for x in r["results"]}
    detail = next((x["detail"] for x in r["results"]
                   if x["status"] in ("FAIL", "WARNING")), "")
    print(f"[{name}] summary={r['summary']} risk={r['risk']['score']}/{r['risk']['grade']}")
    print(f"    非通过项: {statuses if any(s != 'PASS' for s in statuses.values()) else '无'}")
    if detail:
        print(f"    说明: {detail[:120]}")
    return r


print("=" * 90)
print("F01 非法数值与币种（修复前：全部 11 PASS、风险 0）")
for value in (0, -1, "NaN"):
    engine_summary(f"发票毛重={value!r}", modified("invoice", "gross_weight_kg", value))
engine_summary("发票毛重='12300kg'(待复核口径)", modified("invoice", "gross_weight_kg", "12300kg"))
engine_summary("发票金额=0", modified("invoice", "total_amount", 0))
engine_summary("报关币种EUR/发票USD", modified("export_customs_declaration", "currency", "EUR"))

print("=" * 90)
print("F02 空单证与重复单证（修复前：fields={} 全PASS；追加矛盾报关单仍全绿）")
for doc_type in ("packing_list", "certificate_of_origin"):
    b = copy.deepcopy(base)
    next(d for d in b["documents"] if d["doc_type"] == doc_type)["fields"] = {}
    engine_summary(f"{doc_type} fields={{}}", b)
b = copy.deepcopy(base)
extra = copy.deepcopy(next(d for d in b["documents"] if d["doc_type"] == "export_customs_declaration"))
extra["doc_id"] = "SECOND-CUSTOMS"
extra["fields"].update(declared_value=1, waybill_no="WRONG", destination_country="UNKNOWN")
b["documents"].append(extra)
engine_summary("追加矛盾的第二份报关单", b)

print("=" * 90)
print("F03 语义归一化抹平规格差异（修复前：1.5mm vs 15mm 相似度1.0 全绿）")
import semantic
print(f"[相似度] 钢板厚度1.5mm vs 15mm: {semantic.similarity('钢板厚度1.5mm', '钢板厚度15mm')}"
      f"（修复前 1.0）")
cmp = semantic.compare("钢板厚度1.5mm", "钢板厚度15mm")
print(f"[守卫] guard={cmp['guard']} grade={cmp['grade']}")
b = copy.deepcopy(base)
for d in b["documents"]:
    if "goods_description" in d["fields"]:
        d["fields"]["goods_description"] = "钢板厚度1.5mm"
next(d for d in b["documents"] if d["doc_type"] == "export_customs_declaration")["fields"]["goods_description"] = "钢板厚度15mm"
engine_summary("描述规格矛盾批次", b)

print("=" * 90)
print("F04 提取复核门槛（修复前：空INVOICE NO抓下一行SELLER、12,300LB当12300kg、")
print("     480.5截成480、丢页仍needs_review=False、unknown转invoice）")
for name, text in (
    ("blank_labels", "INVOICE NO:\nSELLER:\nBUYER: Demo Buyer\nDESCRIPTION: Ceramic\n"
                     "TOTAL PACKAGES: 480\nGROSS WEIGHT: 12300\nTOTAL AMOUNT: 86400"),
    ("units_and_decimal", "INVOICE NO: INV1\nSELLER: Demo Seller\nBUYER: Demo Buyer\n"
                          "DESCRIPTION: Ceramic\nTOTAL PACKAGES: 480.5\nGROSS WEIGHT: 12,300 LB\n"
                          "TOTAL AMOUNT: 86400"),
):
    fields, conf, warnings = pdf_ingest.extract_fields("invoice", [text])
    print(f"[{name}] invoice_no={fields.get('invoice_no')} (conf={conf['invoice_no']}), "
          f"packages={fields.get('total_packages')} (conf={conf['total_packages']}), "
          f"weight={fields.get('gross_weight_kg')} (conf={conf['gross_weight_kg']})")
    if warnings:
        print(f"    warnings: {warnings}")

from reportlab.pdfgen import canvas
buf = io.BytesIO()
pdf = canvas.Canvas(buf)
obj = pdf.beginText(40, 760)
for line in ("Unrecognized arbitrary content without any invoice labels. " * 4).split():
    obj.textLine(line)
pdf.drawText(obj)
pdf.save()
r = pdf_ingest.process_pdf(buf.getvalue(), "misc.pdf")
print(f"[unknown_pdf] doc_type={r.doc_type}, to_document.doc_type={r.to_document()['doc_type']}, "
      f"needs_review={r.needs_review}（修复前转invoice且无需复核）")

buf = io.BytesIO()
pdf = canvas.Canvas(buf)
obj = pdf.beginText(40, 760)
for line in ("INVOICE NO: INV1", "SELLER: Demo Seller", "BUYER: Demo Buyer",
             "DESCRIPTION OF GOODS: Ceramic goods", "TOTAL PACKAGES: 480",
             "GROSS WEIGHT: 12300", "TOTAL AMOUNT: 86400"):
    obj.textLine(line)
pdf.drawText(obj)
pdf.showPage()
pdf.drawString(40, 760, "scan")
pdf.save()
orig_ocr = pdf_ingest._ocr_page
pdf_ingest._ocr_page = lambda *a: (_ for _ in ()).throw(RuntimeError("simulated page OCR failure"))
try:
    r = pdf_ingest.process_pdf(buf.getvalue(), "invoice.pdf")
    print(f"[ocr丢页] error={r.error}, ocr失败页={r.pages_with_ocr_failure}, "
          f"needs_review={r.needs_review}（修复前 needs_review=False）")
finally:
    pdf_ingest._ocr_page = orig_ocr

print("=" * 90)
print("F07 未知运单类型与覆盖集合（修复前：AIR WAYBILL 匹配；CIM/SMGS+美国 全绿）")
engine_summary("AIR WAYBILL", modified("railway_waybill", "waybill_type", "AIR WAYBILL"))
b = modified("railway_waybill", "waybill_type", "CIM/SMGS")
next(d for d in b["documents"] if d["doc_type"] == "railway_waybill")["fields"]["route_countries"] = ["中国", "美国", "德国"]
r = engine_summary("CIM/SMGS统一运单+途经美国", b)
route_r = next(x for x in r["results"] if x["check_id"] == "ROUTE-001")
print(f"    rule_version={route_r.get('rule_version')}")
print(f"    rule_coverage={route_r.get('rule_coverage')[:60]}...")

print("=" * 90)
print("F08 API结构错误与带单位毛重（修复前：documents=[null]/fields='bad' 返回500；")
print("     12300kg 触发TypeError）")
import api


async def asgi_post(batch):
    body = json.dumps(batch).encode()
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/verify", "raw_path": b"/verify",
             "query_string": b"",
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())],
             "client": ("127.0.0.1", 1), "server": ("check", 80), "root_path": ""}
    await api.app(scope, receive, send)
    status = next((m["status"] for m in messages if m["type"] == "http.response.start"), None)
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, payload[:150].decode("utf-8", "ignore")


for name, batch in (("documents=[null]", {"documents": [None]}),
                    ("fields='bad'", {"documents": [{"doc_type": "invoice", "fields": "bad"}]}),
                    ("documents=[]", {"documents": []}),
                    ("发票毛重=12300kg", modified("invoice", "gross_weight_kg", "12300kg"))):
    status, payload = asyncio.run(asgi_post(batch))
    print(f"[{name}] HTTP {status}（修复前 500/异常）: {payload[:100]}")

print("=" * 90)
print("F09 缓存签名与数据版本（修复前：文件名+大小判重、固定batch_id、下载旧文本）")
import doc_contract
import hashlib


class Up:
    def __init__(self, name, data):
        self.name, self._d, self.size = name, data, len(data)

    def getvalue(self):
        return self._d


a, b2, c = Up("invoice.pdf", b"content-v1"), Up("invoice.pdf", b"content-v2"), Up("invoice.pdf", b"content-v1")
tree = __import__("ast").parse((ROOT / "app.py").read_text(encoding="utf-8"))
funcs = []
for node in tree.body:
    if isinstance(node, __import__("ast").FunctionDef) and node.name == "_file_sig":
        node.decorator_list = []
        funcs.append(node)
ns = {"hashlib": hashlib}
exec(compile(__import__("ast").Module(body=funcs, type_ignores=[]), "x", "exec"), ns)
sig = ns["_file_sig"]
print(f"[上传缓存签名] 同名同大小不同内容: sig(a)!=sig(b) → {sig(a) != sig(b2)}"
      f"（修复前相等误命中）；同内容: sig(a)==sig(c) → {sig(a) == sig(c)}")
d1 = doc_contract.data_version(issues["documents"])
edited = copy.deepcopy(issues["documents"])
next(d for d in edited if d["doc_type"] == "export_customs_declaration")["fields"]["total_packages"] = 480
print(f"[数据版本] 原批次={d1}，编辑箱数后={doc_contract.data_version(edited)}，"
      f"版本变化={d1 != doc_contract.data_version(edited)}")

print("=" * 90)
print("F10 数值推演门卫（修复前：不调工具的'风险0'响应被当有效结果展示，真实重算95）")
import chat_assistant
from verification_engine import run_verification as rv

orig_resolve, orig_post = chat_assistant.resolve_endpoint, chat_assistant._post_chat
chat_assistant.resolve_endpoint = lambda: {"api_key": "dummy", "base_url": "offline",
                                           "model": "stub"}
chat_assistant._post_chat = lambda *a, **k: {"choices": [{"message": {
    "role": "assistant", "content": "风险0，全部通过"}}]}
try:
    reply = chat_assistant.answer_question("如果把报关箱数改成480，风险会变成多少？",
                                           rv(issues), issues["documents"])
    actual = chat_assistant.simulate_field_change(
        issues["documents"], {"export_customs_declaration.total_packages": 480})
    print(f"[不配合LLM] mode={reply['mode']}（修复前 live 直接展示）")
    print(f"    回复: {reply['answer'][:90]}")
    print(f"[真实重算] 风险分={actual['new_risk_score']}（{actual['new_risk_grade']}）")
finally:
    chat_assistant.resolve_endpoint, chat_assistant._post_chat = orig_resolve, orig_post

print("=" * 90)
print("F10 评估口径与冻结留出集（修复前：main每次覆盖评估样本；0F/1F口径与评分矛盾）")
import evaluation
cases = evaluation.load_frozen_holdout()
agree, n = 0, 0
for c in cases:
    v = rv(c)
    n += 1
    rubric = evaluation.ground_truth_label(v["summary"]["fail"], v["summary"]["warning"])
    agree += (c["ground_truth"] == rubric == v["risk"]["grade"])
print(f"[冻结留出集] 读取 {n} 个固定文件（评估只读不覆盖），三方一致 {agree}/{n}")
print(f"[审查反例1] 0 FAIL+2 WARNING(25分) → 口径={evaluation.ground_truth_label(0, 2)}"
      f"（修复前标注low与模型medium矛盾）")
print(f"[审查反例2] 1 FAIL+2 WARNING(55分) → 口径={evaluation.ground_truth_label(1, 2)}"
      f"（修复前标注medium与模型high矛盾）")
print("=" * 90)
print("AFTER-FIX PROBES DONE")
