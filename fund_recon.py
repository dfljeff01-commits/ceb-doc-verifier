# -*- coding: utf-8 -*-
"""资金/费用批次核对引擎（数据核对模块v1.1，纯函数/轻DB辅助）。

任务书口径：
  - 批次覆盖核对：Σ(批次关联班列的结算合计/费用科目) vs batch.total_amount，
    汇总维度为"批次"而非"单趟车"，允许批次内单笔分布不均匀；
  - 用途分类（修订版）：仅 participates_in_freight_recon=TRUE 的批次参与
    覆盖核对；补贴回款/保证金不参与，避免不同性质资金混算；
  - 单趟车层面：款项来自多对多批次时不单独报警，标注"包含在批次XXX中"；
  - 孤儿检测（安全网，非阻断）：批次关联了不存在的班列、同一笔预付在
    v1 train_prepayments 与批次中重复登记等风险，提示人工核实。

纯函数部分不依赖数据库；涉及班列存在性/重复登记的检查由 fund_store 传入
数据后由本模块判定（不直接查库，保持可测）。
"""

from __future__ import annotations

from decimal import Decimal

import train_recon

# ---------------------------------------------------------------- 用途枚举

# 用途代码（业务可维护：展示名/是否现行/默认参与核对均由数据表驱动，
# 不把判断硬编码在业务分支里）
PURPOSE_FREIGHT = "预付运费"
PURPOSE_BALANCE = "尾款结算"
PURPOSE_SUBSIDY = "补贴回款"
PURPOSE_DEPOSIT = "保证金"
PURPOSE_OTHER = "其他"

FUND_PURPOSES = [
    # code, 展示名, 现行常规选项, 默认参与运费核对, 说明
    (PURPOSE_FREIGHT, "预付运费", True, True, "现行主要方式，打款保障班列发运"),
    (PURPOSE_BALANCE, "尾款结算", True, True, "发运后按实际结算补齐差额"),
    (PURPOSE_SUBSIDY, "补贴回款", True, False, "与补贴对账70%回款挂钩"),
    (PURPOSE_DEPOSIT, "保证金", False, False, "历史遗留科目，现行已不使用"),
    (PURPOSE_OTHER, "其他", True, True, "需强制填写具体说明"),
]

FUND_PURPOSE_LABELS = {code: label for code, label, _a, _r, _d in FUND_PURPOSES}
PURPOSE_NOTES = {code: note for code, _l, _a, _r, note in FUND_PURPOSES}

# 新建批次表单默认展示的用途（保证金收进"更多/历史科目"折叠区，防误用）
CURRENT_PURPOSES = [code for code, _l, active, _r, _d in FUND_PURPOSES if active]
HISTORICAL_PURPOSES = [code for code, _l, active, _r, _d in FUND_PURPOSES
                       if not active]


def default_participates(fund_purpose: str) -> bool:
    """用途 → 是否参与运费覆盖核对的默认值（修订版：系统自动带出，
    允许人工改写并留痕，不锁死）。未知用途保守取 True（提示人工确认）。"""
    for code, _l, _a, recon_default, _d in FUND_PURPOSES:
        if code == fund_purpose:
            return recon_default
    return True


def validate_purpose(purpose: str, remark: str = "") -> str | None:
    """用途合法性校验。'其他'必须带具体说明；保证金仅历史导入/显式历史科目可用。
    返回错误消息或 None。"""
    if purpose not in FUND_PURPOSE_LABELS:
        return f"用途不合法：{purpose!r}"
    if purpose == PURPOSE_OTHER and not str(remark or "").strip():
        return "用途为「其他」时必须填写具体说明，不能空着。"
    return None


# ---------------------------------------------------------------- 批次覆盖核对

# cost 批次取数：cost_category → 结算/费用科目字段
COST_CATEGORY_FIELD = {
    "铁路运费": "rail_freight",
    "报关费": "customs_fee",
    "服务费": "service_fee",
    "其他": "other_fee",
}


def _trip_amount_for(batch_type: str, cost_category: str, trip_row: dict) -> Decimal | None:
    """单趟车对批次的应覆盖金额：
      prepay → 结算合计 settle_total；
      cost   → cost_category 对应费用科目（无类目时取结算合计兜底）。"""
    if batch_type == "cost":
        field = COST_CATEGORY_FIELD.get(cost_category or "", "settle_total")
        return train_recon.to_decimal(trip_row.get(field))
    return train_recon.to_decimal(trip_row.get("settle_total"))


def batch_coverage_check(batch: dict, trip_rows: list[dict]) -> dict:
    """批次级覆盖核对（核心）。

    batch 需含 batch_id/batch_type/fund_purpose/total_amount/
                participates_in_freight_recon；
    trip_rows：该批次关联班列的取数行（含 settle_total/费用科目）。
    返回 {covered_total, batch_total, diff, verdict, message, skipped}：
      - participates=FALSE 的批次跳过（补贴回款/保证金），不产出差异；
      - 差额 = 关联班列口径合计 - 批次总额；
      - 口径与 v1 prepay_check 一致，只是维度为批次。
    """
    total = train_recon.to_decimal(batch.get("total_amount"))
    purpose = batch.get("fund_purpose", PURPOSE_FREIGHT)
    participates = batch.get("participates_in_freight_recon", True)

    base_result = {
        "batch_id": batch.get("batch_id"),
        "batch_total": total,
        "covered_total": Decimal("0"),
        "trip_count": len(trip_rows),
        "fund_purpose": purpose,
        "diff": None, "verdict": "skipped",
        "message": "", "skipped": not participates,
    }
    if not participates:
        base_result["message"] = (
            f"用途为「{FUND_PURPOSE_LABELS.get(purpose, purpose)}」，"
            "按口径不参与运费覆盖核对（仅归类展示）。")
        if purpose == PURPOSE_SUBSIDY:
            base_result["message"] += "补贴回款的核对请见补贴对账模块。"
        return base_result

    covered = Decimal("0")
    missing_trips = []
    for row in trip_rows:
        amount = _trip_amount_for(batch.get("batch_type", "prepay"),
                                  batch.get("cost_category"), row)
        if amount is None:
            missing_trips.append(row.get("trip_no"))
            continue
        covered += amount

    diff = None if total is None else covered - total
    if total is None:
        verdict, message = "unknown", "批次总金额缺失，无法核对。"
    elif diff > 0:
        verdict = "shortage"
        message = (f"批次关联 {len(trip_rows)} 趟车的口径合计 "
                   f"{train_recon._fmt_money(covered)} 元，多于批次总额 "
                   f"{train_recon._fmt_money(total)} 元，差额 "
                   f"{train_recon._fmt_money(diff)} 元——批次总量对不上，请人工查明细。")
    elif diff < 0:
        verdict = "under"
        message = (f"批次关联班列口径合计 {train_recon._fmt_money(covered)} 元，"
                   f"少于批次总额 {train_recon._fmt_money(total)} 元，差额 "
                   f"{train_recon._fmt_money(-diff)} 元——"
                   f"可能存在未关联进批次的班列，请人工核实覆盖范围。")
    else:
        verdict = "covered"
        message = (f"批次总额 {train_recon._fmt_money(total)} 元与关联 "
                   f"{len(trip_rows)} 趟车口径合计一致（批次内单笔分布不影响结论）。")
    base_result.update({
        "covered_total": covered, "diff": diff,
        "verdict": verdict, "message": message,
        "missing_trip_amounts": missing_trips,
    })
    return base_result


# ---------------------------------------------------------------- 单趟车来源提示

def trip_funding_context(trip_no: str, batch_links: list[dict],
                         legacy_prepay_total=None) -> dict:
    """单趟车的资金来源展示口径（任务书：来自批次不单独报警）。

    batch_links: [{batch_id, batch_type, fund_purpose, trip_count,
                   allocated_amount, batch_verdict}]
    返回 {from_batches, suppress_single_alert, message}：
      有批次关联 → 该趟车"预付款覆盖"不单独报警，即使本车分摊为0，
      提示去批次页看整体结果（8月9日记0场景的防假警报口径）。
    """
    if not batch_links:
        return {"from_batches": [], "suppress_single_alert": False,
                "legacy_prepay_total": legacy_prepay_total,
                "message": ""}
    parts = []
    any_recon = False
    for link in batch_links:
        label = f"批次 {link['batch_id']}（共{link.get('trip_count', '?')}趟车"
        if link.get("allocated_amount") is not None:
            label += f"，本车分摊 {train_recon._fmt_money(train_recon.to_decimal(link['allocated_amount']))}"
        label += "）"
        parts.append(label)
        if link.get("batch_verdict") not in ("skipped", None):
            any_recon = True
    message = ("本趟车的"
               + ("预付款/费用" if len({l.get("batch_type") for l in batch_links}) > 1
                  else ("费用" if batch_links[0].get("batch_type") == "cost" else "预付款"))
               + "包含在 " + "、".join(parts)
               + " 中，本趟车不再单独判断覆盖是否充足；批次整体核对结果见批次详情。")
    return {"from_batches": batch_links, "suppress_single_alert": True,
            "message": message, "has_recon_batch": any_recon}


# ---------------------------------------------------------------- 孤儿/重复登记检测

def orphan_check(batch_links: list[dict], known_trip_nos: set,
                 legacy_prepay_trip_nos: set) -> dict:
    """安全网检测（非阻断，只提示人工核实）：

    batch_links: [{batch_id, trip_no}] 全部批次关联；
    known_trip_nos: 库内存在的班列/预估编号集合；
    legacy_prepay_trip_nos: v1 train_prepayments 登记了预付款的编号集合。

    返回 {unknown_trip_links, duplicate_risk}：
      - 批次关联了不存在的编号；
      - 同一趟车既在旧表登记预付、又被纳入参与核对的预付批次（重复计算风险）。
    """
    unknown = []
    for link in batch_links:
        if link["trip_no"] not in known_trip_nos:
            unknown.append({"batch_id": link["batch_id"],
                            "trip_no": link["trip_no"]})

    duplicate = []
    recon_trip_batches = {}      # trip_no → [batch_id]（仅参与核对的预付批次）
    for link in batch_links:
        if link.get("batch_type") != "prepay":
            continue
        if not link.get("participates", True):
            continue
        recon_trip_batches.setdefault(link["trip_no"], []).append(link["batch_id"])
    for trip_no, batches in recon_trip_batches.items():
        if trip_no in legacy_prepay_trip_nos:
            duplicate.append({"trip_no": trip_no, "batches": batches,
                              "legacy_prepay": True})

    message_parts = []
    if unknown:
        message_parts.append(
            f"{len(unknown)} 条批次关联指向不存在的班列编号")
    if duplicate:
        message_parts.append(
            f"{len(duplicate)} 趟车同时存在旧表预付款与批次记录，有重复计算风险")
    return {
        "unknown_trip_links": unknown,
        "duplicate_risk": duplicate,
        "has_issue": bool(unknown or duplicate),
        "message": "；".join(message_parts) + "，请人工核实（系统不自动改动）。"
                   if message_parts else "未发现孤儿关联或重复登记风险。",
    }
