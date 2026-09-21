# -*- coding: utf-8 -*-
"""版式抽取层测试（P0任务书A1/A5）：四状态证据、通用标签跨行取值、
数值定稿、集装箱号边界、重量口径判定。SMGS端到端回归见 test_smgs_regression.py。
运行：pytest test_field_extraction.py -v
"""

import field_extraction as fe


def _line(text: str, page: int = 1, bbox=None):
    return fe.Line(text=text, page=page, bbox=bbox, mode="text")


def _units(lines):
    return [fe.PageUnits(page_no=1, mode="text", lines=lines)]


# ---------------------------------------------------------------- 数值/箱号

def test_parse_num_thousands_and_decimal():
    assert fe.parse_num("12,300") == 12300
    assert fe.parse_num("5629.84") == 5629.84
    assert fe.parse_num("3650") == 3650
    assert fe.parse_num("abc") is None
    assert fe.parse_num("12.3.4") is None


def test_container_no_must_not_grab_trailing_digits():
    """ISO箱号：7位数字后不能再吞尺寸/载重数字（任务书A3 栏位7附加值）。"""
    m = fe._CONTAINER_RE.search("TKRU4625794 20' 8\"6'' 30480 kg")
    assert m is not None and m.group(1) == "TKRU4625794"
    # 非箱号（字母不足/数字后再跟数字）
    assert fe._CONTAINER_RE.search("MSKU12345678") is None


def test_strip_grapha_removes_leading_number():
    body, grapha = fe.strip_grapha("18 Вес 重量 (кг)")
    assert grapha == "18" and body.startswith("Вес")
    body, grapha = fe.strip_grapha("普通文本 无栏位")
    assert grapha is None


# ---------------------------------------------------------------- 标签匹配/合理性

def test_plausible_value_rejects_label_and_paren():
    assert fe._plausible_value("(Наименование и адрес)") is False
    assert fe._plausible_value("发货人") is False       # 标签词本身
    assert fe._plausible_value("18") is False           # 栏位号
    assert fe._plausible_value("芜湖德菲图汽车技术有限公司") is True


def test_find_label_lines_prefers_grapha_number():
    units = _units([
        _line("1 Отправитель 发货人"),
        _line("芜湖德菲图汽车技术有限公司"),
    ])
    hits = fe._find_label_lines(units, ["发货人", "Отправитель"])
    assert hits
    line, start, end = hits[0]
    assert line.text.startswith("1")
    # 匹配首个标签词（Отправитель）；切片不被栏位号错位即可（后续仍是同行标签词）
    assert "Отправитель" in line.text[start:end] or "发货人" in line.text[start:end]


# ---------------------------------------------------------------- 续行取值

def test_continuation_lines_by_coordinates():
    """坐标模式：标签行下方、x区间重叠的行才续接（其他栏位不混入）。"""
    label = _line("1 Отправитель 发货人", bbox=(30, 90, 280, 102))
    value = _line("芜湖德菲图汽车技术有限公司", bbox=(36, 110, 240, 122))
    other_box = _line("大同 DATONG", bbox=(302, 110, 500, 122))
    units = _units([label, value, other_box])
    conts = fe._box_lines(units, label)
    assert value in conts and other_box not in conts


# ---------------------------------------------------------------- SMGS重量口径

def test_gross_labeled_weight_is_recognized():
    units = _units([
        _line("18 Вес брутто 毛重: 5629.84 кг"),
        _line("Вес нетто 净重: 3650.0 кг"),
    ])
    fields, evidence, warnings = fe._smgs_extract(units)
    assert fields["gross_weight_kg"] == 5629.84
    assert evidence["gross_weight_kg"]["status"] == "recognized"
    assert fields["net_weight_kg"] == 3650.0


def test_unlabeled_weight_goes_to_needs_review():
    """未标明毛/净口径的重量不得静默判定（任务书A3/A4）：
    首值按栏位口径暂记毛重（低置信+说明），净重候选进 needs_review。"""
    units = _units([
        _line("18 Вес груза 重量: 5629.84 кг"),
        _line("3650.0 кг"),
    ])
    fields, evidence, warnings = fe._smgs_extract(units)
    assert fields["gross_weight_kg"] == 5629.84
    assert evidence["gross_weight_kg"]["status"] == "recognized"
    assert evidence["gross_weight_kg"]["confidence"] == 0.8   # 低置信：口径未标注
    assert "毛/净" in evidence["gross_weight_kg"]["note"]
    assert evidence["net_weight_kg"]["status"] == "needs_review"
    assert evidence["net_weight_kg"]["value"] is None         # 不静默填值
    assert "3650.0" in evidence["net_weight_kg"]["raw_text"]


# ---------------------------------------------------------------- 通用适配器

def test_generic_label_cross_line_extraction():
    """标签独立成行 → 取下一行（旧正则盲区，任务书A5）。"""
    units = _units([
        _line("SELLER:"),
        _line("ABC Trading Co., Ltd."),
        _line("BUYER:"),
        _line("ООО Покупатель"),
    ])
    fields, evidence = fe._generic_label_extract(units, "invoice")
    assert fields["consignor_name"] == "ABC Trading Co., Ltd."
    assert fields["consignee_name"] == "ООО Покупатель"


# ---------------------------------------------------------------- 必填补齐

def test_finalize_marks_required_not_found():
    import doc_contract
    outcome = fe.ExtractOutcome(fields={}, evidence={}, warnings=[])
    outcome.finalize("smgs_rail_waybill")
    assert "waybill_no" in outcome.evidence
    assert outcome.evidence["waybill_no"]["status"] == doc_contract.FIELD_NOT_FOUND
    # 绝不产出 business_missing
    assert all(ev["status"] != doc_contract.FIELD_BUSINESS_MISSING
               for ev in outcome.evidence.values())
