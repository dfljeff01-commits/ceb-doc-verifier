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
    doc_type: str = "unknown"       # invoice / packing_list / railway_waybill / smgs_rail_waybill / export_customs_declaration / unknown
    type_score: int = 0
    pages: list = dc_field(default_factory=list)
    fields: dict = dc_field(default_factory=dict)
    field_confidence: dict = dc_field(default_factory=dict)   # field -> high/review/missing
    field_evidence: dict = dc_field(default_factory=dict)     # field -> 四状态证据（P0任务书A1）
    elapsed_seconds: float = 0.0
    error: str | None = None
    warnings: list = dc_field(default_factory=list)
    pages_with_ocr_failure: list = dc_field(default_factory=list)   # OCR失败页码（从1开始）
    identity_value: str | None = None        # 该份单据识别到的单据编号（拆分场景用于展示）
    split_needs_confirmation: bool = False   # 拆分边界不确定（多单据PDF场景，任务书问题一）

    @property
    def needs_review(self) -> bool:
        """需人工复核：类型未识别 / 任一页OCR失败 / 任一字段待确认或未找到。
        注意（P0任务书A1）：not_found 只是"未找到候选值"，不是业务缺失——
        需要人工处理，但不等于单据不合格。"""
        return (self.doc_type == "unknown"
                or bool(self.pages_with_ocr_failure)
                or any(v in ("missing", "review") for v in self.field_confidence.values()))

    def to_document(self, doc_id_suffix: str = "") -> dict:
        """转成核验引擎的单证 schema。
        unknown 类型保留 unknown（需人工指定），不再静默转换成 invoice（修复 F04）。
        doc_id_suffix 用于多单据PDF拆分场景区分同一文件的不同单据实例。"""
        titles = {"invoice": "商业发票 Commercial Invoice",
                  "packing_list": "装箱单 Packing List",
                  "railway_waybill": "国际铁路运单 Railway Consignment Note",
                  "smgs_rail_waybill": "国际货协运单（СМГС）SMGS Rail Waybill",
                  "export_customs_declaration": "出口报关单 Export Customs Declaration"}
        stem = Path(self.filename).stem[:24]
        doc_id = f"PDF-{stem}" + (f"-{doc_id_suffix}" if doc_id_suffix else "")
        title = titles.get(self.doc_type, f"未识别单证 {self.filename}（需人工指定类型）")
        doc = {
            "doc_type": self.doc_type,
            "doc_id": doc_id,
            "title": title,
            "fields": self.fields,
        }
        if self.field_evidence:
            # 四状态证据随单据走（页面/规则/报告按证据区分识别与业务口径）
            doc["field_meta"] = self.field_evidence
        return doc


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
        # 净重（doc-rules v2.0 装箱单净重规则的提取配套，样式与毛重一致）
        "net_weight_kg": [r"NET\s*WEIGHT[^:：\n]*[:：][ \t]*([\d,]+(?:\.\d+)?(?:[ \t]*[A-Za-z\u4e00-\u9fa5]+)?)"],
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
    "smgs_rail_waybill": ["НАКЛАДНАЯ СМГС", "SMGS NAKLADNAYA", "СМГС NAKLADNAJA",
                           "国际货协运单", "货协运单"],
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
    """文件名+内容关键词打分，返回 (doc_type, score)。
    SMGS优先：СМГС/国际货协运单是比"运单"更强的版式信号（任务书A2类型先行），
    命中即归入 smgs_rail_waybill，不再落入泛化运单。"""
    upper = full_text.upper()
    for kw in DOC_TYPE_KEYWORDS["smgs_rail_waybill"]:
        if kw.upper() in upper:
            return "smgs_rail_waybill", 6
    scores = {}
    fname = filename.upper()
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        if doc_type == "smgs_rail_waybill":
            continue
        score = 0
        for kw in keywords:
            if kw.upper() in upper:
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
        if field in ("gross_weight_kg", "net_weight_kg"):
            label = doc_contract.FIELD_LABELS_ZH.get(field, field)
            m = _NUM_WITH_UNIT_RE.match(value)
            num_str, unit = (m.group(1), m.group(2)) if m else (value, "")
            num, err = doc_contract.parse_number(num_str)
            if err == doc_contract.OK:
                if unit and unit.lower() not in KG_LIKE_UNITS:
                    return value, "review", f"{label}「{value}」单位不是kg，请人工换算复核"
                return num, "high", None
            return value, "review", f"{label}「{value}」无法解析为数值，待人工复核"
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


def extract_fields_evidence(data: bytes | None, pages: list, doc_type: str) -> IngestResult:
    """版式感知抽取（P0任务书A1/A3/A5）：栏位号/坐标/多语言标签候选 + 证据，
    未命中字段回落到旧正则规则。data 为空（重分组等无原始字节场景）时直接
    走旧正则路径（行为与v1.x一致）。"""
    result = IngestResult(filename="")
    result.doc_type = doc_type
    if data:
        try:
            import field_extraction
            units = field_extraction.build_page_units(data, pages)
            outcome = field_extraction.extract_document_fields(
                doc_type, units, legacy_fallback=extract_fields)
            result.fields = outcome.fields
            result.field_confidence = outcome.confidence
            result.field_evidence = outcome.evidence
            result.warnings = list(outcome.warnings)
            return result
        except Exception as exc:
            # 版式层异常不阻断摄取：退回旧正则路径并留警告
            result.warnings.append(f"版式感知抽取异常（{type(exc).__name__}: {exc}），已回退通用规则")
    fields, confidence, warnings = extract_fields(doc_type, [p.text for p in pages])
    result.fields, result.field_confidence, result.warnings = fields, confidence, warnings
    return result


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


def extract_pages(data: bytes, max_pages: int) -> tuple[list, list, list, str | None]:
    """按页提取文字（文本直取/OCR兜底），供整份处理与多单据拆分共用。
    返回 (PageResult列表, 警告列表, OCR失败页码列表, 错误或None)。"""
    pages: list[PageResult] = []
    warnings: list[str] = []
    ocr_failed: list[int] = []
    with pdfplumber.open(__io(data)) as pdf:
        if len(pdf.pages) > max_pages:
            return [], [], [], (f"PDF页数超限（{len(pdf.pages)}页 > 上限{max_pages}页），"
                                f"请拆分后分次上传")
        for idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            chars = len(re.sub(r"\s", "", text))
            if chars >= TEXT_PAGE_MIN_CHARS:
                pages.append(PageResult(idx + 1, "text", chars, 0.0, text))
            else:
                try:
                    ocr_text, seconds = _ocr_page(data, idx)
                except Exception as ocr_exc:
                    # OCR环境缺失/失败：页面仍标记为ocr，记录警告；
                    # 该页视为提取失败 → needs_review（修复 F04 静默丢页）
                    warnings.append(
                        f"第{idx + 1}页OCR失败（{ocr_exc}），该页文字未提取，结果需人工复核；"
                        f"请安装 tesseract-ocr + chi_sim（见README）")
                    ocr_failed.append(idx + 1)
                    ocr_text, seconds = "", 0.0
                pages.append(PageResult(idx + 1, "ocr",
                                        len(re.sub(r"\s", "", ocr_text)),
                                        seconds, ocr_text))
    return pages, warnings, ocr_failed, None


def process_pdf(data: bytes, filename: str, max_pages: int = MAX_PDF_PAGES) -> IngestResult:
    """完整管线：判型提取 → 类型识别 → 字段解析。max_pages 为页数上限（F08）。
    注意：本函数把整份PDF当作一份单据处理；混合多单据PDF请用 split_pdf()
    先按单据边界拆分（任务书问题一）。"""
    t0 = time.perf_counter()
    result = IngestResult(filename=filename)
    try:
        pages, warnings, ocr_failed, error = extract_pages(data, max_pages)
        if error:
            result.error = error
            result.elapsed_seconds = time.perf_counter() - t0
            return result
        result.pages = pages
        result.warnings = list(warnings)
        result.pages_with_ocr_failure = list(ocr_failed)
        page_texts = [p.text for p in pages]
        result.doc_type, result.type_score = detect_doc_type(filename, "\n".join(page_texts))
        ev_result = extract_fields_evidence(data, result.pages, result.doc_type)
        result.fields = ev_result.fields
        result.field_confidence = ev_result.field_confidence
        result.field_evidence = ev_result.field_evidence
        result.warnings.extend(ev_result.warnings)
    except Exception as exc:  # 单文件失败不影响其他文件
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_seconds = time.perf_counter() - t0
    return result


# ---------------------------------------------------------------- 多单据边界识别与拆分（任务书问题一）
#
# 一份PDF里可能扫描了多份独立单据（多车厢多份运单/多批货物混扫）。拆分信号：
#   1. 页面单证类型变化（发票页 → 运单页）；
#   2. 同类型页面出现"新的单据编号"（两个不同的 WAYBILL NO 即两份运单）。
# 同类型且无新编号的连续页视为同一份单据的延续（运单正页+附页）。
# 无法确认时（如后续页完全没提取到编号）needs_confirmation=True，
# 由界面让用户逐页确认归属，系统不擅自猜边界往下走。


@dataclass
class PageGroup:
    """一份候选单据：页码集合 + 判定类型 + 组内识别到的单据编号 + 判定依据。"""
    page_nos: list = dc_field(default_factory=list)       # 从1开始，升序
    doc_type: str = "unknown"
    identity_value: str | None = None
    signals: list = dc_field(default_factory=list)


@dataclass
class SplitResult:
    """整份PDF的拆分结果：候选分组 + 逐份摄取结果 + 是否需要人工确认边界。"""
    filename: str
    pages: list = dc_field(default_factory=list)          # 全部 PageResult（供重新分组复用，不重复OCR）
    groups: list = dc_field(default_factory=list)         # list[IngestResult]，每组一份单据
    assignment: list = dc_field(default_factory=list)     # 页(0基) -> 组序号，默认自动拆分结果
    group_types: list = dc_field(default_factory=list)    # 组序号 -> doc_type（用户可纠正后重建）
    needs_confirmation: bool = False
    reason: str = ""
    error: str | None = None
    elapsed_seconds: float = 0.0


def detect_page_identity(page_text: str, doc_type: str) -> str | None:
    """在页面文本中查找该类型的单据编号（仅按该类型的编号字段查找，
    避免把报关单"随附单证-运单号"栏误认成运单页的编号）。"""
    field = doc_contract.IDENTITY_FIELDS.get(doc_type)
    if not field:
        return None
    for pat in FIELD_RULES.get(doc_type, {}).get(field, []):
        m = re.search(pat, page_text, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def detect_doc_groups(pages: list) -> tuple[list, bool, str]:
    """按页信号把一份PDF拆成候选单据组。
    返回 (PageGroup列表, needs_confirmation, 原因说明)。"""
    page_infos = []
    for p in pages:
        text = p.text or ""
        doc_type, score = detect_doc_type("", text)
        page_infos.append({"page_no": p.page_no, "type": doc_type, "score": score,
                           "identity": detect_page_identity(text, doc_type) if doc_type != "unknown" else None})

    groups: list[PageGroup] = []
    for info in page_infos:
        attach = False
        if groups:
            last = groups[-1]
            same_type = info["type"] != "unknown" and info["type"] == last.doc_type
            no_new_identity = (info["identity"] is None
                               or last.identity_value is None
                               or info["identity"] == last.identity_value)
            attach = same_type and no_new_identity
        if attach:
            last = groups[-1]
            last.page_nos.append(info["page_no"])
            if last.identity_value is None and info["identity"]:
                last.identity_value = info["identity"]
                last.signals.append(f"第{info['page_no']}页出现单据编号 {info['identity']}")
        else:
            g = PageGroup(page_nos=[info["page_no"]], doc_type=info["type"],
                          identity_value=info["identity"])
            if info["identity"]:
                g.signals.append(f"第{info['page_no']}页识别到单据编号 {info['identity']}"
                                 f"（{doc_contract.DOC_TYPE_LABELS.get(info['type'], info['type'])}）")
            elif info["type"] != "unknown":
                g.signals.append(f"第{info['page_no']}页识别为"
                                 f"{doc_contract.DOC_TYPE_LABELS.get(info['type'], info['type'])}，"
                                 f"未提取到单据编号")
            groups.append(g)

    needs, reason = False, ""
    unknown_pages = [info["page_no"] for info in page_infos if info["type"] == "unknown"]
    if unknown_pages:
        needs = True
        reason = f"第 {'、'.join(map(str, unknown_pages))} 页无法识别单证类型（扫描件OCR失败或非标准模板）"
    elif len(groups) > 10:
        needs = True
        reason = f"自动拆分出 {len(groups)} 份单据，数量异常，请人工确认边界"
    else:
        for g in groups:
            if len(g.page_nos) > 1:
                later_no_identity = [n for n in g.page_nos[1:]
                                     if not detect_page_identity(pages[n - 1].text or "", g.doc_type)]
                if later_no_identity:
                    needs = True
                    reason = (f"第 {'、'.join(map(str, g.page_nos))} 页疑似同一份单据，"
                              f"但其中第 {'、'.join(map(str, later_no_identity))} 页未提取到单据编号，"
                              f"无法排除是多份单据被漏拆，请人工确认边界")
                    break
    return groups, needs, reason


def _build_group_result(filename: str, group_pages: list, ocr_failed: list,
                        data: bytes | None = None) -> IngestResult:
    """把一组页面构建为独立的 IngestResult（类型识别+字段解析按组执行）。
    data 提供时启用版式感知抽取（栏位/坐标/证据），否则走旧正则路径。"""
    page_texts = [p.text for p in group_pages]
    doc_type, type_score = detect_doc_type(filename, "\n".join(page_texts))
    ev_result = extract_fields_evidence(data, group_pages, doc_type)
    group_failed = [p.page_no for p in group_pages if p.page_no in ocr_failed]
    return IngestResult(
        filename=filename,
        doc_type=doc_type,
        type_score=type_score,
        pages=list(group_pages),
        fields=ev_result.fields,
        field_confidence=ev_result.field_confidence,
        field_evidence=ev_result.field_evidence,
        warnings=list(ev_result.warnings),
        pages_with_ocr_failure=group_failed,
        identity_value=detect_page_identity("\n".join(page_texts), doc_type),
    )


def rebuild_groups(filename: str, pages: list, assignment: list,
                   ocr_failed: list, group_types: list | None = None,
                   data: bytes | None = None) -> list:
    """按"页 -> 组"归属关系重建各组摄取结果（用户在界面调整边界/纠正类型后调用）。
    assignment 为与 pages 等长的组序号列表（0基）。group_types 提供时作为各组类型
    （人工纠正），否则按组内文本自动识别。"""
    max_idx = max(assignment) if assignment else 0
    buckets: list[list] = [[] for _ in range(max_idx + 1)]
    for page, g_idx in zip(pages, assignment):
        buckets[g_idx].append(page)
    results = []
    for group_pages in buckets:
        if not group_pages:
            continue
        r = _build_group_result(filename, group_pages, set(ocr_failed), data=data)
        if group_types:
            forced = group_types[len(results)] if len(results) < len(group_types) else None
            if forced and forced != r.doc_type and forced != "keep":
                forced_result = extract_fields_evidence(data, group_pages, forced)
                r.fields = forced_result.fields
                r.field_confidence = forced_result.field_confidence
                r.field_evidence = forced_result.field_evidence
                r.warnings.extend(forced_result.warnings)
                r.doc_type = forced
        results.append(r)
    return results


def split_pdf(data: bytes, filename: str, max_pages: int = MAX_PDF_PAGES) -> SplitResult:
    """多单据PDF摄取入口：逐页提取 → 单据边界识别 → 逐份独立的提取+校验输入。
    边界不确定时 needs_confirmation=True 并给出原因，由用户确认/调整后
    用 rebuild_groups() 重建，系统不擅自猜边界（任务书问题一）。"""
    t0 = time.perf_counter()
    result = SplitResult(filename=filename)
    try:
        pages, warnings, ocr_failed, error = extract_pages(data, max_pages)
        if error:
            result.error = error
            result.elapsed_seconds = time.perf_counter() - t0
            return result
        result.pages = pages
        page_warnings = list(warnings)

        groups, needs, reason = detect_doc_groups(pages)
        assignment = []
        for g_idx, g in enumerate(groups):
            assignment.extend([g_idx] * len(g.page_nos))
        result.assignment = assignment
        result.needs_confirmation = needs
        result.reason = reason

        group_failed = set(ocr_failed)
        for g_idx, g in enumerate(groups):
            group_pages = [pages[n - 1] for n in g.page_nos]
            r = _build_group_result(filename, group_pages, group_failed, data=data)
            r.identity_value = g.identity_value or r.identity_value
            # 页级警告（OCR失败）归属到对应组
            r.warnings.extend(w for w in page_warnings
                              if any(f"第{n}页" in w for n in g.page_nos))
            r.split_needs_confirmation = needs
            result.groups.append(r)
    except Exception as exc:
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
            ev_result = extract_fields_evidence(None, result.pages, result.doc_type)
            result.fields = ev_result.fields
            result.field_confidence = ev_result.field_confidence
            result.field_evidence = ev_result.field_evidence
            result.warnings.extend(ev_result.warnings)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_seconds = time.perf_counter() - t0
    return result


def build_batch(results: list) -> dict:
    """把多个摄取结果组装成核验引擎的批次。
    batch_id 由单证内容哈希派生（修复 F09：固定 batch_id 会让邮件/对话等
    按 batch_id 关联的材料在换文件后仍复用旧数据）。"""
    documents = []
    for r in results:
        if r.error:
            continue
        documents.append(r.to_document())
    dv = doc_contract.data_version(documents)
    return {
        "batch_id": f"pdf_upload_{dv}",
        "batch_name": "上传PDF识别批次",
        "description": "由上传的PDF单证经判型/OCR/字段解析后组装（可在预览表中人工修正）。",
        "destination_summary": "—",
        "documents": documents,
    }
