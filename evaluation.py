# -*- coding: utf-8 -*-
"""
量化评估（升级任务书·方向四）：18组模拟单证批次的风险分级一致率评估。

地面真值（ground truth）标注口径 —— 在运行模型之前独立确定，与模型权重无关：
  low    低风险：0 个 FAIL 级问题（允许存在 WARNING 级提示，如路线合规、语义存疑）
  medium 中风险：恰好 1 个 FAIL 级问题（单一硬伤：一处数值不符/缺一份必需单证等）
  high   高风险：≥2 个 FAIL 级问题（多处硬伤叠加，系统性错报）

用法：python evaluation.py
输出：逐批次判定表 + 总体一致率，并写入 evaluation_report.md（可直接贴进演示材料）。
"""

import json
import sys
from pathlib import Path

from verification_engine import run_verification

SET_DIR = Path(__file__).parent / "evaluation_set"

# ---------------------------------------------------------------- 基础数据（与批次A同源）

BASE_CONSIGNOR = "山西洁康陶瓷制品有限公司"
BASE_CONSIGNEE = "RHEINBAU HANDEL GMBH"
BASE_DESC = "陶瓷卫浴洁具"


def make_docs(
    consignee=BASE_CONSIGNEE,
    desc_customs=BASE_DESC,
    pkg_customs=480,
    gw_packing=12300,
    amount_customs=86400.00,
    consignee_customs=None,
    waybill_type="SMGS国际货协运单",
    route=None,
    with_coo=True,
    gw_fields="all",
) -> list:
    """生成一套单证；通过参数注入单点扰动。"""
    route = route or ["中国", "哈萨克斯坦", "俄罗斯", "白俄罗斯", "波兰", "德国"]
    packing = {
        "doc_type": "packing_list", "doc_id": "PL-EVAL-001", "title": "装箱单 Packing List",
        "fields": {
            "packing_list_no": "PL-EVAL-001", "consignor_name": BASE_CONSIGNOR,
            "consignee_name": consignee, "goods_description": BASE_DESC,
            "total_packages": 480, "net_weight_kg": 10800, "container_no": "MSKU8765432",
        },
    }
    if gw_fields != "only_invoice":
        packing["fields"]["gross_weight_kg"] = gw_packing
    waybill_gw = None if gw_fields == "only_invoice" else 12300
    docs = [
        {"doc_type": "invoice", "doc_id": "INV-EVAL-001", "title": "商业发票 Commercial Invoice",
         "fields": {"invoice_no": "INV-EVAL-001", "consignor_name": BASE_CONSIGNOR,
                    "consignee_name": consignee, "goods_description": BASE_DESC,
                    "total_packages": 480, "gross_weight_kg": 12300,
                    "total_amount": 86400.00, "currency": "USD"}},
        packing,
        {"doc_type": "railway_waybill", "doc_id": "SMU-EVAL-001", "title": "国际铁路运单 Railway Consignment Note",
         "fields": {"waybill_no": "SMU/EVAL/2026", "waybill_type": waybill_type,
                    "consignor_name": BASE_CONSIGNOR, "consignee_name": consignee,
                    "departure_station": "中国 西安新筑站", "destination_station": "德国 杜伊斯堡 DIT场站",
                    "route_countries": route, "goods_description": BASE_DESC,
                    "total_packages": 480, "container_no": "MSKU8765432"}},
        {"doc_type": "export_customs_declaration", "doc_id": "DEC-EVAL-001", "title": "出口报关单 Export Customs Declaration",
         "fields": {"declaration_no": "29152026000199999", "consignor_name": BASE_CONSIGNOR,
                    "consignee_name": consignee_customs or consignee,
                    "goods_description": desc_customs, "total_packages": pkg_customs,
                    "declared_value": amount_customs,
                    "currency": "USD", "waybill_no": "SMU/EVAL/2026",
                    "container_no": "MSKU8765432", "departure_country": "中国",
                    "destination_country": route[-1]}},
    ]
    if waybill_gw is not None:
        docs[2]["fields"]["gross_weight_kg"] = waybill_gw
    if gw_fields != "only_invoice":
        docs[3]["fields"]["gross_weight_kg"] = 12300
    if with_coo:
        docs.append({"doc_type": "certificate_of_origin", "doc_id": "COO-EVAL-001",
                     "title": "原产地证书 Certificate of Origin",
                     "fields": {"co_no": "CCPIT-EVAL-001", "issuer": "中国国际贸易促进委员会（CCPIT）",
                                "consignor_name": BASE_CONSIGNOR, "consignee_name": consignee,
                                "goods_description": BASE_DESC, "total_packages": 480}})
    return docs


def case(batch_id, ground_truth, description, docs) -> dict:
    return {
        "batch_id": batch_id, "batch_name": batch_id, "description": description,
        "ground_truth": ground_truth, "documents": docs,
    }


def build_cases() -> list:
    """19组批次：6低险（干净/仅WARNING） + 7中险（恰好1个FAIL） + 6高险（≥2个FAIL）。"""
    turkey = ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]
    cases = [
        # ---------- 低风险：0 FAIL ----------
        case("eval01_clean", "low", "全套单证完全一致、齐全", make_docs()),
        case("eval02_route_smgs_turkey", "low", "SMGS运单走土耳其线（路线合规WARNING）",
             make_docs(route=turkey)),
        case("eval03_route_composite_ok", "low", "CIM/SMGS统一运单走土耳其线（合规）",
             make_docs(route=turkey, waybill_type="CIM/SMGS统一运单")),
        case("eval04_desc_suspect", "low", "报关单品名换序重述（语义存疑0.78）",
             make_docs(desc_customs="卫浴洁具陶瓷制品")),
        case("eval05_cim_on_cis_route", "low", "纯CIM运单经独联体段（覆盖WARNING）",
             make_docs(waybill_type="CIM国际铁路运单")),
        case("eval06_weight_only_invoice", "low", "毛重栏仅发票载明（可比对单证不足WARNING）",
             make_docs(gw_fields="only_invoice")),
        # ---------- 中风险：恰好 1 FAIL ----------
        case("eval07_pkg_mismatch", "medium", "报关单箱数475≠480", make_docs(pkg_customs=475)),
        case("eval08_weight_drift", "medium", "装箱单毛重12500超1%容差", make_docs(gw_packing=12500)),
        case("eval09_desc_mismatch", "medium", "报关单品名错报（语义相似度0.55）",
             make_docs(desc_customs="卫浴陶瓷制品")),
        case("eval10_missing_coo", "medium", "缺少原产地证书", make_docs(with_coo=False)),
        case("eval11_consignee_typo", "medium", "报关单收货人名称不符",
             make_docs(consignee_customs="RHEINBAU HANDEL GMBH.")),
        case("eval12_amount_mismatch", "medium", "报关金额低报1.6%",
             make_docs(amount_customs=85000.00)),
        case("eval15_coo_and_route", "medium", "缺产地证+SMGS走土耳其线（1 FAIL+1 WARNING，边界批次）",
             make_docs(with_coo=False, route=turkey)),
        # ---------- 高风险：≥2 FAIL ----------
        case("eval13_pkg_and_weight", "high", "箱数+毛重双不符",
             make_docs(pkg_customs=475, gw_packing=12500)),
        case("eval14_desc_and_pkg", "high", "品名错报+箱数不符",
             make_docs(desc_customs="卫浴陶瓷制品", pkg_customs=475)),
        case("eval16_full_mess", "high", "品名+箱数+毛重+缺产地证四重问题",
             make_docs(desc_customs="卫浴陶瓷制品", pkg_customs=475, gw_packing=12500, with_coo=False)),
        case("eval17_party_and_waybillno", "high", "收货人不符+运单号交叉不符",
             make_docs(consignee_customs="RHEINBAU HANDEL GMBH.")),
        case("eval18_container_and_amount", "high", "箱号不符+金额低报",
             make_docs(amount_customs=85000.00)),
        case("eval19_desc_and_coo", "high", "品名错报+缺产地证",
             make_docs(desc_customs="卫浴陶瓷制品", with_coo=False)),
    ]
    # eval17 需要额外注入运单号不一致：直接改报关单
    for doc in cases[16]["documents"]:
        if doc["doc_type"] == "export_customs_declaration":
            doc["fields"]["waybill_no"] = "SMU/W-RONG/2026"
    # eval18 注入箱号不一致
    for doc in cases[17]["documents"]:
        if doc["doc_type"] == "export_customs_declaration":
            doc["fields"]["container_no"] = "TCLU9999999"
    return cases


def save_cases(cases: list) -> None:
    SET_DIR.mkdir(exist_ok=True)
    for c in cases:
        (SET_DIR / f"{c['batch_id']}.json").write_text(
            json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    cases = build_cases()
    save_cases(cases)

    rows, agree = [], 0
    for c in cases:
        v = run_verification(c)
        model_grade = v["risk"]["grade"]
        ok = model_grade == c["ground_truth"]
        agree += ok
        n_fail = v["summary"]["fail"]
        n_warn = v["summary"]["warning"]
        rows.append((c["batch_id"], c["ground_truth"], model_grade,
                     v["risk"]["score"], n_fail, n_warn, "✅" if ok else "❌"))

    grade_cn = {"low": "低", "medium": "中", "high": "高"}
    print("=" * 88)
    print(f"{'批次':28s}{'标注':6s}{'模型判定':8s}{'风险分':6s}{'FAIL':6s}{'WARN':6s}一致")
    print("-" * 88)
    for batch_id, gt, mg, score, nf, nw, mark in rows:
        print(f"{batch_id:28s}{grade_cn[gt]:6s}{grade_cn[mg]:8s}{score:<8d}{nf:<6d}{nw:<6d}{mark}")
    print("-" * 88)
    rate = agree / len(rows) * 100
    print(f"一致率：{agree}/{len(rows)} = {rate:.0f}%")

    # 输出 markdown 报告
    lines = [
        "# 风险分级量化评估报告", "",
        f"- 测试集：{len(rows)} 组模拟单证批次（低/中/高 = "
        f"{sum(1 for r in rows if r[1] == 'low')}/{sum(1 for r in rows if r[1] == 'medium')}/"
        f"{sum(1 for r in rows if r[1] == 'high')}）",
        "- 标注口径（独立于模型权重预先确定）：0 个 FAIL=低风险；恰好 1 个 FAIL=中风险；≥2 个 FAIL=高风险",
        f"- **模型判定与人工标注一致率：{agree}/{len(rows)} = {rate:.0f}%**", "",
        "| 批次 | 场景 | 人工标注 | 模型判定 | 风险分 | FAIL | WARNING | 一致 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (batch_id, gt, mg, score, nf, nw, mark), c in zip(rows, cases):
        lines.append(f"| {batch_id} | {c['description']} | {grade_cn[gt]} | {grade_cn[mg]} "
                     f"| {score} | {nf} | {nw} | {mark} |")
    Path(__file__).parent.joinpath("evaluation_report.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print("\n已写入 evaluation_report.md")
    return 0 if agree == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
