# -*- coding: utf-8 -*-
"""
核验引擎自测脚本（不依赖第三方库）。

用法：python selftest.py
按竞赛验收标准逐项断言三组批次的核验结果，全部通过时输出 SELFTEST PASSED。
数据核对模块v1 追加纯逻辑段（train_number/train_recon，均为标准库实现）：
  9条真实记录编号回归（含10-10临/图两条不合并的专项断言）、兜底去重后缀、
  EST预估编号、编号反解析、预付覆盖/结算差异/三方对账阈值/相邻重复值检测。
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

from verification_engine import (STATUS_FAIL, STATUS_PASS, STATUS_WARNING,
                                 run_verification, sort_results_by_severity)

import train_number
import train_recon

SAMPLE_DIR = Path(__file__).parent / "sample_data"

checkpoints = []


def check(name: str, condition: bool, detail: str = ""):
    checkpoints.append((name, bool(condition)))
    mark = "✅" if condition else "❌"
    print(f"  {mark} {name}" + (f" —— {detail}" if detail and not condition else ""))


def load(batch_id: str) -> dict:
    return json.loads((SAMPLE_DIR / f"{batch_id}.json").read_text(encoding="utf-8"))


def main() -> int:
    # ---- 批次A：干净数据，不得误报 ----
    print("== 批次A（全部通过） ==")
    v = run_verification(load("batch_clean"))
    check("0 项 FAIL", v["summary"]["fail"] == 0)
    check("0 项 WARNING", v["summary"]["warning"] == 0)
    # 14 = 结构/齐全性/字段完整性/重复单证 + 9项一致性/路线 + 5份单据各1条单据规范检查（DOC-101）
    check("全部 19 项 PASS（含结构/字段完整性/重复单证/单据规范检查）",
          v["summary"]["pass"] == v["summary"]["total"] == 19)
    # 分层结构（任务书问题四）：按单据实例分组、批次级问题为空
    check("分层结果：5份单据各自分组且均无问题",
          len(v["document_groups"]) == 5
          and all(g["fail_count"] == 0 and g["warning_count"] == 0
                  for g in v["document_groups"]))
    check("分层结果：批次级问题为空", v["batch_level_issues"] == [])

    # ---- 批次B：精确复现4类问题 ----
    print("== 批次B（含4类问题） ==")
    v = run_verification(load("batch_with_issues"))
    fails = {r["check_id"] for r in v["results"] if r["status"] == STATUS_FAIL}
    check("恰好 4 项 FAIL", v["summary"]["fail"] == 4, f"实际 {v['summary']['fail']}")
    check("0 项 WARNING", v["summary"]["warning"] == 0)
    check("含 货物描述不一致", "CONS-001" in fails)
    check("含 报关单箱数不符(475 vs 480)", "CONS-002" in fails)
    check("含 毛重超差(12500 vs 12300, >1%)", "CONS-003" in fails)
    check("含 缺少原产地证书", "DOC-001" in fails)
    desc = next(r for r in v["results"] if r["check_id"] == "CONS-001")
    check("描述FAIL内容引用两个错误值",
          "陶瓷卫浴洁具" in desc["detail"] and "卫浴陶瓷制品" in desc["detail"])
    non_pass = [r for r in v["results"] if r["status"] != STATUS_PASS]
    check("每个FAIL均有非空修正建议",
          all(r.get("suggestion") and len(r["suggestion"]) > 20 for r in non_pass))

    # ---- 批次C：路线合规 WARNING ----
    print("== 批次C（路线合规告警） ==")
    v = run_verification(load("batch_route_warning"))
    warns = [r for r in v["results"] if r["status"] == STATUS_WARNING]
    check("0 项 FAIL", v["summary"]["fail"] == 0)
    check("恰好 1 项 WARNING", len(warns) == 1)
    check("WARNING 为 运单类型-路线不匹配", warns and warns[0]["check_id"] == "ROUTE-001")
    check("WARNING 文案含 土耳其 与 SMGS",
          warns and "土耳其" in warns[0]["detail"] and "SMGS" in warns[0]["detail"])

    # ---- 建议质量抽查：建议与问题匹配（含具体值，非套话） ----
    print("== 建议内容抽查 ==")
    v = run_verification(load("batch_with_issues"))
    by_id = {r["check_id"]: r for r in v["results"]}
    check("描述建议引用以多数单证为准的品名",
          "陶瓷卫浴洁具" in by_id["CONS-001"]["suggestion"])
    check("件数建议引用 480 件基准",
          "480" in by_id["CONS-002"]["suggestion"])
    check("缺少产地证建议提到 CCPIT 补办",
          "CCPIT" in by_id["DOC-001"]["suggestion"] or "贸促会" in by_id["DOC-001"]["suggestion"])

    # ---- 升级：语义相似度分级（升级任务书·方向一） ----
    print("== 语义相似度 ==")
    desc = by_id["CONS-001"]
    check("FAIL 详情展示相似度数值", "语义相似度" in desc["detail"])
    sims = desc.get("similarities") or []
    check("结构化相似度数据存在且 <0.6（mismatch）",
          sims and sims[0]["score"] < 0.6 and sims[0]["grade"] == "mismatch", str(sims))
    suspect_batch = load("batch_with_issues")
    for doc in suspect_batch["documents"]:
        doc["fields"]["goods_description"] = "卫浴洁具陶瓷制品" if doc["doc_type"] == "export_customs_declaration" else "陶瓷卫浴洁具"
    v2 = run_verification(suspect_batch)
    desc2 = next(r for r in v2["results"] if r["check_id"] == "CONS-001")
    sim2 = (desc2.get("similarities") or [{}])[0]
    check("换序重述对落入存疑区(0.6-0.85)且整体降为 WARNING",
          desc2["status"] == STATUS_WARNING and 0.6 <= sim2.get("score", 0) < 0.85,
          f"status={desc2['status']} sim={sim2.get('score')}")

    # ---- 升级：风险评分模型（升级任务书·方向二） ----
    print("== 风险评分 ==")
    v_low = run_verification(load("batch_clean"))
    check("干净批次风险分 0 / 低风险", v_low["risk"]["score"] == 0 and v_low["risk"]["grade"] == "low")
    check("干净批次分数构成为空", v_low["risk"]["breakdown"] == [])
    v_route = run_verification(load("batch_route_warning"))
    check("路线告警批次 10 分 / 低风险且构成含路线项",
          v_route["risk"]["score"] == 10 and v_route["risk"]["grade"] == "low"
          and any("路线" in b["reason"] or "运单" in b["reason"] for b in v_route["risk"]["breakdown"]))
    risk_high = v["risk"]
    check("4问题批次 风险分≥51 / 高风险",
          risk_high["score"] >= 51 and risk_high["grade"] == "high",
          f"score={risk_high['score']}")
    check("分数构成可解释（4项扣分且权重随FAIL数量递增）",
          len(risk_high["breakdown"]) == 4
          and len({b["points"] for b in risk_high["breakdown"]}) > 1,
          str([b["points"] for b in risk_high["breakdown"]]))

    # ---- 数据核对模块v1：班列编号与核对纯逻辑（任务书回归用例） ----
    print("== 数据核对v1：班列统一编号（9条回归） ==")
    fixture = json.loads(
        (SAMPLE_DIR / "datacheck" / "datacheck_trips_v1.json").read_text(encoding="utf-8"))
    code_map = fixture["code_map"]
    existing: set[str] = set()
    generated = []
    for rec, expected in zip(fixture["records"], fixture["expected_numbers"]):
        no, suffix = train_number.build_number(
            rec["dep_date"], rec["station"], rec["port"], rec["dest"],
            rec["train_type"], code_map, existing)
        existing.add(no)
        generated.append(no)
        check(f"记录{rec['no']} {rec['dep_date']} {rec['station']}-{rec['port']}-"
              f"{rec['dest']}-{rec['train_type']} → {expected}",
              no == expected and suffix == 0, f"实际 {no}")
    check("10-10临 与 10-10图 同日同线路生成两个不同编号（不合并/不冲突）",
          "20251010-PW-MZL-RU-L" in generated and "20251010-PW-MZL-RU-T" in generated
          and len(set(generated)) == len(generated))
    dup_no, dup_suffix = train_number.build_number(
        "2025-10-10", "PW", "MZL", "RU", "T", code_map, existing)
    check("兜底去重：同主干再登记 → 追加 -01 序号后缀",
          dup_no == "20251010-PW-MZL-RU-T-01" and dup_suffix == 1, f"实际 {dup_no}")
    est_no = train_number.build_est("2025-10-15", "PW", "MZL", "RU", "T",
                                    code_map)[0]
    check("预估编号格式 EST-YYYYMMDD-…", est_no == "EST-20251015-PW-MZL-RU-T",
          f"实际 {est_no}")
    parsed = train_number.parse_number("20251010-PW-MZL-RU-T-01")
    check("编号反解析（含序号后缀/EST前缀/假日期拒绝）",
          parsed is not None and parsed["suffix"] == 1
          and train_number.parse_number(est_no)["is_est"] is True
          and train_number.parse_number("20251332-PW-MZL-RU-T") is None)

    print("== 数据核对v1：核对计算（预付/结算差异/三方阈值/重复值） ==")
    pay = train_recon.prepay_check("1000.00", [{"amount": "400"}, {"amount": 300}])
    check("预付覆盖：不足 → 需补款差额=300",
          pay["verdict"] == "shortage" and pay["diff"] == Decimal("300"))
    over = train_recon.prepay_check(1000, [{"amount": "1200"}])
    check("预付覆盖：超出 → 多付200", over["verdict"] == "overpaid"
          and over["diff"] == Decimal("-200"))
    sa = train_recon.settle_actual_check(Decimal("2666062.00"), 2660000, "")
    check("结算≠实付且无差异原因 → needs_reason 提示",
          sa["needs_reason"] is True and "待人工填写差异原因" in sa["message"])
    tw_hot = train_recon.three_way_check(
        {"dt_supply_100": 100000, "ly_advance_100": 106001, "auth_confirm_100": 99000},
        Decimal("5"), Decimal("5000"))
    tw_ok = train_recon.three_way_check(
        {"dt_supply_100": 100000, "ly_advance_100": 103000, "auth_confirm_100": 99000},
        Decimal("5"), Decimal("5000"))
    check("三方对账：6%差异触发人工关注 / 3%不触发（5%/5000元阈值）",
          tw_hot["flag"] is True and tw_ok["flag"] is False)
    sub_rows = [{"trip_no": f"R{r['no']}", "dep_date": r["dep_date"],
                 **{k: v for k, v in (r.get("subsidy") or {}).items()}}
                for r in fixture["records"]]
    dup_flags = train_recon.duplicate_neighbor_flags(sub_rows)
    dup_trips = {trip for trip, items in dup_flags.items()
                 if any(i["field"] == "ly_advance_100" for i in items)}
    check("重复值检测：恰好命中 10-05/10-10临/10-10图 三条记录的联运垫付补贴100",
          dup_trips == {"R7", "R8", "R9"}, f"实际 {sorted(dup_trips)}")

    passed = all(ok for _, ok in checkpoints)
    print(f"\n{len(checkpoints)} 项检查，{'全部通过' if passed else '存在失败项'}："
          f"{'SELFTEST PASSED' if passed else 'SELFTEST FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
