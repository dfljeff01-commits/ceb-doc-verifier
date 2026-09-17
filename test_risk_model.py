# -*- coding: utf-8 -*-
"""risk_model 与 semantic 模块单元测试。

覆盖：风险权重/递增/封顶/分级边界、语义相似度分级边界（0.6/0.85 附近）、后端确定性。
运行：pytest test_risk_model.py test_semantic.py -v  （或直接 pytest -v）
"""

import pytest

from risk_model import (FAIL_ESCALATION, GRADE_HIGH, GRADE_LOW, GRADE_MEDIUM,
                        WEIGHT_CONSISTENCY_FAIL, WEIGHT_MISSING_DOC,
                        WEIGHT_ROUTE_WARNING, WEIGHT_SEMANTIC_SUSPECT,
                        compute_risk, grade_for_score)
from semantic import grade_similarity, similarity
from verification_engine import _make_result

# ---------------------------------------------------------------- risk_model


def res(check_id, status, name=None, **extra):
    return _make_result(check_id, "t", name or f"检查{check_id}", status, "d", [], **extra)


def test_clean_results_score_zero():
    risk = compute_risk([res("CONS-001", "PASS"), res("ROUTE-001", "PASS")])
    assert risk["score"] == 0 and risk["grade"] == GRADE_LOW and risk["breakdown"] == []


def test_single_consistency_fail_is_medium():
    risk = compute_risk([res("CONS-002", "FAIL")])
    assert risk["score"] == WEIGHT_CONSISTENCY_FAIL == 25
    assert risk["grade"] == GRADE_MEDIUM


def test_two_fails_escalate_to_high():
    """风险叠加：第2个FAIL在基础分上递增，两处硬伤即高风险。"""
    risk = compute_risk([res("CONS-002", "FAIL"), res("CONS-003", "FAIL")])
    assert risk["score"] == 25 + (25 + FAIL_ESCALATION) == 55
    assert risk["grade"] == GRADE_HIGH


def test_missing_doc_weight_and_extra_docs():
    r1 = res("DOC-001", "FAIL", missing_docs_count=1)
    r3 = res("DOC-001", "FAIL", missing_docs_count=3)
    assert compute_risk([r1])["score"] == WEIGHT_MISSING_DOC == 30
    assert compute_risk([r3])["score"] == 30 + 2 * FAIL_ESCALATION == 40


def test_warning_weights_by_category():
    assert compute_risk([res("CONS-001", "WARNING")])["score"] == WEIGHT_SEMANTIC_SUSPECT == 15
    assert compute_risk([res("ROUTE-001", "WARNING")])["score"] == WEIGHT_ROUTE_WARNING == 10
    assert compute_risk([res("CONS-006", "WARNING")])["score"] == 5


def test_score_capped_at_100():
    fails = [res(f"CONS-00{i}", "FAIL") for i in range(2, 7)]  # 5个FAIL：25+30+35+40+45=175
    assert compute_risk(fails)["score"] == 100


def test_grade_boundaries():
    assert grade_for_score(0) == GRADE_LOW
    assert grade_for_score(20) == GRADE_LOW
    assert grade_for_score(21) == GRADE_MEDIUM
    assert grade_for_score(50) == GRADE_MEDIUM
    assert grade_for_score(51) == GRADE_HIGH
    assert grade_for_score(100) == GRADE_HIGH


def test_breakdown_is_explainable():
    risk = compute_risk([res("DOC-001", "FAIL", missing_docs_count=1, name="必需单证齐全性检查"),
                         res("CONS-002", "FAIL", name="件数一致性")])
    reasons = [(b["reason"], b["points"]) for b in risk["breakdown"]]
    assert len(reasons) == 2
    # 分数构成可逐项对应到检查项与分值（缺单证30；件数为第2个FAIL，25+5递增=30）
    assert any("必需单证" in r and p == 30 for r, p in reasons)
    assert any("件数" in r and p == 30 for r, p in reasons)


# ---------------------------------------------------------------- semantic


@pytest.mark.parametrize("score,expected", [
    (1.0, "match"), (0.85, "match"), (0.8499, "suspect"),
    (0.6, "suspect"), (0.5999, "mismatch"), (0.0, "mismatch"),
])
def test_similarity_grade_boundaries(score, expected):
    assert grade_similarity(score) == expected


def test_identical_text_scores_one():
    assert similarity("陶瓷卫浴洁具", "陶瓷卫浴洁具") == 1.0


def test_punctuation_and_case_normalized():
    assert similarity("陶瓷,卫浴洁具！", "陶瓷卫浴洁具") == 1.0
    assert similarity("ABC GBH", "abc gbh") == 1.0


def test_known_pairs_land_in_expected_bands():
    """回归锚点：错报对<mismatch，换序重述对>suspect，无关对<mismatch。"""
    assert similarity("陶瓷卫浴洁具", "卫浴陶瓷制品") < 0.6          # P0批次B错报对
    assert 0.6 <= similarity("陶瓷卫浴洁具", "卫浴洁具陶瓷制品") < 0.85  # 灰色区
    assert similarity("陶瓷卫浴洁具", "日用陶瓷餐具") < 0.6
    assert similarity("陶瓷卫浴洁具", "bathroom sanitary ware") < 0.6  # 跨语言为退化方案已知局限


def test_backend_deterministic():
    a, b = "陶瓷卫浴洁具", "卫浴洁具陶瓷制品"
    assert similarity(a, b) == similarity(a, b)


# ---------------------------------------------------------------- F10：标注口径统一与冻结留出集

import hashlib
import json as _json
from pathlib import Path as _Path

import evaluation as _evaluation
from verification_engine import run_verification as _run_v


def test_ground_truth_label_rubric():
    """统一后的标注口径：按FAIL/WARNING计数判定（独立于评分权重）。"""
    gt = _evaluation.ground_truth_label
    assert gt(0, 0) == GRADE_LOW
    assert gt(0, 1) == GRADE_LOW
    assert gt(0, 2) == GRADE_MEDIUM          # 0 FAIL 但 2 条 WARNING = 中风险（审查反例）
    assert gt(1, 0) == GRADE_MEDIUM
    assert gt(1, 1) == GRADE_MEDIUM
    assert gt(1, 2) == GRADE_HIGH            # 1 FAIL + 2 WARNING = 高风险（审查反例）
    assert gt(2, 0) == GRADE_HIGH
    assert gt(3, 5) == GRADE_HIGH


def test_frozen_holdout_labels_match_rubric_and_model():
    """逐一核对冻结留出集：存储标注 = 口径判定 = 模型判定，三方一致（修复前矛盾）。"""
    cases = _evaluation.load_frozen_holdout()
    assert len(cases) == 19
    for c in cases:
        v = _run_v(c)
        n_fail, n_warn = v["summary"]["fail"], v["summary"]["warning"]
        rubric = _evaluation.ground_truth_label(n_fail, n_warn)
        assert c["ground_truth"] == rubric == v["risk"]["grade"], (
            f"{c['batch_id']}: stored={c['ground_truth']} rubric={rubric} "
            f"model={v['risk']['grade']} (FAIL={n_fail}, WARN={n_warn})")


def test_review_counterexamples_now_aligned():
    """审查指出的两个矛盾案例在新口径下对齐：
    0 FAIL+2 WARNING(25分)→中；1 FAIL+2 WARNING(55分)→高。"""
    from verification_engine import run_verification
    turkey = ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]
    case1_docs = _evaluation.make_docs(desc_customs="卫浴洁具陶瓷制品", route=turkey)
    v1 = run_verification({"documents": case1_docs})
    assert v1["summary"]["fail"] == 0 and v1["summary"]["warning"] == 2
    assert v1["risk"]["score"] == 25
    assert _evaluation.ground_truth_label(v1["summary"]["fail"],
                                          v1["summary"]["warning"]) == "medium"
    assert v1["risk"]["grade"] == "medium"
    case2_docs = _evaluation.make_docs(desc_customs="卫浴洁具陶瓷制品", route=turkey,
                                       with_coo=False)
    v2 = run_verification({"documents": case2_docs})
    assert v2["summary"]["fail"] == 1 and v2["summary"]["warning"] == 2
    assert v2["risk"]["score"] == 55
    assert _evaluation.ground_truth_label(v2["summary"]["fail"],
                                          v2["summary"]["warning"]) == "high"
    assert v2["risk"]["grade"] == "high"


def test_evaluation_never_overwrites_frozen_holdout(tmp_path, monkeypatch):
    """评估运行只读取冻结留出集：前后文件哈希一致，且支持校准集分离生成。"""
    holdout = _Path(_evaluation.__file__).parent / "evaluation_set"
    before = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
              for f in sorted(holdout.glob("eval*.json"))}
    assert len(before) == 19
    # 屏蔽报告写入对断言无影响；main() 正常跑完
    assert _evaluation.main() == 0
    after = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(holdout.glob("eval*.json"))}
    assert before == after
    # 校准集生成写独立目录，不触碰留出集
    monkeypatch.setattr(_evaluation, "CALIB_DIR", tmp_path / "calib")
    _evaluation.save_cases(_evaluation.build_cases())
    assert (tmp_path / "calib" / "eval01_clean.json").exists()
    assert {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(holdout.glob("eval*.json"))} == before


def test_evaluation_report_contains_required_metrics():
    """评估报告必须输出一致率/漏报率/误报率/处理失败率/字段识别率/待复核率。"""
    _evaluation.main()
    report = (_Path(_evaluation.__file__).parent / "evaluation_report.md").read_text(
        encoding="utf-8")
    for token in ("一致率", "漏报率", "误报率", "处理失败率", "字段识别率", "待复核率",
                  "冻结留出集", "SHA256"):
        assert token in report, f"报告缺少指标：{token}"
