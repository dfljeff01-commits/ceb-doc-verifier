# -*- coding: utf-8 -*-
"""pdf_ingest 单元测试（文本型路径全覆盖；OCR路径在无tesseract环境下跳过）。

前置：先运行 sample_pdfs/generate_samples.py 生成测试PDF。
运行：pytest test_pdf_ingest.py -v
"""

from pathlib import Path

import pytest

import pdf_ingest
from pdf_ingest import (TEXT_PAGE_MIN_CHARS, build_batch, detect_doc_type,
                        extract_fields, ocr_available, process_pdf)

PDF_DIR = Path(__file__).parent / "sample_pdfs"


def load(name: str) -> bytes:
    return (PDF_DIR / name).read_bytes()


def test_sample_pdfs_exist():
    assert (PDF_DIR / "invoice.pdf").exists(), "请先运行 sample_pdfs/generate_samples.py"


def test_text_pdf_fast_path_extracts_fields():
    r = process_pdf(load("invoice.pdf"), "invoice.pdf")
    assert r.error is None
    assert r.doc_type == "invoice"
    assert all(p.mode == "text" for p in r.pages)          # 未走OCR
    assert r.fields["total_packages"] == 480
    assert r.fields["gross_weight_kg"] == 12300
    assert r.fields["total_amount"] == 86400.0
    assert r.fields["consignee_name"] == "ISTANBUL YAPI SANAYI A.S."
    assert r.elapsed_seconds < 2.0                          # 文本路径毫秒级


def test_scanned_pdf_pages_flagged_ocr():
    """判型：扫描型PDF所有页应判为ocr模式（文字层字符数<阈值）。"""
    r = process_pdf(load("scan_invoice.pdf"), "scan_invoice.pdf")
    assert r.error is None                       # OCR环境缺失不应导致整个文件失败
    assert all(p.mode == "ocr" for p in r.pages)
    if ocr_available():
        assert r.pages[0].ocr_seconds > 0


def test_mixed_pdf_classified_per_page():
    """混合型：第1页文本、第2页扫描，按页判型。"""
    r = process_pdf(load("mixed_docs.pdf"), "mixed_docs.pdf")
    modes = [p.mode for p in r.pages]
    assert modes[0] == "text"
    assert modes[-1] == "ocr"


@pytest.mark.skipif(not ocr_available(), reason="本机未安装 tesseract OCR")
def test_ocr_recovers_chinese_text():
    """扫描件OCR：能识别出中文与关键字段（需 tesseract + chi_sim）。

    OCR可能丢失/粘连空格，断言按"去空白+大写"比较（与引擎企业名比对口径一致）；
    提取层如实返回OCR原文，残余噪声由预览表+人工纠正兜底。
    """
    import re

    def nospace(s):
        return re.sub(r"\s+", "", s or "").upper()

    r = process_pdf(load("scan_invoice.pdf"), "scan_invoice.pdf")
    assert r.error is None
    assert r.fields.get("total_packages") == 480
    assert nospace(r.fields.get("consignee_name")) == nospace("ISTANBUL YAPI SANAYI A.S.")
    assert r.field_confidence.get("gross_weight_kg") == "high"


def test_missing_keyword_marked_not_fabricated():
    """缺毛重栏的发票：该字段标记missing，不静塞错误值。"""
    r = process_pdf(load("invoice_missing_weight.pdf"), "invoice_missing_weight.pdf")
    assert r.fields.get("gross_weight_kg") is None
    assert r.field_confidence["gross_weight_kg"] == "missing"
    assert r.needs_review


def test_doc_type_detection_by_content_and_filename():
    text = "COMMERCIAL INVOICE\nINVOICE NO: INV-1\nSELLER: 甲"
    assert detect_doc_type("invoice.pdf", text)[0] == "invoice"
    assert detect_doc_type("whatever.pdf",
                           "装箱单 PACKING LIST\nPACKING LIST NO: PL-1")[0] == "packing_list"
    assert detect_doc_type("empty.pdf", "")[0] == "unknown"


def test_field_extraction_rules_per_type():
    fields, conf, _warn = extract_fields(
        "export_customs_declaration",
        ["报关单编号(DECLARATION NO): 29152026000123456",
         "境内发货人(发货人): 山西洁康陶瓷制品有限公司",
         "品名及规格(货物描述): 卫浴陶瓷制品",
         "件数(箱数): 475",
         "总价(申报金额): USD 86,400.00"])
    assert fields["declaration_no"] == "29152026000123456"
    assert fields["goods_description"] == "卫浴陶瓷制品"
    assert fields["total_packages"] == 475
    assert fields["declared_value"] == 86400.0
    assert conf["declared_value"] == "high"


def test_build_batch_skips_failed_files():
    good = process_pdf(load("invoice.pdf"), "invoice.pdf")
    bad = process_pdf(b"not a pdf", "broken.pdf")
    assert bad.error is not None
    batch = build_batch([good, bad])
    assert len(batch["documents"]) == 1
    assert batch["documents"][0]["doc_type"] == "invoice"


def test_extracted_documents_flow_through_engine():
    """端到端：上传的4份单证提取后能直接进核验引擎并复现预期问题。"""
    from verification_engine import run_verification
    docs = [process_pdf(load(f), f) for f in
            ("invoice.pdf", "packing_list.pdf", "waybill.pdf", "customs_declaration.pdf")]
    batch = build_batch(docs)
    v = run_verification(batch)
    # 提取出的单证自带：缺产地证 + 品名不一致 + 箱数475(报关) ≠ 480(发票/箱单)
    assert v["summary"]["fail"] >= 3
    assert 0 <= v["risk"]["score"] <= 100


def test_page_mode_threshold_constant():
    assert TEXT_PAGE_MIN_CHARS == 50


# ---------------------------------------------------------------- F04：提取错误与丢页的复核门槛


def test_blank_label_does_not_capture_next_line():
    """空 "INVOICE NO:" 不得抓到下一行 SELLER 值（修复前 invoice_no='Demo Buyer'）。"""
    text = ("INVOICE NO:\nSELLER:\nBUYER: Demo Buyer\nDESCRIPTION: Ceramic\n"
            "TOTAL PACKAGES: 480\nGROSS WEIGHT: 12300\nTOTAL AMOUNT: 86400")
    fields, conf, _ = extract_fields("invoice", [text])
    assert fields.get("invoice_no") is None
    assert conf["invoice_no"] == "missing"
    assert conf["consignor_name"] == "missing"      # 空 SELLER 不得抓 BUYER 行
    assert fields.get("consignee_name") == "Demo Buyer"


def test_weight_with_foreign_unit_flagged_review():
    """"12,300 LB" 不得静默当成 12300kg：保留原文并标记待复核。"""
    text = ("INVOICE NO: INV1\nSELLER: S\nBUYER: B\nDESCRIPTION: Ceramic\n"
            "TOTAL PACKAGES: 480\nGROSS WEIGHT: 12,300 LB\nTOTAL AMOUNT: 86400")
    fields, conf, warnings = extract_fields("invoice", [text])
    assert fields.get("gross_weight_kg") == "12,300 LB"     # 不静默转换为数字
    assert conf["gross_weight_kg"] == "review"
    assert any("LB" in w for w in warnings)


def test_weight_with_kg_unit_is_high():
    """单位为 KG 时正常高置信提取（样本PDF格式）。"""
    fields, conf, warnings = extract_fields(
        "invoice", ["GROSS WEIGHT(毛重): 12300 KG"])
    assert fields["gross_weight_kg"] == 12300
    assert conf["gross_weight_kg"] == "high"
    assert warnings == []


def test_decimal_packages_not_truncated():
    """件数 480.5 不得截断为 480：保留原值并标记待复核。"""
    text = ("INVOICE NO: INV1\nSELLER: S\nBUYER: B\nDESCRIPTION: Ceramic\n"
            "TOTAL PACKAGES: 480.5\nGROSS WEIGHT: 12300\nTOTAL AMOUNT: 86400")
    fields, conf, warnings = extract_fields("invoice", [text])
    assert fields.get("total_packages") == "480.5"
    assert conf["total_packages"] == "review"
    assert any("480.5" in w for w in warnings)


def test_unknown_type_keeps_unknown_and_needs_review():
    """无法识别的单证：to_document 保留 unknown（不再静默转 invoice），需人工指定。"""
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    obj = pdf.beginText(40, 760)
    for line in ("Unrecognized document with arbitrary content and no useful labels. " * 3).split():
        obj.textLine(line)
    pdf.drawText(obj)
    pdf.save()
    r = process_pdf(buf.getvalue(), "misc.pdf")
    assert r.doc_type == "unknown"
    assert r.to_document()["doc_type"] == "unknown"
    assert r.needs_review


def test_origin_certificate_has_extraction_rules():
    """原产地证类型有字段解析规则，不再返回空字段+needs_review=False。"""
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    obj = pdf.beginText(40, 760)
    for line in ("CERTIFICATE OF ORIGIN\nISSUER: Demo Chamber\n"
                 "SELLER: Demo Seller\nBUYER: Demo Buyer\n"
                 "DESCRIPTION OF GOODS: Ceramic goods\nTOTAL PACKAGES: 480").splitlines():
        obj.textLine(line)
    pdf.drawText(obj)
    pdf.save()
    r = process_pdf(buf.getvalue(), "certificate_of_origin.pdf")
    assert r.doc_type == "certificate_of_origin"
    assert r.fields.get("consignee_name") == "Demo Buyer"
    assert r.fields.get("total_packages") == 480


def test_ocr_page_failure_forces_review(monkeypatch):
    """任一页 OCR 失败：needs_review 必须为 True，不得静默 error=None+无需复核。"""
    def failed_ocr(*args):
        raise RuntimeError("simulated page OCR failure")
    monkeypatch.setattr(pdf_ingest, "_ocr_page", failed_ocr)
    import io
    from reportlab.pdfgen import canvas
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
    r = process_pdf(buf.getvalue(), "invoice.pdf")
    assert r.error is None
    assert r.pages_with_ocr_failure == [2]
    assert r.needs_review
    assert any("OCR失败" in w for w in r.warnings)


def test_pdf_page_limit_rejects_oversized(monkeypatch):
    """超过页数上限：显式失败并提示拆分，不进入解析（F08）。"""
    import pdfplumber

    class FakePdf:
        pages = [object()] * 25

    class FakeHandle:
        def __enter__(self):
            return FakePdf()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pdfplumber, "open", lambda *a, **kw: FakeHandle())
    r = process_pdf(b"%PDF-fake", "big.pdf", max_pages=20)
    assert r.error is not None
    assert "页数超限" in r.error
