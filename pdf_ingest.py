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

import doc_contract

TEXT_PAGE_MIN_CHARS = 50          # 页面判型阈值（补充任务书给定）
OCR_LANG = "chi_sim+eng"
# 单文件最大页数（超过直接判失败并提示拆分，防止超大文件拖垮服务，F08）
MAX_PDF_PAGES = 20

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
    field_confidence: dict = dc_field(default_factory=dict)   # field -> high/review/missing
    elapsed_seconds: float = 0.0
    error: str | None = None
    warnings: list = dc_field(default_factory=list)
    pages_with_ocr_failure: list = dc_field(default_factory=list)   # OCR失败页码（从1开始）

    @property
    def needs_review(self) -> bool:
        """需人工复核：类型未识别 / 任一页OCR失败 / 任一字段提取失败或存疑。"""
        return (self.doc_type == "unknown"
                or bool(self.pages_with_ocr_failure)
                or any(v in ("missing", "review") for v in self.field_confidence.values()))

    def to_document(self) -> dict:
        """转成核验引擎的单证 schema。
        unknown 类型保留 unknown（需人工指定），不再静默转换成 invoice（修复 F04）。"""
        titles = {"invoice": "商业发票 Commercial Invoice",
                  "packing_list": "装箱单 Packing List",
                  "railway_waybill": "国际铁路运单 Railway Consignment Note",
                  "export_customs_declaration": "出口报关单 Export Customs Declaration"}
        return {
            "doc_type": self.doc_type,
            "doc_id": f"PDF-{Path(self.filename).stem[:24]}",
            "title": titles.get(self.doc_type, f"未识别单证 {self.filename}（需人工指定类型）"),
            "fields": self.fields,
        }


# 字段解析规则：字段名 -> 正则列表（忽略大小写）。
# 口径（修复 F04）：
#   - 标签部分禁止跨行（[^:：\n]*），冒号后只吃同行空白（[ \t]*）——
#     "INVOICE NO:" 空值不得再抓到下一行的 "SELLER:"；
#   - 值部分单行捕获；毛重允许"数字+单位"一起捕获，由 _finalize_field 校验单位。
FIELD_RULES = {
    "invoice": {
        "invoice_no": [r"INVOICE\s*NO[^:：\n]*[:：][ \t]*(\S+)"],
        "consignor_name": [r"SELLER[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?(?:[ \t]*[A-Za-z\u4e00-\u9fa5]+)?)"],
        "total_amount": [r"TOTAL\s*AMOUNT[^:：\n]*[:：][ \t]*(?:USD)?[ \t]*([\d,]+(?:\.\d+)?)"],
        "currency": [r"CURRENCY[^:：\n]*[:：][ \t]*(\S+)", r"币种[^:：\n]*[:：][ \t]*(\S+)"],
    },
    "packing_list": {
        "packing_list_no": [r"PACKING\s*LIST\s*NO[^:：\n]*[:：][ \t]*(\S+)"],
        "consignor_name": [r"SELLER[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?(?:[ \t]*[A-Za-z\u4e00-\u9fa5]+)?)"],
        "container_no": [r"CONTAINER\s*NO[^:：\n]*[:：][ \t]*([A-Z0-9]+)"],
    },
    "railway_waybill": {
        "waybill_no": [r"WAYBILL\s*NO[^:：\n]*[:：][ \t]*(\S+)"],
        "waybill_type": [r"WAYBILL\s*TYPE[^:：\n]*[:：][ \t]*(.+)"],
        "consignor_name": [r"SHIPPER[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"CONSIGNEE[^:：\n]*[:：][ \t]*(.+)"],
        "departure_station": [r"FROM[^:：\n]*[:：][ \t]*(.+)"],
        "destination_station": [r"\bTO\b[^:：\n]*[:：][ \t]*(.+)"],
        "route_countries": [r"VIA[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
        "gross_weight_kg": [r"GROSS\s*WEIGHT[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?(?:[ \t]*[A-Za-z\u4e00-\u9fa5]+)?)"],
        "container_no": [r"CONTAINER\s*NO[^:：\n]*[:：][ \t]*([A-Z0-9]+)"],
    },
    "export_customs_declaration": {
        "declaration_no": [r"报关单编号[^:：\n]*[:：][ \t]*(\S+)", r"DECLARATION\s*NO[^:：\n]*[:：][ \t]*(\S+)"],
        "consignor_name": [r"境内发货人[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"境外收货人[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"品名及规格[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"件数[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
        "gross_weight_kg": [r"毛重[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?(?:[ \t]*[A-Za-z\u4e00-\u9fa5]+)?)"],
        "declared_value": [r"总价[^:：\n]*[:：][ \t]*(?:USD)?[ \t]*([\d,]+(?:\.\d+)?)"],
        "currency": [r"币种[^:：\n]*[:：][ \t]*(\S+)", r"CURRENCY[^:：\n]*[:：][ \t]*(\S+)"],
        "destination_country": [r"运抵国[^:：\n]*[:：][ \t]*(.+)",
                                # OCR常把"运抵国"误读为其他字，兜底匹配括号内的"目的国"
                                r"目的国\)?[ \t]*[:：]?[ \t]*(.+)"],
        "departure_country": [r"起运国[^:：\n]*[:：][ \t]*(.+)",
                              r"发货国\)?[ \t]*[:：]?[ \t]*(.+)"],
        "waybill_no": [r"运单号[^:：\n]*[:：][ \t]*(\S+)"],
        "container_no": [r"集装箱号[^:：\n]*[:：][ \t]*([A-Z0-9]+)"],
    },
    # 原产地证与未识别类型增加通用解析规则（修复 F04：不再返回空字段+无需复核）
    "certificate_of_origin": {
        "co_no": [r"CO\s*NO[^:：\n]*[:：][ \t]*(\S+)", r"产地证编号[^:：\n]*[:：][ \t]*(\S+)"],
        "issuer": [r"ISSUER[^:：\n]*[:：][ \t]*(.+)", r"签发机构[^:：\n]*[:：][ \t]*(.+)"],
        "consignor_name": [r"SELLER[^:：\n]*[:：][ \t]*(.+)", r"发货人[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：][ \t]*(.+)", r"收货人[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：][ \t]*(.+)",
                              r"货物描述[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)",
                           r"件数[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
    },
    "unknown": {
        "consignor_name": [r"SELLER[^:：\n]*[:：][ \t]*(.+)", r"SHIPPER[^:：\n]*[:：][ \t]*(.+)",
                           r"发货人[^:：\n]*[:：][ \t]*(.+)"],
        "consignee_name": [r"BUYER[^:：\n]*[:：][ \t]*(.+)", r"CONSIGNEE[^:：\n]*[:：][ \t]*(.+)",
                           r"收货人[^:：\n]*[:：][ \t]*(.+)"],
        "goods_description": [r"DESCRIPTION OF GOODS[^:：\n]*[:：][ \t]*(.+)",
                              r"品名[^:：\n]*[:：][ \t]*(.+)"],
        "total_packages": [r"TOTAL\s*PACKAGES[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?)"],
    },
}

# 每类单证的必需字段（缺失即标记"提取失败/需人工核对"）——统一引用数据契约层
REQUIRED_FIELDS = doc_contract.REQUIRED_FIELDS

DOC_TYPE_KEYWORDS = {
    "invoice": ["COMMERCIAL INVOICE", "INVOICE NO", "发票"],
    "packing_list": ["PACKING LIST", "装箱单"],
    "railway_waybill": ["CONSIGNMENT NOTE", "WAYBILL", "运单", "RAILWAY"],
    "export_customs_declaration": ["报关单", "DECLARATION NO", "海关出口"],
    "certificate_of_origin": ["CERTIFICATE OF ORIGIN", "原产地", "CCPIT"],
}

NUMERIC_FIELDS = doc_contract.NUMERIC_FIELDS
LIST_FIELDS = doc_contract.LIST_FIELDS

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
    """兼容保留：简单清洗（供旧调用）。新提取路径使用 _finalize_field。"""
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


# 视为kg的单位（毛重单位校验，修复 F04 "12,300 LB 被当成 12300kg"）
KG_LIKE_UNITS = {"kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "公斤", "千克"}

_NUM_WITH_UNIT_RE = re.compile(r"^([\d,]+(?:\.\d+)?)[ \t]*([A-Za-z\u4e00-\u9fa5]*)$")


def _finalize_field(field: str, raw: str) -> tuple[object, str, str | None]:
    """字段值定稿：返回 (值, 置信度high/review, 警告或None)。
    修复 F04：数值必须完整解析（480.5 不得截成480）；带单位毛重必须校验单位；
    无法证明正确的值一律保留原文并标记 review（待人工复核）。"""
    value = raw.strip().rstrip("，,；;。 ")
    if field in NUMERIC_FIELDS:
        if field == "gross_weight_kg":
            m = _NUM_WITH_UNIT_RE.match(value)
            num_str, unit = (m.group(1), m.group(2)) if m else (value, "")
            num, err = doc_contract.parse_number(num_str)
            if err == doc_contract.OK:
                if unit and unit.lower() not in KG_LIKE_UNITS:
                    return value, "review", f"毛重「{value}」单位不是kg，请人工换算复核"
                return num, "high", None
            return value, "review", f"毛重「{value}」无法解析为数值，待人工复核"
        num, err = doc_contract.parse_number(value)
        if err == doc_contract.OK:
            if field in doc_contract.INTEGER_FIELDS:
                pkg_num, pkg_err = doc_contract.parse_packages(value)
                if pkg_err == doc_contract.WITH_UNIT:
                    return value, "review", f"件数「{value}」不是整数计件，已按原值保留，请人工复核"
            return num, "high", None
        if err == doc_contract.WITH_UNIT:
            return value, "review", f"「{value}」数字后带单位/备注，无法确认口径，待人工复核"
        return value, "review", f"「{value}」无法解析为数值，待人工复核"
    if field in LIST_FIELDS:
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[、,，]|->|→", value) if p.strip()]
            return (parts or value), "high", None
        return value, "high", None
    return value, "high", None


def extract_fields(doc_type: str, page_texts: list) -> tuple[dict, dict, list]:
    """按类型规则提取字段；返回 (fields, field_confidence, warnings)。
    规则命中 ≠ 正确：数值口径存疑的字段标记 review 并给出警告（修复 F04）。"""
    rules = FIELD_RULES.get(doc_type, {})
    combined = "\n".join(page_texts)
    fields, confidence, warnings = {}, {}, []
    for fname, patterns in rules.items():
        value = None
        for pat in patterns:
            m = re.search(pat, combined, re.IGNORECASE)
            if m and m.group(1).strip():
                value = m.group(1)
                break
        if value is None:
            confidence[fname] = "missing"
            continue
        final, level, warning = _finalize_field(fname, value)
        if final is None or final == "":
            confidence[fname] = "missing"
            continue
        fields[fname] = final
        confidence[fname] = level
        if warning:
            warnings.append(f"{doc_contract.FIELD_LABELS_ZH.get(fname, fname)}：{warning}")
    return fields, confidence, warnings


def process_pdf(data: bytes, filename: str, max_pages: int = MAX_PDF_PAGES) -> IngestResult:
    """完整管线：判型提取 → 类型识别 → 字段解析。max_pages 为页数上限（F08）。"""
    t0 = time.perf_counter()
    result = IngestResult(filename=filename)
    try:
        with pdfplumber.open(__io(data)) as pdf:
            if len(pdf.pages) > max_pages:
                result.error = (f"PDF页数超限（{len(pdf.pages)}页 > 上限{max_pages}页），"
                                f"请拆分后分次上传")
                result.elapsed_seconds = time.perf_counter() - t0
                return result
            page_texts = []
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
                        # OCR环境缺失/失败：页面仍标记为ocr，记录警告；
                        # 该页视为提取失败 → needs_review（修复 F04 静默丢页）
                        result.warnings.append(
                            f"第{idx + 1}页OCR失败（{ocr_exc}），该页文字未提取，结果需人工复核；"
                            f"请安装 tesseract-ocr + chi_sim（见README）")
                        result.pages_with_ocr_failure.append(idx + 1)
                        ocr_text, seconds = "", 0.0
                    page_texts.append(ocr_text)
                    result.pages.append(PageResult(idx + 1, "ocr",
                                                   len(re.sub(r"\s", "", ocr_text)),
                                                   seconds, ocr_text))
        result.doc_type, result.type_score = detect_doc_type(filename, "\n".join(page_texts))
        result.fields, result.field_confidence, extract_warnings = extract_fields(
            result.doc_type, page_texts)
        result.warnings.extend(extract_warnings)
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
            result.fields, result.field_confidence, extract_warnings = extract_fields(
                result.doc_type, [text])
            result.warnings.extend(extract_warnings)
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
