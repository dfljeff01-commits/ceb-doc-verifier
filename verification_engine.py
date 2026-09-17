# -*- coding: utf-8 -*-
"""
中欧班列单证核验规则引擎（Demo 版）。

职责：对一组"模拟OCR提取结果"的单证数据做三类核验：
  1. 单证齐全性  —— 必需单证是否齐全（发票/装箱单/铁路运单/出口报关单/原产地证）
  2. 字段一致性  —— 货物描述、件数、毛重（1%容差）、收发货人、运单号/箱号、金额等交叉比对
  3. 路线合规性  —— 运单类型与经停国家是否匹配（SMGS/CIM覆盖范围）、报关单起运/目的国与运单路线端点一致性

本引擎为纯标准库实现，不依赖 streamlit / pandas，便于单独自测与后续接入真实OCR。
规则为竞赛演示用简化版，不代表实际铁路/海关作业口径。

对外主入口：run_verification(batch: dict) -> dict
"""

from __future__ import annotations

import re

import risk_model
import semantic

# ---------------------------------------------------------------- 常量与配置

STATUS_PASS = "PASS"
STATUS_WARNING = "WARNING"
STATUS_FAIL = "FAIL"

SEVERITY_ORDER = {STATUS_FAIL: 0, STATUS_WARNING: 1, STATUS_PASS: 2}

# 毛重比对容差（相对发票毛重的比例）
WEIGHT_TOLERANCE = 0.01
# 金额比对容差（报关申报金额 vs 发票金额）
AMOUNT_TOLERANCE = 0.005

# 必需单证类型（齐全性检查）
REQUIRED_DOC_TYPES = [
    "invoice",
    "packing_list",
    "railway_waybill",
    "export_customs_declaration",
    "certificate_of_origin",
]

REQUIRED_DOC_NAMES = {
    "invoice": "商业发票（Commercial Invoice）",
    "packing_list": "装箱单（Packing List）",
    "railway_waybill": "国际铁路运单（Railway Consignment Note）",
    "export_customs_declaration": "出口报关单（Export Customs Declaration）",
    "certificate_of_origin": "原产地证书（Certificate of Origin）",
}

DOC_TYPE_NAMES = dict(REQUIRED_DOC_NAMES)

# SMGS（《国际货协》/OSJD）运单通常覆盖的国家/地区（演示用简化集合）
SMGS_COUNTRIES = {
    "中国", "蒙古", "越南", "哈萨克斯坦", "乌兹别克斯坦", "土库曼斯坦",
    "吉尔吉斯斯坦", "塔吉克斯坦", "俄罗斯", "白俄罗斯", "乌克兰", "摩尔多瓦",
    "拉脱维亚", "立陶宛", "爱沙尼亚", "波兰", "德国", "捷克", "斯洛伐克",
    "匈牙利", "罗马尼亚", "保加利亚", "阿塞拜疆", "格鲁吉亚", "伊朗",
}

# CIM（《国际铁路运输公约》/OTIF）运单通常覆盖的国家/地区（演示用简化集合）
CIM_COUNTRIES = {
    "德国", "法国", "波兰", "捷克", "斯洛伐克", "匈牙利", "奥地利", "瑞士",
    "意大利", "荷兰", "比利时", "西班牙", "土耳其", "塞尔维亚", "罗马尼亚",
    "保加利亚", "希腊", "格鲁吉亚",
}

# ---------------------------------------------------------------- 工具函数


def _doc_name(doc: dict) -> str:
    return doc.get("title") or DOC_TYPE_NAMES.get(doc.get("doc_type"), doc.get("doc_type", "未知单证"))


def _field(doc: dict, key: str):
    """按 key 取字段值；空串/空白视为未提取到（None）。"""
    value = (doc.get("fields") or {}).get(key)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _to_float(value):
    """OCR字段可能是字符串，尝试安全转 float，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _norm_text(value) -> str:
    """文本归一化：去首尾空白、压缩连续空格。"""
    return " ".join(str(value).split()) if value is not None else ""


def _fmt_qty(value: str) -> str:
    """数量显示：整数值去掉小数点（OCR字段转float后 480.0 -> 480）。"""
    try:
        num = float(value)
        return str(int(num)) if num == int(num) else str(num)
    except (ValueError, TypeError):
        return str(value)


def _make_result(check_id, category, check_name, status, detail, involved_docs,
                 suggestion=None, **extra):
    result = {
        "check_id": check_id,
        "category": category,
        "check_name": check_name,
        "status": status,
        "detail": detail,
        "involved_docs": involved_docs,
        "suggestion": suggestion,
    }
    result.update(extra)
    return result


def _group_by_field(documents: list, key: str) -> list:
    """返回 [(doc, value)]：仅含有该字段且有值的单证。"""
    pairs = []
    for doc in documents:
        value = _field(doc, key)
        if value is not None:
            pairs.append((doc, value))
    return pairs


# ---------------------------------------------------------------- 齐全性检查


def check_completeness(documents: list) -> dict:
    present = {doc.get("doc_type") for doc in documents}
    missing = [t for t in REQUIRED_DOC_TYPES if t not in present]
    if missing:
        names = "、".join(REQUIRED_DOC_NAMES[t] for t in missing)
        detail = f"缺少必需单证：{names}"
        return _make_result(
            "DOC-001", "单证齐全性", "必需单证齐全性检查", STATUS_FAIL, detail,
            [t for t in missing],
            f"请在发运前补办{names}并随报关单证一并提交；原产地证书一般由贸促会（CCPIT）或海关出具，"
            f"缺失将影响目的国清关及关税优惠适用。",
            missing_docs_count=len(missing),
        )
    found = "、".join(_doc_name(doc) for doc in documents)
    return _make_result(
        "DOC-001", "单证齐全性", "必需单证齐全性检查", STATUS_PASS,
        f"必需单证齐全（共{len(documents)}份）：{found}", [], None,
    )


# ---------------------------------------------------------------- 一致性检查


def check_goods_description(documents: list) -> dict:
    """
    货物描述一致性（语义相似度分级，升级任务书·方向一）。

    以多数单证的描述为基准，与其余描述逐一计算语义相似度：
      ≥0.85  一致（视为同一表述）
      0.60-0.85  存疑 WARNING（灰色区，转人工复核/AI二次判断）
      <0.60  不一致 FAIL
    结果中给出具体相似度数值与分级，而非简单 PASS/FAIL。
    """
    pairs = _group_by_field(documents, "goods_description")
    if len(pairs) < 2:
        return _make_result(
            "CONS-001", "字段一致性", "货物描述一致性", STATUS_WARNING,
            "载有货物描述的单证不足2份，无法交叉比对。",
            [], "请确认各单证均已填写货物描述栏，再重新核验。",
        )

    # 按归一化文本分组，取多数为基准
    groups: dict = {}
    for doc, value in pairs:
        groups.setdefault(_norm_text(value), []).append(doc)
    base_desc = max(groups, key=lambda k: len(groups[k]))
    base_text = next(d["fields"]["goods_description"] for d in groups[base_desc])

    if len(groups) == 1:
        names = "、".join(_doc_name(d) for d, _ in pairs)
        return _make_result(
            "CONS-001", "字段一致性", "货物描述一致性", STATUS_PASS,
            f"各单证货物描述一致：「{base_desc}」（{names}）",
            [d.get("doc_id") for d, _ in pairs], None,
        )

    # 与基准逐一计算语义相似度
    conflicts, suspects, matched_but_diff, pair_entries = [], [], [], []
    for desc, docs in groups.items():
        if desc == base_desc:
            continue
        score = semantic.similarity(str(base_text), str(docs[0]["fields"]["goods_description"]))
        grade = semantic.grade_similarity(score)
        doc_names = "、".join(_doc_name(d) for d in docs)
        entry = {
            "docs": [d.get("doc_id") for d in docs],
            "text": docs[0]["fields"].get("goods_description"),
            "score": score,
            "grade": grade,
        }
        pair_entries.append(entry)
        if grade == "mismatch":
            conflicts.append(f"{doc_names}（「{desc}」，语义相似度 {score:.2f}）")
        elif grade == "suspect":
            suspects.append(f"{doc_names}（「{desc}」，语义相似度 {score:.2f}）")
        else:
            matched_but_diff.append(f"{doc_names}（「{desc}」，相似度 {score:.2f}）")

    similarity_notes = conflicts + suspects
    involved = [d.get("doc_id") for g in groups.values() for d in g
                if _norm_text(g[0]["fields"].get("goods_description")) != base_desc]
    similarities_payload = [{"docs": g["docs"], "text": g["text"],
                             "score": g["score"], "grade": g["grade"]}
                            for g in pair_entries]

    if conflicts:
        detail = (f"货物描述与多数单证不一致（基准「{base_desc}」）：{'；'.join(similarity_notes)}")
        return _make_result(
            "CONS-001", "字段一致性", "货物描述一致性", STATUS_FAIL, detail, involved,
            f"以多数单证载明的「{base_desc}」为准，修改{'；'.join(conflicts)}中的货物描述，"
            f"确保全套单证品名一致后再行报关；品名不一致易在口岸查验时被转人工审单，造成换装滞留。",
            similarities=similarities_payload,
        )

    if suspects:
        detail = (f"货物描述存在表述差异，处于语义灰色区（基准「{base_desc}」）：{'；'.join(suspects)}；"
                  f"需人工复核或由AI二次判断是否为同一批货物的合理表述差异")
        return _make_result(
            "CONS-001", "字段一致性", "货物描述一致性", STATUS_WARNING, detail, involved,
            f"请人工核对这些差异描述对应的货物明细，或交由AI推理层二次判断："
            f"若确认为同一批货物的同义表述，统一各单证品名写法即可；若实为不同货物，立即更正报关单证。",
            similarities=similarities_payload,
        )

    note = f"（{'；'.join(matched_but_diff)}，语义一致）" if matched_but_diff else ""
    return _make_result(
        "CONS-001", "字段一致性", "货物描述一致性", STATUS_PASS,
        f"各单证货物描述语义一致（基准「{base_desc}」）{note}",
        involved, None, similarities=similarities_payload,
    )


def check_total_packages(documents: list) -> dict:
    pairs = _group_by_field(documents, "total_packages")
    if len(pairs) < 2:
        return _make_result(
            "CONS-002", "字段一致性", "件数（箱数）一致性", STATUS_WARNING,
            "载有件数的单证不足2份，无法交叉比对。", [],
            "请确认发票、装箱单、报关单均已填写总件数栏。",
        )
    values = {str(_to_float(v) if _to_float(v) is not None else v): [] for _, v in pairs}
    for doc, value in pairs:
        values[str(_to_float(value) if _to_float(value) is not None else value)].append(doc)

    if len(values) == 1:
        count = _fmt_qty(next(iter(values)))
        names = "、".join(_doc_name(d) for d, _ in pairs)
        return _make_result(
            "CONS-002", "字段一致性", "件数（箱数）一致性", STATUS_PASS,
            f"各单证件数一致：{count}件（{names}）",
            [d.get("doc_id") for d, _ in pairs], None,
        )

    # 以出现最多的件数为基准
    base = max(values, key=lambda k: len(values[k]))
    wrong = []
    for value, docs in values.items():
        if value != base:
            wrong.append("、".join(f"{_doc_name(d)}（{_fmt_qty(value)}件）" for d in docs))
    detail = f"件数不一致：多数单证为{_fmt_qty(base)}件，{'；'.join(wrong)}"
    return _make_result(
        "CONS-002", "字段一致性", "件数（箱数）一致性", STATUS_FAIL, detail,
        [d.get("doc_id") for docs in values.values() for d in docs
         if str(_to_float(d.get("fields", {}).get("total_packages"))) != base],
        f"请核对现场装箱计数：若实际为{base}件，则更正{'、'.join(wrong)}并重新提交报关；"
        f"若实际与其他单证一致，则需同步更正发票/装箱单。件数不符将导致口岸过机查验与铅封核对异常。",
    )


def check_gross_weight(documents: list) -> dict:
    pairs = _group_by_field(documents, "gross_weight_kg")
    if len(pairs) < 2:
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_WARNING,
            "载有毛重的单证不足2份，无法交叉比对。", [],
            "请确认发票、装箱单均已填写毛重（kg）。",
        )
    baseline_doc, baseline_value = None, None
    for doc, value in pairs:
        if doc.get("doc_type") == "invoice":
            baseline_doc, baseline_value = doc, _to_float(value)
            break
    if baseline_value is None:
        baseline_doc, baseline_value = pairs[0][0], _to_float(pairs[0][1])

    over = []
    for doc, value in pairs:
        weight = _to_float(value)
        if weight is None:
            over.append((_doc_name(doc), value, None))
            continue
        diff_pct = abs(weight - baseline_value) / baseline_value if baseline_value else 0
        if diff_pct > WEIGHT_TOLERANCE:
            over.append((_doc_name(doc), weight, diff_pct))

    if not over:
        names = "、".join(_doc_name(d) for d, _ in pairs)
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_PASS,
            f"各单证毛重一致或差异在1%容差内：{', '.join(f'{_doc_name(d)} {_to_float(v):.0f}kg' for d, v in pairs)}",
            [d.get("doc_id") for d, _ in pairs], None,
        )
    bad_items = []
    for name, weight, diff_pct in over:
        if diff_pct is None:
            bad_items.append(f"{name}（毛重无法解析）")
        else:
            bad_items.append(f"{name}（{weight:.0f}kg，偏离基准{diff_pct * 100:.2f}%）")
    detail = (f"毛重超出1%容差：基准为{_doc_name(baseline_doc)} {baseline_value:.0f}kg，"
              f"{'；'.join(bad_items)}")
    return _make_result(
        "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_FAIL, detail,
        [d.get("doc_id") for d, v in pairs
         if abs((_to_float(v) or 0) - baseline_value) / (baseline_value or 1) > WEIGHT_TOLERANCE],
        f"请复核出厂称重与装箱称重记录，以衡器数据为准修正偏差单证的毛重后重新申报；"
        f"毛重差异超1%在口岸过磅查验时易被认定为申报不实，产生滞留与罚款风险。",
    )


def _norm_name(value) -> str:
    """企业名称比对归一化：去全部空白+大写+OCR常见混淆字符折叠（1→I、0→O、|→I）。
    名称比对对空格不敏感——OCR识别常丢失/增插空格、把I读成1等，按OCR后处理惯例
    折叠易混字符；实体性差异（不同词元）仍会判不一致。"""
    s = re.sub(r"\s+", "", str(value or "")).upper()
    return s.translate(str.maketrans({"1": "I", "0": "O", "|": "I"}))


def _check_party(documents, key, check_id, label):
    pairs = _group_by_field(documents, key)
    if len(pairs) < 2:
        return _make_result(
            check_id, "字段一致性", f"{label}一致性", STATUS_WARNING,
            f"载有{label}的单证不足2份，无法交叉比对。", [],
            f"请确认各单证均已填写{label}栏。",
        )
    groups: dict = {}
    for doc, value in pairs:
        groups.setdefault(_norm_name(value), []).append(doc)
    if len(groups) == 1:
        display = pairs[0][1]          # 展示原始值（含原始空格），而非归一化键
        names = "、".join(_doc_name(d) for d, _ in pairs)
        return _make_result(
            check_id, "字段一致性", f"{label}一致性", STATUS_PASS,
            f"各单证{label}一致：「{display}」（{names}）",
            [d.get("doc_id") for d, _ in pairs], None,
        )
    base = max(groups, key=lambda k: len(groups[k]))
    base_display = groups[base][0].get("fields", {}).get(key, base)
    wrong = ["、".join(f"{_doc_name(d)}（{groups[k][0].get('fields', {}).get(key)}）"
                       for d in docs) for k, docs in groups.items() if k != base]
    detail = f"{label}不一致：多数单证为「{base_display}」，{'；'.join(wrong)}"
    return _make_result(
        check_id, "字段一致性", f"{label}一致性", STATUS_FAIL, detail,
        [d.get("doc_id") for k, docs in groups.items() for d in docs if k != base],
        f"以营业执照/贸易合同的中英文名称准，统一各单证{label}栏；"
        f"收发货人名称不符会触发海关企业信息比对异常。",
    )


def check_consignor(documents: list) -> dict:
    return _check_party(documents, "consignor_name", "CONS-004", "发货人名称")


def check_consignee(documents: list) -> dict:
    return _check_party(documents, "consignee_name", "CONS-005", "收货人名称")


def check_waybill_no_crossref(documents: list) -> dict:
    """报关单『运单号』应与铁路运单号一致（单证间交叉引用）。"""
    waybills = [d for d in documents if d.get("doc_type") == "railway_waybill"]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not waybills or not customs:
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_WARNING,
            "缺少铁路运单或出口报关单，无法交叉核对运单号。", [],
            "请先补齐铁路运单与出口报关单。",
        )
    wb_no = _field(waybills[0], "waybill_no")
    cus_no = _field(customs[0], "waybill_no")
    if wb_no is None or cus_no is None:
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_WARNING,
            "运单号字段缺失（运单或报关单未填写），无法交叉核对。",
            [waybills[0].get("doc_id"), customs[0].get("doc_id")],
            "请在铁路运单及报关单『随附单证-运单号』栏补填运单号。",
        )
    if _norm_text(wb_no).upper() == _norm_text(cus_no).upper():
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_PASS,
            f"报关单运单号与铁路运单一致：{wb_no}",
            [waybills[0].get("doc_id"), customs[0].get("doc_id")], None,
        )
    detail = f"报关单运单号（{cus_no}）与铁路运单号（{wb_no}）不一致"
    return _make_result(
        "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_FAIL, detail,
        [customs[0].get("doc_id")],
        f"请将报关单『随附单证-运单号』栏更正为铁路运单号{wb_no}后重新申报。",
    )


def check_container_no(documents: list) -> dict:
    pairs = _group_by_field(documents, "container_no")
    if len(pairs) < 2:
        return _make_result(
            "CONS-007", "字段一致性", "集装箱号一致性", STATUS_PASS,
            "仅一份单证载有集装箱号，无交叉比对需求。", [], None,
        )
    groups: dict = {}
    for doc, value in pairs:
        groups.setdefault(_norm_text(value).upper().replace(" ", ""), []).append(doc)
    if len(groups) == 1:
        return _make_result(
            "CONS-007", "字段一致性", "集装箱号一致性", STATUS_PASS,
            f"各单证集装箱号一致：{next(iter(groups))}",
            [d.get("doc_id") for d, _ in pairs], None,
        )
    base = max(groups, key=lambda k: len(groups[k]))
    wrong = ["、".join(f"{_doc_name(d)}（{groups[k][0].get('fields', {}).get('container_no')}）"
                       for d in docs) for k, docs in groups.items() if k != base]
    detail = f"集装箱号不一致：多数单证为{base}，{'；'.join(wrong)}"
    return _make_result(
        "CONS-007", "字段一致性", "集装箱号一致性", STATUS_FAIL, detail,
        [d.get("doc_id") for k, docs in groups.items() for d in docs if k != base],
        "请核对现场集装箱号与铅封号，更正偏差单证；箱号不符将直接导致口岸无法放行。",
    )


def check_declared_amount(documents: list) -> dict:
    invoices = [d for d in documents if d.get("doc_type") == "invoice"]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not invoices or not customs:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_WARNING,
            "缺少发票或报关单，无法核对申报金额。", [], "请先补齐商业发票与出口报关单。",
        )
    inv_amt = _to_float(_field(invoices[0], "total_amount"))
    cus_amt = _to_float(_field(customs[0], "declared_value"))
    currency = _field(invoices[0], "currency") or ""
    if inv_amt is None or cus_amt is None:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_WARNING,
            "金额字段缺失，无法核对。",
            [invoices[0].get("doc_id"), customs[0].get("doc_id")],
            "请补填发票总金额与报关单申报金额。",
        )
    diff_pct = abs(cus_amt - inv_amt) / inv_amt if inv_amt else 0
    if diff_pct <= AMOUNT_TOLERANCE:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_PASS,
            f"报关申报金额与发票金额一致：{cus_amt:,.2f} {currency}",
            [invoices[0].get("doc_id"), customs[0].get("doc_id")], None,
        )
    detail = f"申报金额不符：发票{inv_amt:,.2f} {currency}，报关单{cus_amt:,.2f} {currency}（偏离{diff_pct * 100:.2f}%）"
    return _make_result(
        "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_FAIL, detail,
        [customs[0].get("doc_id")],
        f"请以发票金额{inv_amt:,.2f} {currency}为准更正报关单申报金额；低报/高报均可能被认定为申报不实。",
    )


# ---------------------------------------------------------------- 路线合规检查


def check_waybill_type_route(documents: list) -> dict:
    waybills = [d for d in documents if d.get("doc_type") == "railway_waybill"]
    if not waybills:
        return _make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_WARNING,
            "缺少铁路运单，无法核验路线合规性。", [], "请先补齐国际铁路运单。",
        )
    wb = waybills[0]
    wb_type = _norm_text(_field(wb, "waybill_type") or "")
    route = _field(wb, "route_countries") or []
    if not wb_type or not route:
        return _make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_WARNING,
            "运单缺少运单类型或经停国家信息，无法核验。",
            [wb.get("doc_id")], "请在运单上补填运单类型（SMGS/CIM）及经停国家。",
        )

    uncovered_smgs = [c for c in route if c not in SMGS_COUNTRIES]
    uncovered_cim = [c for c in route if c not in CIM_COUNTRIES]

    is_pure_smgs = "SMGS" in wb_type.upper() and "CIM" not in wb_type.upper()
    is_pure_cim = "CIM" in wb_type.upper() and "SMGS" not in wb_type.upper()
    is_composite = ("CIM" in wb_type.upper() and "SMGS" in wb_type.upper())

    if is_composite:
        return _make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_PASS,
            f"运单类型为「{wb_type}」，同时覆盖SMGS与CIM段经停国家：{'、'.join(route)}",
            [wb.get("doc_id")], None,
        )
    if is_pure_smgs and uncovered_smgs:
        bad = "、".join(uncovered_smgs)
        return _make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_WARNING,
            f"线路含{bad}，但运单类型为{wb_type}（SMGS运单不覆盖{bad}段）。",
            [wb.get("doc_id")],
            f"本线路经由{bad}（中间走廊/巴库-第比利斯-卡尔斯段），建议改用CIM/SMGS统一运单，"
            f"或提前与承运人确认卡尔斯换装段的运单转换与补单安排，避免边境段无有效运单凭证。",
        )
    if is_pure_cim and uncovered_cim:
        bad = "、".join(uncovered_cim)
        return _make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_WARNING,
            f"线路含{bad}，但运单类型为{wb_type}（CIM运单不覆盖{bad}段）。",
            [wb.get("doc_id")],
            f"CIM运单不覆盖{bad}段，建议改用CIM/SMGS统一运单或分段衔接安排。",
        )
    return _make_result(
        "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_PASS,
        f"运单类型「{wb_type}」与经停国家（{'、'.join(route)}）匹配。",
        [wb.get("doc_id")], None,
    )


def check_route_ends(documents: list) -> dict:
    waybills = [d for d in documents if d.get("doc_type") == "railway_waybill"]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not waybills or not customs:
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_WARNING,
            "缺少铁路运单或报关单，无法核对路线端点。", [], "请先补齐铁路运单与出口报关单。",
        )
    route = _field(waybills[0], "route_countries") or []
    dep_cus = _field(customs[0], "departure_country")
    dst_cus = _field(customs[0], "destination_country")
    if not route or dep_cus is None or dst_cus is None:
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_WARNING,
            "运单经停国家或报关单起运/目的国信息缺失，无法核对。",
            [waybills[0].get("doc_id"), customs[0].get("doc_id")],
            "请补填运单经停国家与报关单起运国/运抵国。",
        )
    if route[0] == dep_cus and route[-1] == dst_cus:
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_PASS,
            f"运单路线端点（{route[0]} → {route[-1]}）与报关单起运国/运抵国一致。",
            [waybills[0].get("doc_id"), customs[0].get("doc_id")], None,
        )
    detail = (f"路线端点不一致：运单路线为{' → '.join(route)}，"
              f"报关单起运国/运抵国为{dep_cus}/{dst_cus}")
    return _make_result(
        "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_FAIL, detail,
        [customs[0].get("doc_id")],
        "请核对货物实际运输路径，修改报关单起运国/运抵国（或运单路线）后重新申报。",
    )


# ---------------------------------------------------------------- 主入口

CHECK_RUNNERS = [
    check_completeness,
    check_goods_description,
    check_total_packages,
    check_gross_weight,
    check_consignor,
    check_consignee,
    check_waybill_no_crossref,
    check_container_no,
    check_declared_amount,
    check_waybill_type_route,
    check_route_ends,
]


def run_verification(batch: dict) -> dict:
    """
    对一个批次（解析后的JSON dict，含 documents 列表）执行全部核验。

    返回结构：
    {
        "batch_id": ...,
        "batch_name": ...,
        "results": [ {check_id, category, check_name, status, detail, involved_docs, suggestion}, ... ],
        "summary": {"total": n, "pass": p, "warning": w, "fail": f},
        "risk": { "score": 0-100, "grade": "low"|"medium"|"high",
                  "grade_label": ..., "breakdown": [可解释分数构成] },
        "suggestions": [ 仅为 FAIL/WARNING 项动态生成的修正建议文本, ... ]
    }
    """
    documents = batch.get("documents", [])
    results = [runner(documents) for runner in CHECK_RUNNERS]

    summary = {
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == STATUS_PASS),
        "warning": sum(1 for r in results if r["status"] == STATUS_WARNING),
        "fail": sum(1 for r in results if r["status"] == STATUS_FAIL),
    }
    risk = risk_model.compute_risk(results)
    suggestions = [
        f"【{r['check_name']}】{r['suggestion']}"
        for r in results
        if r["status"] in (STATUS_FAIL, STATUS_WARNING) and r.get("suggestion")
    ]
    return {
        "batch_id": batch.get("batch_id", ""),
        "batch_name": batch.get("batch_name", ""),
        "batch_description": batch.get("description", ""),
        "results": results,
        "summary": summary,
        "risk": risk,
        "suggestions": suggestions,
    }


def sort_results_by_severity(results: list) -> list:
    """按 FAIL > WARNING > PASS 稳定排序（供展示层使用）。"""
    return sorted(results, key=lambda r: SEVERITY_ORDER.get(r["status"], 99))
