# -*- coding: utf-8 -*-
"""
PDF单证摄取模块（补充任务书）：上传PDF → 判型 → 提取文字 → 解析结构化字段。

处理管线（按页路由，同一PDF可混合判型）：
  1. 文本提取：pdfplumber 直接抽取文字层；
  2. 页面判型：某页提取字符数 < 50 判为扫描页 → OCR 兜底；
     OCR 光栅化优先用 pdf2image（依赖系统 poppler），不可用时退化为
     PyMuPDF 光栅化（免 poppler），两者均输出位图后交给 pytesseract
     （依赖系统 tesseract-ocr + 中文包 chi_sim）；
  3. 单证类型识别：文件名 + 内容关键词打分（发票/箱单/运单/报关单），
     自动判断作为默认值，界面上允许人工纠正；
  4. 字段解析：按单证类型用"关键词+正则"定位提取（最稳定可控），
     必需字段缺失时标记 confidence="missing"（需人工核对），
     不把空值/错误值静塞给核验引擎。

诚实性说明：本模块是"关键词+正则"的结构化解析，不是AI大模型抽取；
识别引擎为 tesseract OCR（扫描件路径）。字段解析效果不好的案例请记录后
反馈架构方，勿自行大幅更改解析设计。
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import pdfplumber

TEXT_PAGE_MIN_CHARS = 50          # 页面判型阈值（补充任务书给定）
OCR_LANG = "chi_sim+eng"

# ---------------------------------------------------------------- tesseract 定位


def _locate_tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(cand).exists():
            return cand
    return None


def ocr_available() -> bool:
    return _locate_tesseract() is not None


def _rasterize_page(pdf_bytes: bytes, page_index: int, dpi: int = 200):
    """光栅化指定页为PNG字节。优先 pdf2image(poppler)，退化 PyMuPDF。"""
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=page_index + 1,
                                    last_page=page_index + 1)
        import io
        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        import pymupdf as fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pix = doc[page_index].get_pixmap(dpi=dpi)
        data = pix.tobytes("png")
        doc.close()
        return data


def _ocr_page(pdf_bytes: bytes, page_index: int) -> tuple[str, float]:
    import pytesseract
    exe = _locate_tesseract()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    from PIL import Image
    import io

    t0 = time.perf_counter()
    img_bytes = _rasterize_page(pdf_bytes, page_index)
    text = pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)), lang=OCR_LANG)
    return text, time.perf_counter() - t0


# ---------------------------------------------------------------- 类型与规则


@dataclass
class PageResult:
    page_no: int                    # 从1开始
    mode: str                       # "text" | "ocr"
    chars: int
    ocr_seconds: float = 0.0
    text: str = ""


@dataclass
class IngestResult:
    filename: str
    doc_type: str = "unknown"       # invoice / packing_list / railway_waybill / export_customs_declaration / unknown
    type_score: int = 0
    pages: list = dc_field(default_factory=list)
    fields: dict = dc_field(default_factory=dict)
    field_confidence: dict = dc_field(default_factory=dict)   # field -> high/missing
    elapsed_seconds: float = 0.0
    error: str | None = None
    warnings: list = dc_field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return any(v == "missing" for v in self.field_confidence.values())

    def to_document(self) -> dict:
        """转成核验引擎的单证 schema。"""
        titles = {"invoice": "商业发票 Commercial Invoice",
                  "packing_list": "装箱单 Packing List",
                  "railway_waybill": "国际铁路运单 Railway Consignment Note",
                  "export_customs_declaration": "出口报关单 Export Customs Declaration"}
        return {
            "doc_type": self.doc_type if self.doc_type != "unknown" else "invoice",
            "doc_id": f"PDF-{Path(self.filename).stem[:24]}",
            "title": titles.get(self.doc_type, f"上传单证 {self.filename}"),
            "fields": self.fields,
        }


# 字段解析规则：字段名 -> 正则列表（忽略大小写，值取同一行冒号后内容）
FIELD_RULES = {
    "invoice": {
        "invoice_no": [r"INVOICE\s*NO[^:：]*[:：]\s*(\S+)"],
        "consignor_name": [r"SELLER[^:：\n]*[:：]\s*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：]\s*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：]\s*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：]\s*([\d,]+)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：]\s*([\d,]+(?:\.\d+)?)"],
        "total_amount": [r"TOTAL\s*AMOUNT[^:：\n]*[:：]\s*(?:USD)?\s*([\d,]+(?:\.\d+)?)"],
    },
    "packing_list": {
        "packing_list_no": [r"PACKING\s*LIST\s*NO[^:：\n]*[:：]\s*(\S+)"],
        "consignor_name": [r"SELLER[^:：\n]*[:：]\s*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：]\s*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：]\s*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：]\s*([\d,]+)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：]\s*([\d,]+(?:\.\d+)?)"],
        "container_no": [r"CONTAINER\s*NO[^:：\n]*[:：]\s*([A-Z0-9]+)"],
    },
    "railway_waybill": {
        "waybill_no": [r"WAYBILL\s*NO[^:：\n]*[:：]\s*(\S+)"],
        "waybill_type": [r"WAYBILL\s*TYPE[^:：\n]*[:：]\s*(.+)"],
        "consignor_name": [r"SHIPPER[^:：\n]*[:：]\s*(.+)"],
        "consignee_name": [r"CONSIGNEE[^:：\n]*[:：]\s*(.+)"],
        "departure_station": [r"FROM[^:：\n]*[:：]\s*(.+)"],
        "destination_station": [r"\bTO\b[^:：\n]*[:：]\s*(.+)"],
        "route_countries": [r"VIA[^:：\n]*[:：]\s*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：]\s*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：]\s*([\d,]+)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：]\s*([\d,]+(?:\.\d+)?)"],
        "container_no": [r"CONTAINER\s*NO[^:：\n]*[:：]\s*([A-Z0-9]+)"],
    },
    "export_customs_declaration": {
        "declaration_no": [r"报关单编号[^:：\n]*[:：]\s*(\S+)", r"DECLARATION\s*NO[^:：\n]*[:：]\s*(\S+)"],
        "consignor_name": [r"境内发货人[^:：\n]*[:：]\s*(.+)"],
        "consignee_name": [r"境外收货人[^:：\n]*[:：]\s*(.+)"],
        "goods_description": [r"品名及规格[^:：\n]*[:：]\s*(.+)"],
        "total_packages": [r"件数[^:：\n]*[:：]\s*([\d,]+)"],
        "gross_weight_kg": [r"毛重[^:：\n]*[:：]\s*([\d,]+(?:\.\d+)?)"],
        "declared_value": [r"总价[^:：\n]*[:：]\s*(?:USD)?\s*([\d,]+(?:\.\d+)?)"],
        "destination_country": [r"运抵国[^:：\n]*[:：]\s*(.+)",
                                # OCR常把"运抵国"误读为其他字，兜底匹配括号内的"目的国"
                                r"目的国\)?\s*[:：]?\s*(.+)"],
        "departure_country": [r"起运国[^:：\n]*[:：]\s*(.+)",
                              r"发货国\)?\s*[:：]?\s*(.+)"],
        "waybill_no": [r"运单号[^:：\n]*[:：]\s*(\S+)"],
        "container_no": [r"集装箱号[^:：\n]*[:：]\s*([A-Z0-9]+)"],
    },
}

# 每类单证的必需字段（缺失即标记"提取失败/需人工核对"）
REQUIRED_FIELDS = {
    "invoice": ["invoice_no", "consignor_name", "consignee_name",
                "goods_description", "total_packages", "total_amount"],
    "packing_list": ["consignor_name", "consignee_name", "goods_description", "total_packages"],
    "railway_waybill": ["waybill_no", "consignor_name", "consignee_name", "goods_description"],
    "export_customs_declaration": ["consignor_name", "consignee_name", "goods_description",
                                   "total_packages", "declared_value"],
    "certificate_of_origin": ["consignor_name", "consignee_name", "goods_description"],
    "unknown": ["consignor_name", "consignee_name", "goods_description"],
}

DOC_TYPE_KEYWORDS = {
    "invoice": ["COMMERCIAL INVOICE", "INVOICE NO", "发票"],
    "packing_list": ["PACKING LIST", "装箱单"],
    "railway_waybill": ["CONSIGNMENT NOTE", "WAYBILL", "运单", "RAILWAY"],
    "export_customs_declaration": ["报关单", "DECLARATION NO", "海关出口"],
    "certificate_of_origin": ["CERTIFICATE OF ORIGIN", "原产地", "CCPIT"],
}

NUMERIC_FIELDS = {"total_packages", "gross_weight_kg", "total_amount", "declared_value"}
LIST_FIELDS = {"route_countries"}

TYPE_TITLES = {
    "invoice": "商业发票 Commercial Invoice",
    "packing_list": "装箱单 Packing List",
    "railway_waybill": "国际铁路运单 Railway Consignment Note",
    "export_customs_declaration": "出口报关单 Export Customs Declaration",
    "certificate_of_origin": "原产地证书 Certificate of Origin",
}


# ---------------------------------------------------------------- 主流程


def detect_doc_type(filename: str, full_text: str) -> tuple[str, int]:
    """文件名+内容关键词打分，返回 (doc_type, score)。"""
    scores = {}
    fname = filename.upper()
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw.upper() in full_text.upper():
                score += 2
            if kw.upper() in fname:
                score += 1
        scores[doc_type] = score
    best = max(scores, key=scores.get)
    return (best if scores[best] > 0 else "unknown"), scores[best]


def _clean_value(field: str, raw: str):
    value = raw.strip().rstrip("，,；;。 ")
    if field in NUMERIC_FIELDS:
        num = value.replace(",", "")
        try:
            return float(num) if "." in num else int(num)
        except ValueError:
            return value
    if field in LIST_FIELDS:
        parts = [p.strip() for p in re.split(r"[、,，]|->|→", value) if p.strip()]
        return parts or value
    return value


def extract_fields(doc_type: str, page_texts: list) -> tuple[dict, dict]:
    """按类型规则提取字段；返回 (fields, field_confidence)。"""
    rules = FIELD_RULES.get(doc_type, {})
    combined = "\n".join(page_texts)
    fields, confidence = {}, {}
    for fname, patterns in rules.items():
        value = None
        for pat in patterns:
            m = re.search(pat, combined, re.IGNORECASE)
            if m and m.group(1).strip():
                value = _clean_value(fname, m.group(1))
                break
        if value is not None:
            fields[fname] = value
            confidence[fname] = "high"
        else:
            confidence[fname] = "missing"
    return fields, confidence


def process_pdf(data: bytes, filename: str) -> IngestResult:
    """完整管线：判型提取 → 类型识别 → 字段解析。"""
    t0 = time.perf_counter()
    result = IngestResult(filename=filename)
    try:
        page_texts = []
        with pdfplumber.open(__io(data)) as pdf:
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                chars = len(re.sub(r"\s", "", text))
                if chars >= TEXT_PAGE_MIN_CHARS:
                    page_texts.append(text)
                    result.pages.append(PageResult(idx + 1, "text", chars, 0.0, text))
                else:
                    try:
                        ocr_text, seconds = _ocr_page(data, idx)
                    except Exception as ocr_exc:
                        # OCR环境缺失/失败：页面仍标记为ocr，记录警告并继续（优雅降级）
                        result.warnings.append(
                            f"第{idx + 1}页OCR失败（{ocr_exc}），该页文字未提取；"
                            f"请安装 tesseract-ocr + chi_sim（见README）")
                        ocr_text, seconds = "", 0.0
                    page_texts.append(ocr_text)
                    result.pages.append(PageResult(idx + 1, "ocr",
                                                   len(re.sub(r"\s", "", ocr_text)),
                                                   seconds, ocr_text))
        result.doc_type, result.type_score = detect_doc_type(filename, "\n".join(page_texts))
        result.fields, result.field_confidence = extract_fields(result.doc_type, page_texts)
    except Exception as exc:  # 单文件失败不影响其他文件
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_seconds = time.perf_counter() - t0
    return result


def __io(data: bytes):
    import io
    return io.BytesIO(data)


def _ocr_image_bytes(data: bytes) -> tuple:
    """对单张图片字节做OCR，返回 (文本, 耗时秒)。"""
    import io

    import pytesseract
    from PIL import Image

    exe = _locate_tesseract()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    t0 = time.perf_counter()
    text = pytesseract.image_to_string(Image.open(io.BytesIO(data)), lang=OCR_LANG)
    return text, time.perf_counter() - t0


def process_image(data: bytes, filename: str) -> IngestResult:
    """图片摄取（App拍照/相册路径）：OCR → 类型识别 → 字段解析。"""
    t0 = time.perf_counter()
    result = IngestResult(filename=filename)
    try:
        if not ocr_available():
            result.error = ("本机未安装 tesseract OCR，无法识别图片"
                            "（请安装 tesseract-ocr + 中文包 chi_sim，或使用容器版后端）")
        else:
            text, seconds = _ocr_image_bytes(data)
            result.pages.append(PageResult(1, "ocr", len(re.sub(r"\s", "", text)),
                                           seconds, text))
            result.doc_type, result.type_score = detect_doc_type(filename, text)
            result.fields, result.field_confidence = extract_fields(result.doc_type, [text])
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_seconds = time.perf_counter() - t0
    return result


def build_batch(results: list) -> dict:
    """把多个摄取结果组装成核验引擎的批次。"""
    documents = []
    for r in results:
        if r.error:
            continue
        documents.append(r.to_document())
    return {
        "batch_id": "pdf_upload",
        "batch_name": "上传PDF识别批次",
        "description": "由上传的PDF单证经判型/OCR/字段解析后组装（可在预览表中人工修正）。",
        "destination_summary": "—",
        "documents": documents,
    }
