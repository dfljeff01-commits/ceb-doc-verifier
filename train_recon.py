# -*- coding: utf-8 -*-
"""数据核对计算引擎（数据核对模块v1）——纯函数，不依赖数据库。

覆盖任务书 §二/§三 的四类核对口径：
  1. 预付款累计 是否覆盖 结算合计（差额/需补款/多付）；
  2. 结算合计 与 实付运费 差异（差异原因为空 → "待人工填写差异原因"提示）；
  3. 三方对账：大同供应链测算补贴 vs 联运公司测算补贴 vs 上级拨付，
     超过阈值（百分比 或 绝对金额，任一触发，可配置）标注"需人工关注"；
  4. 重复值检测：相邻记录同类字段数值完全一致（精确相等）→ 异常提示，
     只提示不下结论（真实数据中 10-05 / 10-10临 / 10-10图 三条补贴值
     完全重复即为典型场景，本引擎不得替用户判定对错）。

四状态口径沿用单据核对已验证思路（财务域措辞）：
  confirmed 已确认 / pending 待确认 / not_found 未找到 /
  business_missing 业务确认缺失。字段允许暂缺，不强制必填、不阻断。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- 状态口径

RECORD_CONFIRMED = "confirmed"
RECORD_PENDING = "pending"
RECORD_NOT_FOUND = "not_found"
RECORD_BUSINESS_MISSING = "business_missing"

RECORD_STATUS_LABELS = {
    RECORD_CONFIRMED: "已确认",
    RECORD_PENDING: "待确认",
    RECORD_NOT_FOUND: "未找到",
    RECORD_BUSINESS_MISSING: "业务确认缺失",
}

REVIEW_NORMAL = "normal"
REVIEW_SUSPECT = "suspect"
REVIEW_REVIEWED = "reviewed"

REVIEW_STATUS_LABELS = {
    REVIEW_NORMAL: "正常",
    REVIEW_SUSPECT: "存疑，需人工复核",
    REVIEW_REVIEWED: "已复核",
}

# 补贴测算数值字段（train_subsidies 列 → 中文标签，重复值检测与展示共用）
SUBSIDY_AMOUNT_FIELDS = {
    "dt_supply_100": "大同供应链·补贴资料100%",
    "dt_supply_70": "大同供应链·补贴70%",
    "ly_advance_100": "联运垫付补贴100%",
    "ly_recover_70": "需回款给联运的70%",
    "auth_confirm_100": "上级·确认补贴100%",
    "auth_advance_70": "上级·预拨付70%",
    "auth_remain_30": "上级·剩余30%",
    "forecast_diff": "预测补贴差额",
}

# 三方对账取数口径：三方各取"100%"口径数字（任务书 §三 三方对账）
THREE_WAY_FIELDS = {
    "dt_supply_100": "大同供应链测算补贴(100%)",
    "ly_advance_100": "联运公司测算补贴(100%)",
    "auth_confirm_100": "上级拨付(确认补贴100%)",
}

# 结算明细数值字段（导入校验/展示共用）
SETTLEMENT_AMOUNT_FIELDS = {
    "rail_freight": "铁路运费",
    "customs_fee": "报关费",
    "service_fee": "服务费",
    "other_fee": "其他费用",
    "settle_total": "结算合计",
    "actual_freight": "实付运费",
}

# 默认核对阈值（app_config 缺行时的兜底；正式值以 train_store.get_config 为准）
DEFAULT_THRESHOLDS = {"recon_warn_pct": Decimal("5"),
                      "recon_warn_amount": Decimal("5000")}


def to_decimal(value):
    """宽容数值转换：None/''/空白 → None；不可解析 → None（不抛错）。

    金额字段允许暂缺是任务书明确口径（不阻断），解析失败按"未填写"处理，
    由界面提示，绝不让脏数据炸掉整个核对页面。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    text = str(value).strip().replace(",", "").replace("¥", "").replace("元", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "未填写"
    return f"{value:,.2f}"


# ---------------------------------------------------------------- 1. 预付覆盖

def prepay_check(settle_total, prepays: list[dict]) -> dict:
    """预付款累计 是否覆盖 结算合计。

    prepays: [{"amount": 数值可转, "paid_at": ...}, ...]
    返回 {prepay_total, settle_total, diff, verdict, message}：
      diff = 结算合计 - 预付累计；>0 需补款 / <0 多付 / =0 已覆盖。
    """
    prepay_total = Decimal("0")
    for p in prepays:
        amount = to_decimal(p.get("amount"))
        if amount is not None:
            prepay_total += amount
    settle = to_decimal(settle_total)
    diff = None if settle is None else settle - prepay_total
    if settle is None:
        verdict = "unknown"
        message = "结算合计未填写，无法核对预付款覆盖情况。"
    elif diff > 0:
        verdict = "shortage"
        message = f"预付款累计 {_fmt_money(prepay_total)} 元，少于结算合计 " \
                  f"{_fmt_money(settle)} 元，需补款 {_fmt_money(diff)} 元。"
    elif diff < 0:
        verdict = "overpaid"
        message = f"预付款累计 {_fmt_money(prepay_total)} 元，超过结算合计 " \
                  f"{_fmt_money(settle)} 元，多付 {_fmt_money(-diff)} 元，请人工确认。"
    else:
        verdict = "covered"
        message = f"预付款累计 {_fmt_money(prepay_total)} 元，恰好覆盖结算合计。"
    return {"prepay_total": prepay_total, "settle_total": settle,
            "diff": diff, "verdict": verdict, "message": message}


# ---------------------------------------------------------------- 2. 结算 vs 实付

def settle_actual_check(settle_total, actual_freight, diff_reason) -> dict:
    """结算合计 与 实付运费差异核对；差异原因为空必须提示（不能空着不提示）。"""
    settle = to_decimal(settle_total)
    actual = to_decimal(actual_freight)
    reason = str(diff_reason or "").strip()
    if settle is None or actual is None:
        return {"diff": None, "has_diff": False, "needs_reason": False,
                "message": "结算合计或实付运费未填写，暂不能核对结算与实付差异。"}
    diff = settle - actual
    has_diff = diff != 0
    needs_reason = has_diff and not reason
    if not has_diff:
        message = "结算合计与实付运费一致。"
    elif needs_reason:
        message = (f"结算合计与实付运费存在差异 {_fmt_money(diff)} 元，"
                   f"待人工填写差异原因。")
    else:
        message = f"结算合计与实付运费差异 {_fmt_money(diff)} 元，原因：{reason}。"
    return {"diff": diff, "has_diff": has_diff,
            "needs_reason": needs_reason, "message": message}


# ---------------------------------------------------------------- 3. 三方对账

def three_way_check(row: dict, warn_pct=None, warn_amount=None) -> dict:
    """三方对账：三个100%口径数字两两比对，超阈值标记需人工关注。

    阈值口径（任务书 §三，暂定可配置）：|差| > 绝对阈值 或
    |差|/两值中绝对值较大者 > 百分比阈值（分母取大者 → 保守少误报），
    任一条件触发即标注。数值缺失的一对跳过并提示。
    """
    pct = to_decimal(warn_pct) if warn_pct is not None else DEFAULT_THRESHOLDS["recon_warn_pct"]
    amount = to_decimal(warn_amount) if warn_amount is not None else DEFAULT_THRESHOLDS["recon_warn_amount"]
    values = {field: to_decimal(row.get(field)) for field in THREE_WAY_FIELDS}
    pairs = []
    fields = list(THREE_WAY_FIELDS)
    any_flag = False
    for i in range(len(fields)):
        for j in range(i + 1, len(fields)):
            fa, fb = fields[i], fields[j]
            va, vb = values[fa], values[fb]
            entry = {
                "a_field": fa, "b_field": fb,
                "a_label": THREE_WAY_FIELDS[fa], "b_label": THREE_WAY_FIELDS[fb],
                "a_value": va, "b_value": vb, "diff": None,
                "diff_pct": None, "flag": False, "message": "",
            }
            if va is None or vb is None:
                entry["message"] = (f"{THREE_WAY_FIELDS[fa]}或{THREE_WAY_FIELDS[fb]}"
                                    f"未填写，该组暂不能比对。")
            else:
                diff = va - vb
                basis = max(abs(va), abs(vb), Decimal("1"))
                pct_val = (abs(diff) / basis * 100) if diff != 0 else Decimal("0")
                flag = abs(diff) > amount or pct_val > pct
                entry.update({
                    "diff": diff, "diff_pct": pct_val, "flag": flag,
                    "message": (f"差异 {_fmt_money(diff)} 元（{pct_val:.2f}%），"
                                f"需人工关注" if flag else "一致"),
                })
                any_flag = any_flag or flag
            pairs.append(entry)
    missing = [THREE_WAY_FIELDS[f] for f, v in values.items() if v is None]
    return {"pairs": pairs, "flag": any_flag,
            "values": values,
            "missing_fields": missing,
            "message": ("三方数值均在阈值范围内" if not any_flag and not missing
                        else "；".join(p["message"] for p in pairs if p["message"]))}


# ---------------------------------------------------------------- 4. 重复值检测

def duplicate_neighbor_flags(rows: list[dict],
                             fields: dict | None = None) -> dict[str, list[dict]]:
    """相邻记录同类字段数值完全一致检测（简单精确相等，不搞复杂算法）。

    rows: 至少含 trip_no、dep_date（可排序）与若干数值字段的记录，
          内部按 (dep_date, trip_no) 排序后两两相邻比较；
    fields: {字段名: 中文标签}，默认补贴数值字段。
    返回 {trip_no: [{field, label, value, other_trip, other_dep_date}]}，
    相同的两侧记录都标注（界面双向可见）。只提示，不下结论。
    """
    fields = fields or SUBSIDY_AMOUNT_FIELDS
    ordered = sorted(rows, key=lambda r: (str(r.get("dep_date") or ""),
                                          str(r.get("trip_no") or "")))
    flags: dict[str, list[dict]] = {}
    for i in range(len(ordered) - 1):
        left, right = ordered[i], ordered[i + 1]
        for field, label in fields.items():
            lv, rv = to_decimal(left.get(field)), to_decimal(right.get(field))
            if lv is None or rv is None or lv != rv:
                continue
            for row, other in ((left, right), (right, left)):
                flags.setdefault(row["trip_no"], []).append({
                    "field": field, "label": label,
                    "value": str(lv),
                    "other_trip": other.get("trip_no"),
                    "other_dep_date": str(other.get("dep_date") or ""),
                })
    return flags


def missing_field_names(row: dict, fields: dict | None = None) -> list[str]:
    """给定记录中"未填写"的字段中文名列表（界面提示用，不阻断）。"""
    fields = fields or SUBSIDY_AMOUNT_FIELDS
    return [label for field, label in fields.items()
            if to_decimal(row.get(field)) is None]
