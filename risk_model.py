# -*- coding: utf-8 -*-
"""
单证组风险评分模型（升级任务书·方向二）。

把规则引擎输出的多路检查信号汇总为一个 0-100 的综合风险分，
并给出可解释的"分数构成"分解，展示这是结构化加权推理而非黑箱。

评分设计（权重经过测试集校准，见 evaluation.py）：
  - 必需单证缺失        30 分（每多缺一份 +5）
  - 一致性/齐全性 FAIL   首个 25 分，此后每个 FAIL 递增 +5（风险叠加：多处不一致
                        意味着系统性错报而非孤立笔误，故非线性增长）
  - 语义存疑 WARNING    15 分（相似度灰色区，转人工/AI复核）
  - 路线合规 WARNING    10 分
  - 其他 WARNING         5 分（字段缺失无法比对等）
  - 总分封顶 100

风险分级（升级任务书给定）：
  0-20  低风险（绿） / 21-50 中风险（黄） / 51-100 高风险（红）
"""

from __future__ import annotations

# 与 verification_engine 中的状态常量保持一致（独立定义以避免循环导入）
STATUS_FAIL = "FAIL"
STATUS_WARNING = "WARNING"

# 检查项类别 → 基础权重
WEIGHT_MISSING_DOC = 30          # 必需单证缺失（FAIL）
WEIGHT_CONSISTENCY_FAIL = 25     # 一致性类 FAIL 的首犯权重
FAIL_ESCALATION = 5              # 每增加一个 FAIL 的递增分
WEIGHT_SEMANTIC_SUSPECT = 15     # 语义存疑 WARNING
WEIGHT_ROUTE_WARNING = 10        # 路线合规 WARNING
WEIGHT_MINOR_WARNING = 5         # 其他 WARNING

GRADE_LOW = "low"
GRADE_MEDIUM = "medium"
GRADE_HIGH = "high"

GRADE_META = {
    GRADE_LOW: {"label": "低风险", "color": "#1B5E20", "bg": "#E8F5E9"},
    GRADE_MEDIUM: {"label": "中风险", "color": "#8D6E00", "bg": "#FFF8E1"},
    GRADE_HIGH: {"label": "高风险", "color": "#B71C1C", "bg": "#FFEBEE"},
}


def grade_for_score(score: int) -> str:
    if score <= 20:
        return GRADE_LOW
    if score <= 50:
        return GRADE_MEDIUM
    return GRADE_HIGH


def _weight_for(result: dict, fail_seen: int) -> int:
    """单条检查结果对应的扣分。fail_seen 为此前已累计的 FAIL 数（用于递增）。"""
    check_id = result.get("check_id", "")
    status = result.get("status")

    if status == STATUS_FAIL:
        if check_id == "DOC-001":
            extra_docs = max(0, int(result.get("missing_docs_count", 1)) - 1)
            return WEIGHT_MISSING_DOC + extra_docs * FAIL_ESCALATION
        return WEIGHT_CONSISTENCY_FAIL + fail_seen * FAIL_ESCALATION

    if status == STATUS_WARNING:
        if check_id == "CONS-001":      # 语义存疑
            return WEIGHT_SEMANTIC_SUSPECT
        if check_id == "ROUTE-001":     # 路线合规
            return WEIGHT_ROUTE_WARNING
        return WEIGHT_MINOR_WARNING

    return 0


def compute_risk(results: list) -> dict:
    """
    输入规则引擎的 results 列表，输出：
    {
        "score": 0-100,
        "grade": "low"|"medium"|"high",
        "grade_label": "低风险"|"中风险"|"高风险",
        "breakdown": [ {check_id, check_name, status, points, reason}, ... ]  # 仅计分项
    }
    """
    breakdown = []
    score = 0
    fail_seen = 0
    for r in results:
        if r.get("status") == STATUS_FAIL:
            points = _weight_for(r, fail_seen)
            fail_seen += 1
        else:
            points = _weight_for(r, 0)
        if points <= 0:
            continue
        if r.get("status") == STATUS_FAIL and r.get("check_id") == "DOC-001":
            reason = f"必需单证缺失（缺{r.get('missing_docs_count', 1)}份）"
        else:
            reason = r.get("check_name", r.get("check_id", ""))
        breakdown.append({
            "check_id": r.get("check_id"),
            "check_name": r.get("check_name"),
            "status": r.get("status"),
            "points": points,
            "reason": reason,
        })
        score += points

    score = min(100, score)
    grade = grade_for_score(score)
    return {
        "score": score,
        "grade": grade,
        "grade_label": GRADE_META[grade]["label"],
        "breakdown": breakdown,
    }
