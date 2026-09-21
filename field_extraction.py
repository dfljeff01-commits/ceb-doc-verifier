# -*- coding: utf-8 -*-
"""
版式感知字段抽取层 v2（P0任务书A1/A3/A5 + 真实文件回归修正）。

定位：在 pdf_ingest 的"文字提取（文本直取/OCR）"之上、字段定稿之前，提供
基于 版面坐标 + 栏位号 + 多语言标签 的候选抽取，并给每个字段产出**证据**
（原文/页码/来源栏位/坐标/方法/置信度/四状态），供页面、规则引擎与报告
复核——"识别"与"业务"自此分离：抽取不到 ≠ 单据缺项。

v2（真实СМГС运单版式回归后修订）：
  - 栏位格取值：真实表单的标签与值分处不同文本块；按"标签行下方、同列"
    收集格内续行，跳过无编号子标签（签字—Подпись/数量/记号等），
    在下一编号栏位标签行处截断；
  - 企业名称：首行前导token（中文公司名）与 法律形态正则（ООО/OOO/LLC…）
    双路候选，优先中文公司名，否则注册名口径；地址/税号/电话分离为附加
    信息，不并入名称；收货人完整多词俄文名称不得截断；
  - 站名：剥离承运人前缀（"中铁(КЖД)/"），截断俄文重复尾巴；
  - 货名：截断俄文翻译尾巴（保留在证据原文），HS编码行单独成字段；
  - 重量：栏位18格内数值按СМГС口径作为毛重（置信度0.8+说明）；
    净重必须有"净/нетто"标签才recognized，否则 needs_review（列出候选，
    不静默判定、不因任务书基准值强行识别）。

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

# 数值：千分位逗号 或 纯数字，可带小数
_NUMBER_RE = re.compile(r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")
# ISO6346集装箱号：4字母+7数字，且后续不能再紧跟数字（防吞箱型尺寸数字）
_CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})(?![0-9])")
_CURRENCY_TOKEN = re.compile(
    r"\b(CNY|RMB|USD|EUR|RUB|KZT|CHF|GBP|JPY)\b|(人民币|美元|欧元|卢布|坚戈)")
# 行首栏位号（"18 " / "18." / 栏位格左上角独立数字行）
_GRAPHA_PREFIX_RE = re.compile(r"^\s*(\d{1,2})[.\s]+")
# HS编码行（SMGS货物栏常见附属行）
_HS_CODE_RE = re.compile(r"HS\s*CODE\s*[:：]?\s*(\d{6,10})", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[А-ЯЁа-яё]")


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


def cut_cyrillic_tail(text: str) -> tuple[str, str]:
    """截断尾部俄文（中俄双语字段取中英口径），返回 (主值, 俄文尾巴)。"""
    m = _CYRILLIC_RE.search(text)
    if m:
        return text[:m.start()].strip(" —_/，,;；"), text[m.start():].strip()
    return text.strip(), ""


# ---------------------------------------------------------------- SMGS 适配器（任务书A3）
#
# SMGS（国际货协运单/Накладная СМГС）是栏位表格版式：每个业务字段有固定
# 栏位号（графа）与中俄双语标签。抽取依据 = 栏位号 + 多语言标签 + 栏位格
# 坐标区域，未对任何单一PDF硬编码。真实表单里标签与值常分处不同文本块，
# 且格内混有无编号子标签（签字/数量/记号等），统一由 _box_lines 处理。

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
    ("container_no",        ["7"],    ["集装箱", "Контейнер", "Container", "车辆", "Вагон"]),
    ("goods_description",   ["15"],   ["货物名称", "Наименование груза", "Наименованиегруза",
                                       "Description of Goods"]),
    ("packing_type",        ["16"],   ["包装种类", "Вид упаковки", "Видупаковки",
                                       "Род упаковки", "Родупаковки",
                                       "Kind of Package", "Packing Type"]),
    ("total_packages",      ["17", "14"], ["件数", "Число мест", "Числомест",
                                           "Number of Packages", "No. of Packages"]),
    ("gross_weight_kg",     ["18", "13"], ["毛重", "Вес брутто", "Весбрутто",
                                           "Масса брутто", "Gross Weight"]),
    ("net_weight_kg",       ["18", "13"], ["净重", "Вес нетто", "Веснетто",
                                           "Масса нетто", "Net Weight"]),
    ("seal_no",             ["19"],   ["封印", "Пломба", "Пломбы", "Seal"]),
    ("carrier_segments",    ["22"],   ["承运人", "Перевозчик", "Carrier"]),
    ("attached_documents",  ["24"],   ["随附文件", "添附的文件", "Приложенные документы",
                                       "Приложенныедокументы", "Attached Documents",
                                       "Документы"]),
    ("waybill_no",          [],      ["运单号", "Номер накладной", "Номернакладной",
                                      "Waybill No"]),
    # 注意：不含"Страна назначения"——与栏位5到站（Станция назначения）过于接近
    ("destination_country", [],      ["运抵国", "到达国家", "Destination Country"]),
    ("currency",            [],      ["币种", "Валюта", "Currency"]),
    ("cargo_value",         [],      ["货值", "货物价值", "Стоимость груза", "Стоимостьгруза",
                                      "Cargo Value", "Value of Goods"]),
]

_ALL_SMGS_LABEL_FRAGS = [frag for _f, _g, frags in _SMGS_FIELDS for frag in frags]

# 栏位格内的无编号子标签/表头（不是值，跳过；也不是其他栏位起点，不截断）
_SMGS_BOX_STOP_MARKS = ["签字", "Подпись", "数量", "记号", "знаки", "К-во",
                        "车站代码", "коды станций", "换装后", "После перегрузки",
                        "Масса груза", "К-во мест", "Пограничные станции",
                        "Масса (в кг)", "Род упаковки", "缔约承运人",
                        "Договорный перевозчик", "Оригинал накладной"]

_SMGS_SUB_LABEL_WORDS = ["签字", "Подпись", "缔约承运人", "Договорный перевозчик",
                         "数量", "记号", "знаки", "К-во", "车站代码",
                         "换装后", "После перегрузки"]


def _label_pattern(frags: list) -> re.Pattern:
    return re.compile("|".join(re.escape(f) for f in frags), re.IGNORECASE)


def _find_label_lines(units: list, frags: list):
    """返回 [(line, label_start, label_end)]：命中标签的行。
    标签匹配基于原文（含行首栏位号）做，保证 label_end 可直接切片取值；
    行首带栏位号的优先（更可信），按页码/纵坐标排序。"""
    pat = _label_pattern(frags)
    # 先收集裸栏位号行（如单独一行"22"，其后无空格/标点），供相邻无编号标签行继承
    bare_numbers = []
    for pu in units:
        for line in pu.lines:
            text = line.text.strip()
            if re.fullmatch(r"\d{1,2}", text):
                bare_numbers.append((line, text))

    def inherit_grapha(line: Line) -> str | None:
        _body, grapha = strip_grapha(line.text)
        if grapha:
            return grapha
        if line.bbox is None:
            return None
        for num_line, num in bare_numbers:
            if num_line is line or num_line.bbox is None or num_line.page != line.page:
                continue
            dy = abs(num_line.bbox[1] - line.bbox[1])
            dx = abs(num_line.bbox[0] - line.bbox[0])
            if dy < 8 and dx < 60:
                return num
        return None

    with_no, without = [], []
    for pu in units:
        for line in pu.lines:
            m = pat.search(line.text)
            if not m:
                continue
            grapha = inherit_grapha(line)
            hit = (line, m.start(), m.end())
            (with_no if grapha else without).append(hit)
    with_no.sort(key=lambda h: (h[0].page, h[0].bbox[1] if h[0].bbox else 0))
    without.sort(key=lambda h: (h[0].page, h[0].bbox[1] if h[0].bbox else 0))
    return with_no + without


_PAREN_ONLY = re.compile(r"^[（(【][^）)】]*[）)】]$")
_LEGAL_FORM_ONLY = re.compile(
    r'^(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|КХ|Co\.?,?\s*Ltd\.?|Company|公司|株式会社)'
    r'[.\s"«»]*$', re.IGNORECASE)
_ADDRESS_HINT = re.compile(
    r"(УНП|ИНН|ОКПО|ТГНЛ|统一社会信用代码|邮编|Почтовый|индекс|тел\.|Телефон|fax|@|"
    r"г\.\s|ул\.|пр-т|проспект|улица|\.street|road|楼|室|号院|省|市|区|县|街道|开发区|"
    r"region|city|province|область|город|район)", re.IGNORECASE)


def _plausible_value(text: str) -> bool:
    """标签同行剩余是否像一个字段值（而非括注说明/另一语言标签/栏位号）。
    以括注开头的文本（如"(毛重): 12300 KG"）先剥掉引导括注再判断——
    剥后仍有实质内容则视为值（调用方会用 strip_leading_paren 清理）。"""
    t = text.strip(" :\uFF1A\t\r\n/|、,，;；")
    if not t:
        return False
    if t.startswith(("（", "(", "【")):
        t2 = _LEADING_PAREN_RE.sub("", t, count=1).strip(" :\uFF1A")
        if not t2 or t2.startswith(("（", "(", "【")) or _PAREN_ONLY.match(t2):
            return False                   # 纯括注（一层或多层）→ 不是值
        t = t2
    if _PAREN_ONLY.match(t):
        return False                       # "(Наименование и адрес)"类括注
    all_labels = _label_pattern(_ALL_SMGS_LABEL_FRAGS)
    if all_labels.fullmatch(t):
        return False                       # 剩余本身就是标签（如"/ Валюта 币种"）
    if all_labels.search(t) and len(t) <= 45:
        return False                       # 短文本且含标签词（多语标签行/子标签）
    if re.fullmatch(r"\d{1,2}[.\s]*", t):
        return False                       # 栏位号本身
    return True


def _clean_tail(text: str) -> str:
    # 前导破折号一并剥离：真实表单标签俄文尾巴形如"—Отправитель"
    return text.strip(" :\uFF1A\t\r\n/|、,，;；").lstrip("—-").strip()


_LEADING_PAREN_RE = re.compile(r"^[（(【][^）)】]*[）)】]\s*[:：]?\s*")


def strip_leading_paren(text: str) -> str:
    """去掉值文本开头的括注引导（如"（发货人）：山西洁康..." → "山西洁康..."）。"""
    if not isinstance(text, str):
        return text
    return _LEADING_PAREN_RE.sub("", text.strip(), count=1)


def _is_other_grapha_label(line: Line) -> bool:
    """是否为其他栏位的编号标签行（值收集在此截断）。"""
    body, grapha = strip_grapha(line.text)
    if grapha and _label_pattern(_ALL_SMGS_LABEL_FRAGS).search(body):
        return True
    return False


def _is_box_noise(line: Line) -> bool:
    """栏位格内的无编号子标签/表头（签字—Подпись/数量/记号等）：跳过不收集。"""
    body, grapha = strip_grapha(line.text)
    if grapha:
        return False
    text = body
    for mark in _SMGS_BOX_STOP_MARKS:
        if mark in text:
            # 形如"签字—Подпись"的纯子标签：短且无数字
            if len(text) < 40 and not re.search(r"\d", text.replace("№", "")):
                return True
    return False


def _box_lines(units: list, line: Line, max_lines: int = 6,
               x_pad_right: int = 12) -> list:
    """标签行下方的栏位格续行（坐标：纵向紧邻+同列；OCR退化为顺序后几行）。
    遇到其他栏位的编号标签行即截断。x_pad_right 供多列栏位格（如栏位22的
    承运人|区段|代码三列）放宽右侧边界。"""
    if line.bbox is None:
        flat = [l for pu in units for l in pu.lines]
        try:
            i = flat.index(line)
        except ValueError:
            return []
        picked = []
        for other in flat[i + 1:]:
            if _is_other_grapha_label(other):
                break
            picked.append(other)
            if len(picked) >= max_lines:
                break
        return picked
    x0, y0, x1, _ = line.bbox
    result = []
    for pu in units:
        for other in pu.lines:
            if other is line or other.bbox is None:
                continue
            ox0, oy0, _ox1, _oy1 = other.bbox
            below = 0 < (oy0 - y0) < 90                    # 栏位格纵向范围
            in_column = (x0 - 26) <= ox0 <= (x1 + x_pad_right)
            if below and in_column:
                if _is_other_grapha_label(other):
                    break
                result.append(other)
            if len(result) >= max_lines:
                break
        if len(result) >= max_lines:
            break
    return result


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


# ---------------------------------------------------------------- 企业名/站名/货名规范化（v2）

# 法律形态 + 注册名（俄文/西里尔口径，兼容拉丁OOO字形）
_LEGAL_FORM_RE = re.compile(
    r"\b(ООО|OOO|ОАО|ЗАО|ПАО|АО|ИП)\s*[«\"“]?"
    r"([^,，;；\n»\"”]+?)[»\"”]?(?=\s*(?:[,，;；]|\d|$))", re.IGNORECASE)
_COMPANY_SUFFIX_RE = re.compile(r"(公司|厂|集团|中心|有限公司|株式会社|GmbH|LLC|Ltd)$",
                                re.IGNORECASE)


def _extract_org(block_texts: list) -> tuple:
    """从栏位格文本块提取企业注册名。返回 (value, note, raw_first)。
    双路候选：① 首行前导token（中文公司名，如"芜湖德菲图汽车技术有限公司"）；
    ② 法律形态正则（ООО/OOO + 注册名，如 OOO СТС-ЛОГИСТИКА）。地址/税号/
    电话不并入名称。优先 ①（有中文公司名时），否则 ②，否则首行原文。"""
    first = (block_texts[0] if block_texts else "").strip()
    raw_first = first

    # ① 首行前导 token（遇到含数字/地址暗示的 token 即止）
    good = []
    for token in first.split():
        if re.search(r"\d", token) or _ADDRESS_HINT.search(token):
            break
        good.append(token)
    candidate = " ".join(good).strip(" ,，;；")
    cjk_candidate = None
    if candidate and _COMPANY_SUFFIX_RE.search(candidate) and len(candidate) >= 5:
        cjk_candidate = candidate

    # ② 法律形态注册名
    legal = None
    legal_note = ""
    joined = "\n".join(block_texts)
    m = _LEGAL_FORM_RE.search(joined)
    if m:
        prefix, name = m.group(1), m.group(2).strip(" \t«»\"“”")
        if name:
            note_bits = []
            if prefix.upper() == "OOO" and _CYRILLIC_RE.search(name):
                prefix = "ООО"
                note_bits.append("原文前缀为拉丁OOO，已规范为西里尔ООО")
            legal = f'{prefix} "{name}"'
            if f'"{name}"' not in joined and f'«{name}»' not in joined:
                note_bits.append("原文未带引号，已按注册名口径规范化")
            legal_note = "；".join(note_bits)

    if cjk_candidate:
        extras = [t for t in block_texts[1:] if t.strip()]
        rest = first[len(cjk_candidate):].strip(" ,，;；")
        if rest:
            extras.insert(0, rest)
        return cjk_candidate, "；".join(e for e in extras if e)[:160], raw_first
    if legal:
        extras = []
        if legal_note:
            extras.append(legal_note)
        en = re.search(r"(?:LLC|Ltd|GmbH|Inc)[^,，;；\n]*", first)
        if en and en.group(0) not in legal:
            extras.append("英文名：" + en.group(0))
        return legal, "；".join(extras)[:160], raw_first
    return first, "", raw_first


def _normalize_station(text: str) -> tuple:
    """站名定稿：剥离承运人前缀（"中铁(КЖД)/"），截断俄文重复尾巴。
    返回 (value, note)。"""
    note_bits = []
    v = text.strip()
    if "/" in v:
        head, tail = v.split("/", 1)
        if _CYRILLIC_RE.search(head) or "铁" in head:
            note_bits.append(f"承运人前缀「{head.strip()}」已剥离")
            v = tail.strip()
    v, cyr_tail = cut_cyrillic_tail(v)
    if cyr_tail:
        note_bits.append(f"俄文原名「{cyr_tail}」")
    return v, "；".join(note_bits)


def _normalize_goods(block_texts: list) -> tuple:
    """货名定稿：取中英口径（截断俄文翻译尾巴），HS编码行单独成字段。
    返回 (goods_value, note, hs_code)。"""
    hs_code = None
    main = ""
    cyr_tail = ""
    for text in block_texts:
        hm = _HS_CODE_RE.search(text)
        if hm:
            hs_code = hm.group(1)
            continue
        if not main:
            main, cyr_tail = cut_cyrillic_tail(text)
    note = f"俄文名称「{cyr_tail}」" if cyr_tail else ""
    return main.strip(" —\\-/，,;；"), note, hs_code


# ---------------------------------------------------------------- 栏位格取值


def _box_value(units: list, hits: list, allow_multi: bool = False,
               x_pad_right: int = 12):
    """从标签命中行取值：同行合理剩余 + 栏位格续行（跳过子标签/纯编码行）。
    多值字段跳过纯编号行（承运人区段表里的车站代码不是承运人名）。
    返回 (value_text, 证据行, 页码, bbox, 候选列表)。"""
    for line, _s, _e in hits:
        parts, cands = [], []
        same = _clean_tail(line.text[_e:])
        if _plausible_value(same):
            parts.append(same)
            cands.append(same)
        for cont in _box_lines(units, line, x_pad_right=x_pad_right):
            body, _g = strip_grapha(cont.text)
            body = body.strip()
            if not body:
                continue
            if _is_box_noise(cont):
                continue
            if allow_multi and re.fullmatch(r"\d{3,8}", body):
                continue              # 纯编号行（车站代码/邮编等）不是文本值
            if not _plausible_value(body):
                continue
            parts.append(body)
            cands.append(body)
            if not allow_multi:
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

    # ---- 标签类字段
    label_fields = {
        "consignor_name": ("发货人", True), "consignee_name": ("收货人", True),
        "goods_description": ("货物名称", True),
        "departure_station": ("发站", False), "destination_station": ("到站", False),
        "destination_country": ("运抵国", False),
        "packing_type": ("包装种类", False), "seal_no": ("封印", False),
        "carrier_segments": ("承运人", True),
        "attached_documents": ("随附文件", True),
        "waybill_no": ("运单号", False),
    }
    for fname, (zh, allow_multi) in label_fields.items():
        grapha, frags = frag_map(fname)
        hits = _find_label_lines(units, frags)
        if not hits:
            continue
        # 栏位22是多列表格（承运人|区段|车站代码），放宽右界以纳入区段站名
        x_pad = 150 if fname == "carrier_segments" else 12
        value, line, page, bbox, cands = _box_value(
            units, hits, allow_multi=allow_multi, x_pad_right=x_pad)
        if not value:
            continue
        note = ""
        if fname in ("consignor_name", "consignee_name"):
            value, note, raw_first = _extract_org(cands or [value])
        elif fname in ("departure_station", "destination_station"):
            value, station_note = _normalize_station(value)
            note = station_note
        elif fname == "goods_description":
            value, goods_note, hs = _normalize_goods(cands or [value])
            note = goods_note
            if hs:
                fields["hs_code"] = hs
                evidence["hs_code"] = make_evidence(
                    " ".join(cands), hs, page, region_label(grapha, "货物名称附属行"),
                    "HS CODE行", 0.9, dc.FIELD_RECOGNIZED, "", bbox)
        elif fname == "seal_no":
            nums = _NUMBER_RE.findall(value)
            if nums:
                value = nums[0]
        if not value:
            continue
        status, conf = dc.FIELD_RECOGNIZED, 0.85
        if fname in ("consignor_name", "consignee_name") and _LEGAL_FORM_ONLY.match(value):
            status, conf = dc.FIELD_NEEDS_REVIEW, 0.35
            note = (note + "；" if note else "") + "仅识别到企业法律形态词，名称不完整，请人工确认"
        fields[fname] = value
        evidence[fname] = make_evidence(
            cands[0] if cands else value, value, page, region_label(grapha, zh),
            "栏位号+多语言标签" if strip_grapha(line.text)[1] else "多语言标签（栏位格取值）",
            conf, status, note, bbox)
        if note and status == dc.FIELD_RECOGNIZED and fname in (
                "consignor_name", "consignee_name"):
            warnings.append(f"{dc.FIELD_LABELS_ZH.get(fname, fname)}：{note}")

    # ---- 集装箱号：ISO6346 全文检索（优先含集装箱/车辆标签的行）
    container_line = None
    for line in all_lines:
        if _CONTAINER_RE.search(line.text.upper()):
            container_line = line
            if _label_pattern(["集装箱", "Контейнер", "Container", "车辆", "Вагон"]) \
                    .search(line.text):
                break
    if container_line is not None:
        m = _CONTAINER_RE.search(container_line.text.upper())
        extra_type = ""
        tail = _CONTAINER_RE.sub("", container_line.text.upper()).strip(" \t/,-")
        tail_m = re.search(r"\b(\d{1,2}[A-Z]\d)\b", tail)
        if tail_m:
            extra_type = tail_m.group(1)
        else:
            # 箱型/尺寸常与箱号分处相邻文本块：检索同一行带（Δy<6）右侧内容
            if container_line.bbox is not None:
                cx0, cy0, cx1, _ = container_line.bbox
                for line2 in all_lines:
                    if line2 is container_line or line2.bbox is None:
                        continue
                    if (line2.page == container_line.page
                            and abs(line2.bbox[1] - cy0) < 6
                            and line2.bbox[0] >= cx1):
                        tm = re.search(r"\b(\d{1,2}[A-Z]\d)\b",
                                       line2.text.upper())
                        if tm:
                            extra_type = tm.group(1)
                            break
        fields["container_no"] = m.group(1)
        evidence["container_no"] = make_evidence(
            container_line.text, m.group(1), container_line.page,
            region_label(["7"], "集装箱"), "ISO6346箱号模式", 0.95,
            dc.FIELD_RECOGNIZED,
            f"箱型/尺寸 {extra_type}" if extra_type else "", container_line.bbox)

    # ---- 件数：栏位17/14标签 → 栏位格内数值（栏位号不计入候选）
    grapha, frags = frag_map("total_packages")
    for line, _s, _e in _find_label_lines(units, frags):
        nums = _NUMBER_RE.findall(_clean_tail(line.text[_e:]))
        if not nums:
            for cont in _box_lines(units, line, max_lines=3):
                if _is_box_noise(cont):
                    continue
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

    # ---- 重量（v2：栏位18格内数值=毛重口径；净重必须有标签）
    _collect_weights(units, fields, evidence, warnings)

    # ---- 币种与货值
    _collect_currency_value(units, fields, evidence)

    return fields, evidence, warnings


_WEIGHT_GROSS_FRAGS = ["毛重", "Вес брутто", "Весбрутто", "Масса брутто", "Gross Weight"]
_WEIGHT_NET_FRAGS = ["净重", "Вес нетто", "Веснетто", "Масса нетто", "Net Weight"]
_WEIGHT_ANY_FRAGS = ["重量", "Вес", "Масса", "Weight"]


def _weight_box_numbers(units: list, frags: list,
                        allowed_grapha: set | None = None) -> tuple:
    """栏位18/13标签行自身及其格内数值，带各自行的毛/净标签判定。
    allowed_grapha 限定栏位号（"8 车辆…9 载重量"等邻栏不得混入）。
    返回 (labeled_gross, labeled_net, unlabeled)——均为 (值, 原文, 页, 行) 列表。"""
    labeled_gross, labeled_net, unlabeled = [], [], []
    seen_ids = set()
    hits = _find_label_lines(units, frags)
    if allowed_grapha:
        hits = [h for h in hits
                if (strip_grapha(h[0].text)[1] or "") in allowed_grapha]
    for line, _s, _e in hits:
        scan = [line] + _box_lines(units, line, max_lines=8)
        for ln in scan:
            if id(ln) in seen_ids:
                continue
            seen_ids.add(id(ln))
            is_label_line = ln is line
            # 标签行自身的栏位号不是数值（"13 货物重量"的13是栏位号）
            body = strip_grapha(ln.text)[0] if is_label_line else ln.text
            nums = _NUMBER_RE.findall(body)
            if not nums:
                continue
            is_gross = any(_label_pattern([f]).search(body) for f in _WEIGHT_GROSS_FRAGS)
            is_net = any(_label_pattern([f]).search(body) for f in _WEIGHT_NET_FRAGS)
            is_weight = (is_gross or is_net
                         or any(_label_pattern([f]).search(body)
                                for f in _WEIGHT_ANY_FRAGS)
                         or ln is not line)      # 栏位18格内的裸数值行
            if not is_weight:
                continue
            for num in nums:
                value = parse_num(num)
                if value is None:
                    continue
                if not is_gross and not is_net and ln is not line:
                    # 栏位格内的裸数值须像重量（排除封印数量"1"等串入）
                    if not (30 <= value < 100000):
                        continue
                item = (value, body, ln.page, ln)
                if is_gross:
                    labeled_gross.append(item)
                elif is_net:
                    labeled_net.append(item)
                else:
                    unlabeled.append(item)
    return labeled_gross, labeled_net, unlabeled


def _page_kg_numbers(units: list, exclude_values: set) -> list:
    """页面其他位置的重量数值（带kg单位，或紧邻kg单位行）：净重候选证据。"""
    result = []
    all_lines = [l for pu in units for l in pu.lines]
    kg_lines = [l for l in all_lines
                if re.search(r"\b(kgs?|кг|公斤|千克)\b", l.text, re.IGNORECASE)]
    kg_ys = [(l.page, l.bbox[1] if l.bbox else -1) for l in kg_lines
             if l.bbox is not None]
    for l in all_lines:
        _body, grapha = strip_grapha(l.text)
        if grapha and _label_pattern(_ALL_SMGS_LABEL_FRAGS).search(_body):
            continue                      # 栏位标签行（如"19 封印—Пломбы"）不是重量
        for num in _NUMBER_RE.findall(l.text):
            value = parse_num(num)
            if value is None or value in exclude_values or not (30 <= value < 100000):
                continue                  # 排除栏位号/6位站码等非重量数字
            near_kg = bool(re.search(r"\b(kgs?|кг|公斤|千克)\b", l.text,
                                     re.IGNORECASE))
            if not near_kg and l.bbox is not None:
                near_kg = any(p == l.page and abs(l.bbox[1] - y) <= 14
                              for p, y in kg_ys)
            if near_kg:
                result.append((value, l.text.strip(), l.page, l))
    return result


def _collect_weights(units: list, fields: dict, evidence: dict, warnings: list) -> None:
    """重量口径（任务书A3/A4）：
    - 栏位18格内数值按СМГС口径作为毛重（置信度0.8+说明，不冒充高置信）；
    - 净重必须有"净/нетто"标签才recognized；否则列出候选 needs_review，
      不静默判定，不因任何基准值强行识别。"""
    labeled_gross, labeled_net, unlabeled = _weight_box_numbers(
        units, _WEIGHT_GROSS_FRAGS + _WEIGHT_NET_FRAGS + _WEIGHT_ANY_FRAGS,
        allowed_grapha={"18", "13"})

    if labeled_gross:
        value, raw, page, line = labeled_gross[0]
        fields["gross_weight_kg"] = value
        evidence["gross_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·毛重", "栏位标签（брутто/毛重）",
            0.92, dc.FIELD_RECOGNIZED,
            "；".join(f"「{r}」" for _v, r, _p, _l in labeled_gross[1:]), line.bbox)
    elif unlabeled:
        value, raw, page, line = unlabeled[0]
        fields["gross_weight_kg"] = value
        evidence["gross_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·重量（公斤）", "栏位18格内数值",
            0.8, dc.FIELD_RECOGNIZED,
            "栏位未标注毛/净，按СМГС栏位18口径作为毛重；请复核", line.bbox)

    if labeled_net:
        value, raw, page, line = labeled_net[0]
        fields["net_weight_kg"] = value
        evidence["net_weight_kg"] = make_evidence(
            raw, value, page, "СМГС栏位18·净重", "栏位标签（нетто/净重）",
            0.9, dc.FIELD_RECOGNIZED, "", line.bbox)
        return

    # 净重无标签：收集候选（栏位18格内第二个重量 + 页面其他kg数值）→ needs_review
    known = {v for v, _r, _p, _l in labeled_gross + unlabeled}
    cands = list(unlabeled[1:])                        # 格内第二个未标注重量
    for e in _page_kg_numbers(units, known):
        if e[0] not in known:
            cands.append(e)
    if cands:
        raws = "；".join(f"「{r}」" for _v, r, _p, _l in cands[:4])
        first = cands[0]
        evidence["net_weight_kg"] = make_evidence(
            raws, None, first[2], "СМГС栏位18/20/21区域", "多重量候选（无净重标签）",
            0.0, dc.FIELD_NEEDS_REVIEW,
            "页面无『净重/нетто』标签，以下重量候选无法证明净重口径，未判定，请人工确认：" + raws,
            first[3].bbox if first[3] is not None else None)
        warnings.append("货物净重：页面无净重标签，多个重量候选无法判定口径，已转人工确认"
                        "（未静默判定）")


def _collect_currency_value(units: list, fields: dict, evidence: dict) -> None:
    """货值+币种：货值标签行的 数值+币种 联合判定。"""
    frags = ["货值", "货物价值", "Стоимость груза", "Стоимостьгруза",
             "Cargo Value", "Value of Goods"]
    hits = _find_label_lines(units, frags)
    for line, _s, _e in hits:
        same = _clean_tail(line.text[_e:])
        cur = _CURRENCY_TOKEN.search(same)
        nums = _NUMBER_RE.findall(same)
        if not (cur and nums):
            for cont in _box_lines(units, line, max_lines=2):
                if _is_box_noise(cont):
                    continue
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
    for line, _s, _e in _find_label_lines(units, ["币种", "Валюта", "Currency"]):
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


def _is_label_like(text: str) -> bool:
    """文本是否本身是某个栏位标签行（续接应停止）。"""
    body, _ = strip_grapha(text)
    if _label_pattern(_ALL_SMGS_LABEL_FRAGS).search(body) and len(body) < 48:
        return True
    return False


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
                raw, value, page,
                f"{dc.DOC_TYPE_LABELS.get(doc_type, doc_type)}·标签「{m.group(0)}」",
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
