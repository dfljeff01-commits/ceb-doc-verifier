"""Offline CTO review evidence. Does not modify product data or call an LLM."""
import ast
import asyncio
import copy
import hashlib
import importlib.abc
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for key in ("ARK_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY", "OPENAI_API_KEY", "SEMANTIC_BACKEND"):
    os.environ.pop(key, None)
import api
import evaluation
import pdf_ingest
import semantic
from verification_engine import run_verification

OUTPUT = Path(__file__).parent
evidence = {"runtime": sys.executable, "python": sys.version.split()[0], "ocr_available": pdf_ingest.ocr_available(), "cases": []}
base = json.loads((ROOT / "sample_data/batch_clean.json").read_text(encoding="utf-8"))

def record(name, value):
    evidence["cases"].append({"name": name, "observed": value})

def modified(doc_type, key, value):
    batch = copy.deepcopy(base)
    next(d for d in batch["documents"] if d["doc_type"] == doc_type)["fields"][key] = value
    return batch

def engine(name, batch):
    try:
        result = run_verification(batch)
        record(name, {"summary": result["summary"], "risk": result["risk"], "statuses": {r["check_id"]: r["status"] for r in result["results"]}})
    except Exception as exc:
        record(name, {"exception": type(exc).__name__, "message": str(exc)})

engine("clean_baseline", base)
for value in (0, -1, "NaN", "12300kg"):
    engine(f"invoice_weight_{value}", modified("invoice", "gross_weight_kg", value))
engine("zero_invoice_amount", modified("invoice", "total_amount", 0))
engine("different_currency", modified("export_customs_declaration", "currency", "EUR"))
for doc_type in ("packing_list", "certificate_of_origin"):
    batch = copy.deepcopy(base)
    next(d for d in batch["documents"] if d["doc_type"] == doc_type)["fields"] = {}
    engine(f"empty_{doc_type}_fields", batch)
batch = copy.deepcopy(base)
for doc in batch["documents"]:
    if "goods_description" in doc["fields"]:
        doc["fields"]["goods_description"] = "钢板厚度1.5mm"
next(d for d in batch["documents"] if d["doc_type"] == "export_customs_declaration")["fields"]["goods_description"] = "钢板厚度15mm"
record("numeric_description_similarity", semantic.similarity("钢板厚度1.5mm", "钢板厚度15mm"))
engine("numeric_description_mismatch", batch)
batch = copy.deepcopy(base)
additional = copy.deepcopy(next(d for d in batch["documents"] if d["doc_type"] == "export_customs_declaration"))
additional["doc_id"] = "SECOND-CUSTOMS"
additional["fields"].update(declared_value=1, waybill_no="WRONG", destination_country="UNKNOWN")
batch["documents"].append(additional)
engine("second_inconsistent_customs_document", batch)
engine("unknown_waybill_type", modified("railway_waybill", "waybill_type", "AIR WAYBILL"))
batch = modified("railway_waybill", "waybill_type", "CIM/SMGS")
next(d for d in batch["documents"] if d["doc_type"] == "railway_waybill")["fields"]["route_countries"] = ["中国", "美国", "德国"]
engine("composite_route_outside_internal_coverage", batch)
batch = copy.deepcopy(base)
route = ["中国", "俄罗斯", "德国"]
wb = next(d for d in batch["documents"] if d["doc_type"] == "railway_waybill")
wb["fields"]["route_countries"] = route
engine("mobile_route_before_edit", batch)
wb["fields"]["route_countries"] = "[中国, 俄罗斯, 德国]"
engine("mobile_route_after_text_edit", batch)

for name, text in (
    ("blank_labels", "INVOICE NO:\nSELLER:\nBUYER: Demo Buyer\nDESCRIPTION: Ceramic\nTOTAL PACKAGES: 480\nGROSS WEIGHT: 12300\nTOTAL AMOUNT: 86400"),
    ("units_and_decimal_packages", "INVOICE NO: INV1\nSELLER: Demo Seller\nBUYER: Demo Buyer\nDESCRIPTION: Ceramic\nTOTAL PACKAGES: 480.5\nGROSS WEIGHT: 12,300 LB\nTOTAL AMOUNT: 86400"),
):
    fields, confidence = pdf_ingest.extract_fields("invoice", [text])
    record(name, {"fields": fields, "confidence": confidence})

from reportlab.pdfgen import canvas
for name, filename, text in (
    ("unrecognized_pdf", "misc.pdf", "Unrecognized document with arbitrary content and no useful invoice labels. " * 3),
    ("origin_certificate_pdf", "certificate_of_origin.pdf", "CERTIFICATE OF ORIGIN\nISSUER: Demo Chamber\nORIGIN: China\nCO NO: COO-1\nDESCRIPTION: Ceramic goods"),
):
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    obj = pdf.beginText(40, 760)
    for line in text.splitlines():
        obj.textLine(line)
    pdf.drawText(obj)
    pdf.save()
    result = pdf_ingest.process_pdf(buffer.getvalue(), filename)
    record(name, {"doc_type": result.doc_type, "fields": result.fields, "confidence": result.field_confidence, "needs_review": result.needs_review, "error": result.error, "document": result.to_document()})

buffer = io.BytesIO()
pdf = canvas.Canvas(buffer)
obj = pdf.beginText(40, 760)
for line in ("INVOICE NO: INV1", "SELLER: Demo Seller", "BUYER: Demo Buyer", "DESCRIPTION OF GOODS: Ceramic goods", "TOTAL PACKAGES: 480", "GROSS WEIGHT: 12300", "TOTAL AMOUNT: 86400"):
    obj.textLine(line)
pdf.drawText(obj)
pdf.showPage()
pdf.drawString(40, 760, "scan")
pdf.save()
original_ocr = pdf_ingest._ocr_page
def failed_ocr(*args):
    raise RuntimeError("simulated page OCR failure")
pdf_ingest._ocr_page = failed_ocr
try:
    result = pdf_ingest.process_pdf(buffer.getvalue(), "invoice.pdf")
    record("ocr_failed_second_page", {"page_modes": [p.mode for p in result.pages], "error": result.error, "warnings": result.warnings, "needs_review": result.needs_review, "confidence": result.field_confidence, "ocr_dependency": "failure stub"})
finally:
    pdf_ingest._ocr_page = original_ocr

import chat_assistant
original_resolve, original_post = chat_assistant.resolve_endpoint, chat_assistant._post_chat
chat_assistant.resolve_endpoint = lambda: {"api_key": "dummy", "base_url": "offline", "model": "stub"}
chat_assistant._post_chat = lambda *args, **kwargs: {"choices": [{"message": {"role": "assistant", "content": "风险0，全部通过"}}]}
try:
    issues = json.loads((ROOT / "sample_data/batch_with_issues.json").read_text(encoding="utf-8"))
    actual = chat_assistant.simulate_field_change(issues["documents"], {"export_customs_declaration.total_packages": 480})
    reply = chat_assistant.answer_question("如果把报关箱数改成480，风险会变成多少？", run_verification(issues), issues["documents"])
    record("hypothetical_question_without_tool", {"reply": reply, "actual_recompute": actual, "llm_transport": "stub; no provider request"})
finally:
    chat_assistant.resolve_endpoint, chat_assistant._post_chat = original_resolve, original_post

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
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/verify", "raw_path": b"/verify", "query_string": b"", "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())], "client": ("127.0.0.1", 1), "server": ("review", 80), "root_path": ""}
    caught = None
    try:
        await api.app(scope, receive, send)
    except Exception as exc:
        caught = {"exception": type(exc).__name__, "message": str(exc)}
    return {"status": next((m["status"] for m in messages if m["type"] == "http.response.start"), None), "error": caught}

for name, batch in (
    ("api_clean", base),
    ("api_empty_documents", {"documents": []}),
    ("api_null_document", {"documents": [None]}),
    ("api_invalid_fields_type", {"documents": [{"doc_type": "invoice", "fields": "bad"}]}),
    ("api_invalid_weight", modified("invoice", "gross_weight_kg", "12300kg")),
):
    record(name, asyncio.run(asgi_post(batch)))

syntax_errors = []
sources = list(ROOT.glob("*.py")) + list((ROOT / "_selftest").glob("*.py")) + list((ROOT / "sample_pdfs").glob("*.py"))
for source in sources:
    try:
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    except SyntaxError as exc:
        syntax_errors.append({"file": source.relative_to(ROOT).as_posix(), "line": exc.lineno, "message": exc.msg})
record("python_source_syntax_check", {"files_checked": len(sources), "errors": syntax_errors})

script = '''import importlib.abc, sys
class BlockMultipart(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('multipart', 'python_multipart'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockMultipart())
try:
    import api
    print('API_IMPORT_OK')
except Exception as exc:
    print(type(exc).__name__ + ': ' + str(exc))
    sys.exit(1)
'''
proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True)
record("api_startup_without_multipart_simulation", {"exit_code": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()})

cases = evaluation.build_cases()
agree = sum(run_verification(c)["risk"]["grade"] == c["ground_truth"] for c in cases)
record("evaluation_in_memory", {"agree": agree, "total": len(cases), "original_files_overwritten": False})
turkey = ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]
for with_coo in (True, False):
    engine(f"evaluation_label_counterexample_with_coo_{with_coo}", {"documents": evaluation.make_docs(desc_customs="卫浴洁具陶瓷制品", route=turkey, with_coo=with_coo)})

class Context:
    def __enter__(self): return self
    def __exit__(self, *args): return False
class FakeStreamlit:
    def __init__(self):
        self.labels = []
        self.downloads = []
        self.session_state = {"email::review": {"mode": "offline_template", "zh": "原始中文", "en": "original English"}}
    def expander(self, *args, **kwargs): return Context()
    def text_input(self, label, value, **kwargs): self.labels.append(label); return value
    def number_input(self, label, value, **kwargs): self.labels.append(label); return value
    def selectbox(self, label, options, index, **kwargs): self.labels.append(label); return options[index]
    def subheader(self, *args, **kwargs): pass
    def caption(self, *args, **kwargs): pass
    def button(self, *args, **kwargs): return False
    def tabs(self, *args, **kwargs): return [Context(), Context()]
    def text_area(self, *args, **kwargs): return "用户编辑后的文本"
    def download_button(self, label, data, **kwargs): self.downloads.append(data.decode("utf-8"))

tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
selected = []
editable_fields = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "EDITABLE_FIELDS" for t in node.targets):
        editable_fields = ast.literal_eval(node.value)
    if isinstance(node, ast.FunctionDef) and node.name in {"_file_sig", "_doc_title", "collect_edited_documents", "render_email_generator"}:
        node.decorator_list = []
        selected.append(node)
fake = FakeStreamlit()
namespace = {"st": fake, "EDITABLE_FIELDS": editable_fields, "STATUS_PASS": "PASS"}
exec(compile(ast.Module(body=selected, type_ignores=[]), "app_review_functions", "exec"), namespace)
batch = {"batch_id": "review", "documents": [{"doc_type": "invoice", "doc_id": "I1", "fields": {"invoice_no": "I1"}}]}
edited_docs, count = namespace["collect_edited_documents"](batch)
record("web_missing_fields_editor", {"rendered_edit_labels": fake.labels, "output_fields": edited_docs[0]["fields"], "source_functions_executed": ["collect_edited_documents", "_doc_title"], "ui_dependency": "stub"})
namespace["render_email_generator"]({"batch_id": "review", "results": [{"status": "FAIL"}]}, [])
record("email_edit_download", {"text_area_returned": "用户编辑后的文本", "downloaded": fake.downloads, "source_function_executed": "render_email_generator", "ui_dependency": "stub"})
record("upload_cache_same_name_and_size", {"signature_a": namespace["_file_sig"](SimpleNamespace(name="invoice.pdf", size=100)), "signature_b": namespace["_file_sig"](SimpleNamespace(name="invoice.pdf", size=100)), "content_part_of_cache_key": False})

snapshot_files = sources + list(ROOT.glob("*.md")) + [ROOT / n for n in ("requirements.txt", "Dockerfile", "start.sh", "llm_presets.json", ".dockerignore", "mobile_app/lib/main.dart", "mobile_app/pubspec.yaml", "mobile_app/pubspec.lock", "mobile_app/test/api_error_test.dart", "mobile_app/android/app/build.gradle.kts", "mobile_app/android/app/src/main/AndroidManifest.xml")]
snapshot_files += list((ROOT / "sample_data").glob("*.json")) + list((ROOT / "evaluation_set").glob("*.json"))
hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(snapshot_files)) if p.is_file()}
(OUTPUT / "source_sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
(OUTPUT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
for item in evidence["cases"]:
    value = item["observed"]
    if isinstance(value, dict) and "summary" in value:
        value = {"summary": value["summary"], "risk_score": value["risk"]["score"], "risk_grade": value["risk"]["grade"]}
    print(item["name"], json.dumps(value, ensure_ascii=False))
