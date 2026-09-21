# -*- coding: utf-8 -*-
"""
版式感知字段抽取层（P0任务书A1/A3/A5）。

定位：在 pdf_ingest 的"文字提取（文本直取/OCR）"之上、字段定稿之前，提供
基于 版面坐标 + 栏位号 + 多语言标签 的候选抽取，并给每个字段产出**证据**
（原文/页码/来源栏位/坐标/方法/置信度/四状态），供页面、规则引擎与报告
复核——"识别"与"业务"自此分离：抽取不到 ≠ 单据缺项。

结构（类型先行，任务书A2）：
  extract_document_fields(doc_type, units)
    ├─ SMGS适配器（栏位号+中俄英标签+坐标区域，任务书A3）
    └─ 通用适配器（中英俄同义标签 + 跨行取值） → 兜底沿用 pdf_ingest.FIELD_RULES 正则

四状态（doc_contract 统一常量）：recognized / needs_review / not_found /
business_missing(/not_applicable)。business_missing 只能由人工确认产生，
本模块绝不输出该状态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

import doc_contract as dc

# ---------------------------------------------------------------- 页面单元（坐标层）


@dataclass
class Line:
    text: str
    page: int
    bbox: tuple | None = None        # (x0, y0, x1, y1)；OCR页无坐标
    mode: str = "text"               # text | ocr


@dataclass
class PageUnits:
    page_no: int
    mode: str
    lines: list = dc_field(default_factory=list)   # list[Line]，按阅读顺序

    def all_text(self) -> str:
        return "\n".join(l.text for l in self.lines)


def build_page_units(data: bytes, pages: list) -> list:
    """PageResult 列表 → 带坐标的行单元。文本页用 PyMuPDF 取词坐标；
    OCR 页只有纯文本（tesseract image_to_string 无坐标），行=bbox=None。"""
    units: list[PageUnits] = []
    text_page_map: dict = {}
    pdf = None
    try:
        import pymupdf as fitz
        pdf = fitz.open(stream=data, filetype="pdf")
        for idx, page in enumerate(pdf):
            text_page_map[idx + 1] = page
    except Exception:
        pdf = None
    for p in pages:
        if p.mode == "text" and pdf is not None and p.page_no in text_page_map:
            mupdf_page = text_page_map[p.page_no]
            buckets: dict = {}
            for x0, y0, x1, y1, word, block_no, line_no, _w in mupdf_page.get_text("words"):
                buckets.setdefault((block_no, line_no), []).append(
                    (x0, y0, x1, y1, word))
            lines = []
            for words in buckets.values():
                text = " ".join(w[4] for w in words).strip()
                if text:
                    lines.append(Line(
                        text=text, page=p.page_no,
                        bbox=(min(w[0] for w in words), min(w[1] for w in words),
                              max(w[2] for w in words), max(w[3] for w in words)),
                        mode="text"))
            # 阅读顺序：按 y 再按 x
            lines.sort(key=lambda l: (round(l.bbox[1], 1), l.bbox[0]))
            units.append(PageUnits(p.page_no, "text", lines))
        else:
            lines = [Line(text=t, page=p.page_no, bbox=None, mode="ocr")
                     for t in (p.text or "").splitlines() if t.strip()]
            units.append(PageUnits(p.page_no, p.mode, lines))
    if pdf is not None:
        pdf.close()
    return units


# ---------------------------------------------------------------- 证据结构


def make_evidence(raw_text: str, value, page: int | None, region: str,
                  method: str, confidence: float, status: str,
                  note: str = "", bbox: tuple | None = None) -> dict:
    ev = {
        "raw_text": str(raw_text or "").strip(),
        "value": value,
        "page": page,
        "region": region,
        "method": method,
        "confidence": round(float(confidence), 2),
        "status": status,
        "note": note,
    }
    if bbox:
        ev["bbox"] = [round(float(v), 1) for v in bbox]
    return ev


STATUS_HIGH = "high"      # → 旧 field_confidence 口径
STATUS_REVIEW = "review"
STATUS_MISSING = "missing"


def confidence_tag(status: str) -> str:
    """四状态 → 旧三档置信度（兼容 IngestResult.field_confidence 消费方）。"""
    return {dc.FIELD_RECOGNIZED: STATUS_HIGH,
            dc.FIELD_NEEDS_REVIEW: STATUS_REVIEW,
            dc.FIELD_NOT_FOUND: STATUS_MISSING,
            dc.FIELD_BUSINESS_MISSING: STATUS_MISSING}.get(status, STATUS_MISSING)


# ---------------------------------------------------------------- 通用正则

# 数值：千分位逗号 或 纯数字，可带小数（\d{1,3}(,\d{3})+ | \d+）（修复 \d{1,3} 截断）
_NUMBER_RE = re.compile(r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")
# ISO6346集装箱号：4字母+7数字，且后续不能再紧跟数字（防吞箱型尺寸数字）
_CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})(?![0-9])")
_CURRENCY_TOKEN = re.compile(
    r"\b(CNY|RMB|USD|EUR|RUB|KZT|CHF|GBP|JPY)\b|(人民币|美元|欧元|卢布|坚戈)")
# 行首栏位号（"18 " / "18." / 栏位格左上角独立数字行）
_GRAPHA_PREFIX_RE = re.compile(r"^\s*(\d{1,2})[.\s]+")


def parse_num(text: str):
    try:
        value = float(text.replace(",", ""))
        return int(value) if value == int(value) else value
    except ValueError:
        return None


def strip_grapha(text: str) -> tuple[str, str | None]:
    """去掉行首栏位号，返回 (剩余文本, 栏位号)。栏位号本身不是字段值。"""
    m = _GRAPHA_PREFIX_RE.match(text)
    if m:
        return text[m.end():].strip(), m.group(1)
    return text, None


# ---------------------------------------------------------------- SMGS 适配器（任务书A3）
#
# SMGS（国际货协运单/Накладная СМГС）是栏位表格版式：每个业务字段有固定
# 栏位号（графа）与中俄双语标签。抽取依据 = 栏位号 + 多语言标签 + 坐标区域：
# 值优先取标签同行合理剩余；标签行无有效值（常见于栏位格：标签在上、值在下）
# 时取栏位格内下方续行。未对任何单一PDF硬编码。

SMGS_TYPE_KEYWORDS = ["НАКЛАДНАЯ СМГС", "SMGS NAKLADNAYA", "国际货协运单",
                      "货协运单", "СМГС NAKLADNAJA"]

# 栏位定义：字段 -> (栏位号列表, 标签片段[中/俄/英])
_SMGS_FIELDS = [
    ("consignor_name",      ["1"],    ["发货人", "Отправитель", "Sender", "Shipper", "Consignor"]),
    ("departure_station",   ["2"],    ["发站", "Станция отправления", "Станцияотправления",
                                       "Departure Station", "Station of Departure"]),
    ("consignee_name",      ["4"],    ["收货人", "Получатель", "Consignee", "Receiver"]),
    ("destination_station", ["5"],    ["到站", "Станция назначения", "Станцияназначения",
                                       "Destination Station", "Station of Destination"]),
    ("container_no",        ["7"],    ["集装箱", "Контейнер", "Container"]),
    ("goods_description",   ["15"],   ["货物名称", "Наименование груза", "Наименованиегруза",
                                       "Description of Goods"]),
    ("packing_type",        ["16"],   ["包装种类", "Вид упаковки", "Видупаковки",
                                       "Kind of Package", "Packing Type"]),
    ("total_packages",      ["17", "14"], ["件数", "Число мест", "Числомест",
                                           "Number of Packages", "No. of Packages"]),
    ("gross_weight_kg",     ["18", "13"], ["毛重", "Вес брутто", "Весбрутто",
                                           "Масса брутто", "Gross Weight"]),
    ("net_weight_kg",       ["18", "13"], ["净重", "Вес нетто", "Веснетто",
                                           "Масса нетто", "Net Weight"]),
    ("seal_no",             ["19"],   ["封印", "Пломба", "Пломбы", "Seal"]),
    ("carrier_segments",    ["22"],   ["承运人", "Перевозчик", "Carrier"]),
    ("attached_documents",  ["24"],   ["随附文件", "Приложенные документы",
                                       "Приложенныедокументы", "Attached Documents"]),
    ("waybill_no",          [],      ["运单号", "Номер накладной", "Номернакладной",
                                      "Waybill No"]),
    ("destination_country", [],      ["运抵国", "Страна назначения", "Странаназначения",
                                      "Destination Country"]),
    ("currency",            [],      ["币种", "Валюта", "Currency"]),
    ("cargo_value",         [],      ["货值", "Стоимость груза", "Стоимостьгруза",
                                      "Cargo Value", "Value of Goods"]),
]

_ALL_SMGS_LABEL_FRAGS = [frag for _f, _g, frags in _SMGS_FIELDS for frag in frags]


def _label_pattern(frags: list) -> re.Pattern:
    return re.compile("|".join(re.escape(f) for f in frags), re.IGNORECASE)


def _find_label_lines(units: list, frags: list):
    """返回 [(line, label_start, label_end, 栏位号)]：命中标签的行。
    标签匹配基于原文（含行首栏位号）做，保证 label_end 可直接切片取值；
    行首带栏位号的优先（更可信），按页码/纵坐标排序。"""
    pat = _label_pattern(frags)
    with_no, without = [], []
    for pu in units:
        for line in pu.lines:
            m = pat.search(line.text)
            if not m:
                continue
            _body, grapha = strip_grapha(line.text)
            hit = (line, m.start(), m.end())
            (with_no if grapha else without).append(hit)
    with_no.sort(key=lambda h: (h[0].page, h[0].bbox[1] if h[0].bbox else 0))
    without.sort(key=lambda h: (h[0].page, h[0].bbox[1] if h[0].bbox else 0))
    return with_no + without


_PAREN_ONLY = re.compile(r"^[（(].*[）)]$")
_LEGAL_FORM_ONLY = re.compile(
    r'^(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|КХ|Co\.?,?\s*Ltd\.?|Company|公司|株式会社)'
    r'[.\s"«»]*$', re.IGNORECASE)
_ADDRESS_HINT = re.compile(
    r"(УНП|ИНН|统一社会信用代码|邮编|Почтовый|индекс|тел\.|Телефон|fax|@|"
    r"г\.\s|ул\.|пр-т|проспект|улица|\.street|road|楼|室|号院)", re.IGNORECASE)


def _plausible_value(text: str) -> bool:
    """标签同行剩余是否像一个字段值（而非括注说明/另一语言标签/栏位号）。"""
    t = text.strip(" :\uFF1A\t\r\n/|、,，;；")
    if not t:
        return False
    if _PAREN_ONLY.match(t):
        return False                       # "(Наименование и адрес)"类括注
    all_labels = _label_pattern(_ALL_SMGS_LABEL_FRAGS)
    if all_labels.fullmatch(t):
        return False                       # 剩余本身就是标签（如"/ Валюта 币种"）
    if all_labels.match(t):
        return False                       # 剩余以另一语言的标签词开头（多语标签行）
    if re.fullmatch(r"\d{1,2}[.\s]*", t):
        return False                       # 栏位号本身
    return True


def _clean_tail(text: str) -> str:
    return text.strip(" :\uFF1A\t\r\n/|、,，;；")


_LEADING_PAREN_RE = re.compile(r"^[（(【][^）)】]*[）)】]\s*[:：]?\s*")


def strip_leading_paren(text: str) -> str:
    """去掉值文本开头的括注引导（如"（发货人）：山西洁康..." → "山西洁康..."）。"""
    if not isinstance(text, str):
        return text
    return _LEADING_PAREN_RE.sub("", text.strip(), count=1)


def _continuation_lines(units: list, line: Line, max_lines: int = 3) -> list:
    """标签行下方的续行（栏位格内）：坐标模式下按 纵向紧邻 + 横向相关 选取；
    OCR无坐标时退化为全页顺序的下一行。"""
    if line.bbox is None:
        flat = [l for pu in units for l in pu.lines]
        try:
            i = flat.index(line)
        except ValueError:
            return []
        return flat[i + 1:i + 1 + max_lines]
    x0, y0, x1, _ = line.bbox
    result = []
    for pu in units:
        for other in pu.lines:
            if other is line or other.bbox is None:
                continue
            ox0, oy0, ox1, _oy1 = other.bbox
            below = 0 < (oy0 - y0) < 60          # 同一栏位格内（紧邻下方）
            # 续行左端应落在标签行横向跨度内（同一栏位格，跨栏不混）
            in_column = (x0 - 8) <= ox0 <= (x1 + 10)
            if below and in_column:
                result.append(other)
    result.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
    return result[:max_lines]


def _is_label_like(text: str) -> bool:
    """文本是否本身是某个栏位标签行（续接应停止）。"""
    body, _ = strip_grapha(text)
    if _label_pattern(_ALL_SMGS_LABEL_FRAGS).search(body) and len(body) < 48:
        return True
    return False


def _clean_org_name(parts: list) -> tuple:
    """企业名称定稿：合并跨行名称；地址/税号线分离为附加信息。"""
    name_parts, extras = [], []
    for part in parts:
        text = _clean_tail(part)
        if not text:
            continue
        if _ADDRESS_HINT.search(text):
            extras.append(text)
            continue
        name_parts.append(text)
    return (" ".join(name_parts), extras)


def _box_value(units: list, hits: list, allow_multi: bool = False,
               stop_first_line: bool = True) -> tuple:
    """从标签命中行取值：同行合理剩余 → 栏位格内下方续行。
    返回 (value_text, 证据行, 页码, bbox, 候选列表)。"""
    for line, _s, _e in hits:
        same = _clean_tail(line.text[_e:])
        parts, cands = [], []
        if _plausible_value(same):
            parts.append(same)
            cands.append(same)
        conts = _continuation_lines(units, line)
        for cont in conts:
            if _is_label_like(cont.text):
                break
            body, _ = strip_grapha(cont.text)
            if not _plausible_value(body):
                continue
            parts.append(body)
            cands.append(body)
            if stop_first_line and not (allow_multi and _LEGAL_FORM_ONLY.match(parts[0])):
                break
        if parts:
            return " ".join(parts), line, line.page, line.bbox, cands
    return "", None, None, None, []


def _smgs_extract(units: list) -> tuple:
    """SMGS栏位抽取：返回 (fields, evidence, warnings)。"""
    fields: dict = {}
    evidence: dict = {}
    warnings: list = []
    all_lines = [l for pu in units for l in pu.lines]

    def region_label(grapha: list, zh: str) -> str:
        nos = "/".join(grapha) if grapha else "—"
        return f"СМГС栏位{nos}·{zh}"

    def frag_map(fname: str) -> tuple:
        for f, g, frs in _SMGS_FIELDS:
            if f == fname:
                return g, frs
        return [], []

    # ---- 标签类字段：名称/站名/描述/封印/承运人/随附文件/运抵国等
    label_fields = {
        "consignor_name": ("发货人", True), "consignee_name": ("收货人", True),
        "goods_description": ("货物名称", False),
        "departure_station": ("发站", False), "destination_station": ("到站", False),
        "packing_type": ("包装种类", False), "seal_no": ("封印", False),
        "carrier_segments": ("承运人", False),
        "attached_documents": ("随附文件", False),
        "waybill_no": ("运单号", False),
        "destination_country": ("运抵国", False),
    }
    for fname, (zh, allow_multi) in label_fields.items():
        grapha, frags = frag_map(fname)
        hits = _find_label_lines(units, frags)
        if not hits:
            continue
        value, line, page, bbox, cands = _box_value(units, hits, allow_multi=allow_multi)
        if not value:
            continue
        note = ""
        value = strip_leading_paren(value)
        if fname in ("consignor_name", "consignee_name"):
            value, extras = _clean_org_name([value])
            if extras:
                note = "地址/税号等信息未并入名称：" + "；".join(extras)
        if fname not in ("consignor_name", "consignee_name"):
            # 非名称字段同样剥离值开头的标签括注
            value = strip_leading_paren(value)
        if fname == "seal_no":
            # 封印号定稿为数值型编号（数量/单位留在证据原文里）
            nums = _NUMBER_RE.findall(value)
            if nums:
                value = nums[0]
        status, conf = dc.FIELD_RECOGNIZED, 0.85
        if fname in ("consignor_name", "consignee_name") and _LEGAL_FORM_ONLY.match(value):
            status, conf, note = dc.FIELD_NEEDS_REVIEW, 0.35, \
                "仅识别到企业法律形态词，名称不完整，请人工确认"
        fields[fname] = value
        evidence[fname] = make_evidence(
            cands[0] if cands else value, value, page, region_label(grapha, zh),
            "栏位号+多语言标签" if strip_grapha(line.text)[1] else "多语言标签（栏位格取值）",
            conf, status, note, bbox)
        if note and status == dc.FIELD_RECOGNIZED:
            warnings.append(f"{dc.FIELD_LABELS_ZH.get(fname, fname)}：{note}")

    # ---- 集装箱号：ISO6346 箱号全文检索（优先栏位7区域行）
    container_line = None
    for line in all_lines:
        if _CONTAINER_RE.search(line.text.upper()):
            container_line = line
            if any(_label_pattern(frags).search(line.text) for frags in
                   (["集装箱", "Контейнер", "Container"],)):
                break
    if container_line is not None:
        m = _CONTAINER_RE.search(container_line.text.upper())
        fields["container_no"] = m.group(1)
        evidence["container_no"] = make_evidence(
            container_line.text, m.group(1), container_line.page,
            region_label(["7"], "集装箱"), "ISO6346箱号模式", 0.95,
            dc.FIELD_RECOGNIZED, "", container_line.bbox)

    # ---- 件数：栏位17/14标签 → 栏位格内数值（栏位号不计入候选）
    grapha, frags = frag_map("total_packages")
    hits = _find_label_lines(units, frags)
    for line, _s, _e in hits:
        nums = _NUMBER_RE.findall(strip_grapha(line.text)[0])
        if not nums:
            for cont in _continuation_lines(units, line):
                if _is_label_like(cont.text):
                    break
                nums = _NUMBER_RE.findall(cont.text)
                if nums:
                    line = cont
                    break
        if nums:
            value = parse_num(nums[-1])
            if value is None:
                continue
            fields["total_packages"] = value
            evidence["total_packages"] = make_evidence(
                line.text, value, line.page, region_label(grapha, "件数"),
                "栏位号+标签数值", 0.9, dc.FIELD_RECOGNIZED,
                f"候选数字：{'、'.join(nums)}" if len(nums) > 1 else "", line.bbox)
            break

    # ---- 重量：多重量必须留证据；无法证明毛/净口径的进待确认（任务书A3/A4）
    _collect_weights(units, fields, evidence, warnings)

    # ---- 币种与货值
    _collect_currency_value(units, fields, evidence)

    return fields, evidence, warnings


_WEIGHT_GROSS_FRAGS = ["毛重", "Вес брутто", "Весбрутто", "Масса брутто", "Gross Weight"]
_WEIGHT_NET_FRAGS = ["净重", "Вес нетто", "Веснетто", "Масса нетто", "Net Weight"]
_WEIGHT_ANY_FRAGS = ["重量", "Вес", "Масса", "Weight"]


def _collect_weights(units: list, fields: dict, evidence: dict, warnings: list) -> None:
    """重量候选收集：每个候选保留来源行原文；带毛/净标签的直接定稿；
    无口径标签的不静默判定——单值按栏位惯例暂记毛重并转待确认，多值只列候选。"""
    candidates = []          # (kind, value, raw, page, line)
    for pu in units:
        for line in pu.lines:
            body, _ = strip_grapha(line.text)
            nums = _NUMBER_RE.findall(body)
            if not nums:
                continue
            is_gross = any(_label_pattern([f]).search(body) for f in _WEIGHT_GROSS_FRAGS)
            is_net = any(_label_pattern([f]).search(body) for f in _WEIGHT_NET_FRAGS)
            is_weight = (is_gross or is_net
                         or any(_label_pattern([f]).search(body) for f in _WEIGHT_ANY_FRAGS))
            if not is_weight:
                continue
            for num in nums:
                value = parse_num(num)
                if value is None:
                    continue
                if is_gross:
                    candidates.append(("gross", value, line.text, line.page, line))
                elif is_net:
                    candidates.append(("net", value, line.text, line.page, line))
                else:
                    candidates.append(("any", value, line.text, line.page, line))

    gross = [c for c in candidates if c[0] == "gross"]
    net = [c for c in candidates if c[0] == "net"]
    any_ = [c for c in candidates if c[0] == "any"]

    if gross:
        _kind, value, raw, page, line = gross[0]
        fields["gross_weight_kg"] = value
        evidence["gross_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·毛重", "栏位标签（брутто/毛重）",
            0.92, dc.FIELD_RECOGNIZED,
            f"页内毛重口径数值：{'、'.join(str(c[1]) for c in gross)}" if len(gross) > 1 else "",
            line.bbox)
    elif any_:
        # 栏位18的"Вес/Масса"未标注毛/净：按运单惯例暂记毛重，但口径不可证明 → 待确认
        _kind, value, raw, page, line = any_[0]
        fields["gross_weight_kg"] = value
        others = "；".join(f"「{c[2]}」" for c in any_[1:])
        evidence["gross_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·重量", "栏位标签（未标明毛/净）",
            0.4, dc.FIELD_NEEDS_REVIEW,
            "重量栏位未标明毛重/净重口径，按栏位惯例暂记为毛重，请人工确认"
            + (f"；页内其他重量候选：{others}" if others else ""), line.bbox)
        warnings.append("货物重量：栏位未标明毛/净口径，已进入待人工确认（未静默判定）")

    if net:
        _kind, value, raw, page, line = net[0]
        fields["net_weight_kg"] = value
        evidence["net_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·净重", "栏位标签（нетто/净重）",
            0.9, dc.FIELD_RECOGNIZED, "", line.bbox)
    elif any_ and len(any_) >= 2 and "gross_weight_kg" in fields:
        raws = "；".join(f"「{c[2]}」" for c in any_)
        warnings.append(f"页内存在多个未标明口径的重量值：{raws}；净重未判定，请人工确认")


def _collect_currency_value(units: list, fields: dict, evidence: dict) -> None:
    """货值+币种：货值标签行的 数值+币种 联合判定。"""
    frags = ["货值", "Стоимость груза", "Стоимостьгруза",
             "Cargo Value", "Value of Goods"]
    hits = _find_label_lines(units, frags)
    for line, _s, _e in hits:
        same = _clean_tail(line.text[_e:])
        cur = _CURRENCY_TOKEN.search(same)
        nums = _NUMBER_RE.findall(same)
        if not (cur and nums):
            for cont in _continuation_lines(units, line):
                if _is_label_like(cont.text):
                    break
                nums = nums or _NUMBER_RE.findall(cont.text)
                cur = cur or _CURRENCY_TOKEN.search(cont.text)
                if cur and nums:
                    line = cont
                    same = cont.text
                    break
        if cur and nums:
            value = parse_num(nums[-1])
            currency = cur.group(1) or cur.group(2)
            if value is None:
                continue
            fields["cargo_value"] = value
            evidence["cargo_value"] = make_evidence(
                same, value, line.page, "СМГС·货值栏", "标签+数值+币种",
                0.85, dc.FIELD_RECOGNIZED, "", line.bbox)
            fields["currency"] = currency
            evidence["currency"] = make_evidence(
                same, currency, line.page, "СМГС·货值栏", "标签+币种",
                0.8, dc.FIELD_RECOGNIZED, "", line.bbox)
            return
    # 币种兜底：币种标签行
    _g, cur_frags = [], ["币种", "Валюта", "Currency"]
    for line, _s, _e in _find_label_lines(units, cur_frags):
        rest = _clean_tail(line.text[_e:])
        cur = _CURRENCY_TOKEN.search(rest)
        if cur:
            currency = cur.group(1) or cur.group(2)
            fields["currency"] = currency
            evidence["currency"] = make_evidence(
                line.text, currency, line.page, "СМГС·币种栏", "标签+币种",
                0.8, dc.FIELD_RECOGNIZED, "", line.bbox)
            return


# ---------------------------------------------------------------- 通用适配器（任务书A5）

# 各类型通用同义标签（中/英/俄）：标签独立成行时取下一行为值（跨行），
# 弥补旧正则"标签后必须同行有值"的盲区。SMGS优先走上面的栏位适配器。
GENERIC_LABELS = {
    "consignor_name": ["发货人", "发件人", "售方", "SELLER", "SHIPPER", "Consignor",
                       "Отправитель"],
    "consignee_name": ["收货人", "收件人", "买方", "BUYER", "CONSIGNEE", "Receiver",
                       "Получатель"],
    "goods_description": ["货物描述", "品名", "DESCRIPTION OF GOODS", "Goods"],
    "total_packages": ["件数", "箱数", "TOTAL PACKAGES", "Number of Packages"],
    "gross_weight_kg": ["毛重", "GROSS WEIGHT", "Вес брутто"],
    "net_weight_kg": ["净重", "NET WEIGHT", "Вес нетто"],
    "container_no": ["集装箱号", "箱号", "CONTAINER NO", "Container"],
    "seal_no": ["封印号", "铅封号", "封志", "SEAL NO", "Пломба"],
    "packing_type": ["包装种类", "包装方式", "PACKING TYPE", "Kind of Package"],
}


def _generic_label_extract(units: list, doc_type: str) -> tuple:
    """通用标签抽取：标签行有合理同行值取同行；否则取下一行。
    返回 (fields, evidence)。"""
    fields: dict = {}
    evidence: dict = {}
    lines = [l for pu in units for l in pu.lines]
    for fname, frags in GENERIC_LABELS.items():
        pat = _label_pattern(frags)
        for i, line in enumerate(lines):
            m = pat.search(line.text)
            if not m:
                continue
            rest = _clean_tail(line.text[m.end():])
            if _plausible_value(rest):
                raw, value, page, bbox = rest, rest, line.page, line.bbox
            elif i + 1 < len(lines) and not _is_label_like(lines[i + 1].text):
                nxt = lines[i + 1].text.strip()
                if not _plausible_value(nxt):
                    continue
                raw, value, page, bbox = nxt, nxt, lines[i + 1].page, lines[i + 1].bbox
            else:
                continue
            raw, value = strip_leading_paren(raw), strip_leading_paren(value) \
                if isinstance(value, str) else value
            if fname in dc.NUMERIC_FIELDS or fname == "total_packages":
                nums = _NUMBER_RE.findall(raw)
                if not nums:
                    continue
                value = parse_num(nums[-1])
                if value is None:
                    continue
            if fname == "container_no":
                cm = _CONTAINER_RE.search(raw.upper())
                if not cm:
                    continue
                value = cm.group(1)
            fields[fname] = value
            evidence[fname] = make_evidence(
                raw, value, page, f"{dc.DOC_TYPE_LABELS.get(doc_type, doc_type)}·标签「{m.group(0)}」",
                "多语言标签（跨行取值）", 0.65, dc.FIELD_RECOGNIZED, "", bbox)
            break
    return fields, evidence


# ---------------------------------------------------------------- 汇总入口


@dataclass
class ExtractOutcome:
    fields: dict = dc_field(default_factory=dict)
    evidence: dict = dc_field(default_factory=dict)     # field -> evidence dict
    warnings: list = dc_field(default_factory=list)
    confidence: dict = dc_field(default_factory=dict)   # 旧三档口径（兼容）

    def finalize(self, doc_type: str):
        """补齐四状态：必填字段未命中 → not_found（绝不 business_missing）；
        生成旧口径 field_confidence。"""
        required = dc.REQUIRED_FIELDS.get(doc_type, [])
        for f in required:
            if f not in self.evidence:
                self.evidence[f] = make_evidence(
                    "", None, None, "—", "未找到候选", 0.0, dc.FIELD_NOT_FOUND,
                    "OCR/文本/版式规则均未找到候选值；可能是未识别到，不代表单据业务缺失")
        for f, ev in self.evidence.items():
            self.confidence[f] = confidence_tag(ev["status"])
        return self


def extract_document_fields(doc_type: str, units: list,
                            legacy_fallback=None) -> ExtractOutcome:
    """类型先行抽取主入口。legacy_fallback 为 pdf_ingest.FIELD_RULES 的
    正则兜底函数（(doc_type, page_texts) -> (fields, confidence, warnings)），
    由调用方注入以避免循环依赖。"""
    outcome = ExtractOutcome()
    page_texts = [pu.all_text() for pu in units]

    if doc_type == "smgs_rail_waybill":
        fields, evidence, warnings = _smgs_extract(units)
        outcome.fields, outcome.evidence, outcome.warnings = fields, evidence, warnings
    else:
        fields, evidence = _generic_label_extract(units, doc_type)
        outcome.fields, outcome.evidence = fields, evidence

    # 旧正则兜底：只补未命中字段（保历史识别能力，任务书A5）
    if legacy_fallback is not None:
        try:
            lf_fields, _lf_conf, lf_warn = legacy_fallback(doc_type, page_texts)
        except Exception:
            lf_fields, lf_warn = {}, []
        for f, v in lf_fields.items():
            if f not in outcome.fields and v not in (None, ""):
                outcome.fields[f] = v
                outcome.evidence[f] = make_evidence(
                    v, v, None, dc.DOC_TYPE_LABELS.get(doc_type, doc_type),
                    "通用兜底正则", 0.6, dc.FIELD_RECOGNIZED, "")
        outcome.warnings.extend(lf_warn)

    return outcome.finalize(doc_type)
