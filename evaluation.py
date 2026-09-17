# -*- coding: utf-8 -*-
"""
量化评估（升级任务书·方向四；审查整改 F10）。

评估集分两层，二者不得混用同一批数据自证：
  1. 冻结留出集 evaluation_set/*.json —— 固定文件，评估只读取、绝不重新生成或覆盖；
     文件哈希写入评估报告（可审计"权重调好后留出集没有被动过"）。
  2. 校准集 build_cases() —— 用于调整权重时可迭代，仅在 --rebuild-calibration
     时重新生成到 evaluation_set_calibration/ 目录（不影响冻结留出集）。

标注口径（统一后的地面真值定义，独立于评分权重，按检查结果状态计数）：
  low    低风险：0 个 FAIL 且 ≤1 条 WARNING
  medium 中风险：恰好 1 个 FAIL 且 ≤1 条 WARNING；或 0 个 FAIL 但 ≥2 条 WARNING
                 （多条警告叠加 = 系统性疑点，与评分模型的叠加升级原则一致）
  high   高风险：≥2 个 FAIL；或 1 个 FAIL 且 ≥2 条 WARNING
  （修复前口径"0 FAIL=低 / 恰好1 FAIL=中 / ≥2 FAIL=高"忽略了 WARNING 叠加，
   与评分模型在"0 FAIL但25分中风险""1 FAIL但55分高风险"等案例上自相矛盾，
   已按叠加升级原则统一。已知边界：1 FAIL+2条轻度WARNING(如两条5分)时
   标注判high而评分可能为medium(≤50分)，冻结集内无此类案例，出现即测试失败。）

报告指标（F10 要求）：
  - 风险分级：一致率、漏报率（模型分级低于标注）、误报率（模型分级高于标注）、
    处理失败率；
  - 字段提取（对 sample_pdfs 实跑摄取管线）：字段识别率（高置信提取/应提取）、
    待复核率（missing+review，即需人工修正）、处理失败率。

用法：
  python evaluation.py                       # 读取冻结留出集评估（不写评估集）
  python evaluation.py --rebuild-calibration # 重新生成校准集（写入 calibration 目录）
"""

import hashlib
import json
import sys
from pathlib import Path

from verification_engine import run_verification

SET_DIR = Path(__file__).parent / "evaluation_set"                 # 冻结留出集（只读）
CALIB_DIR = Path(__file__).parent / "evaluation_set_calibration"   # 校准集（可迭代）
REPORT_PATH = Path(__file__).parent / "evaluation_report.md"
SAMPLE_PDF_DIR = Path(__file__).parent / "sample_pdfs"

SEVERITY = {"low": 0, "medium": 1, "high": 2}
GRADE_CN = {"low": "低", "medium": "中", "high": "高"}

# ---------------------------------------------------------------- 基础数据（与批次A同源）

BASE_CONSIGNOR = "山西洁康陶瓷制品有限公司"
BASE_CONSIGNEE = "RHEINBAU HANDEL GMBH"
BASE_DESC = "陶瓷卫浴洁具"


def ground_truth_label(n_fail: int, n_warn: int) -> str:
    """统一后的标注口径（见模块docstring）：按FAIL/WARNING计数独立判定，
    不读取任何评分权重。"""
    if n_fail >= 2:
        return "high"
    if n_fail == 1:
        return "high" if n_warn >= 2 else "medium"
    return "medium" if n_warn >= 2 else "low"


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
    """19组校准场景：6低险 + 7中险 + 6高险（用于权重校准，可迭代）。"""
    turkey = ["中国", "哈萨克斯坦", "阿塞拜疆", "格鲁吉亚", "土耳其"]
    cases = [
        # ---------- 低风险：0 FAIL 且 ≤1 WARNING ----------
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
        # ---------- 中风险：1 FAIL（≤1 WARNING）或 0 FAIL ≥2 WARNING ----------
        case("eval07_pkg_mismatch", "medium", "报关单箱数475≠480", make_docs(pkg_customs=475)),
        case("eval08_weight_drift", "medium", "装箱单毛重12500超1%容差", make_docs(gw_packing=12500)),
        case("eval09_desc_mismatch", "medium", "报关单品名错报（语义相似度0.55）",
             make_docs(desc_customs="卫浴陶瓷制品")),
        case("eval10_missing_coo", "medium", "缺少原产地证书", make_docs(with_coo=False)),
        case("eval11_consignee_typo", "medium", "报关单收货人名称不符",
             make_docs(consignee_customs="RHEINBAU HANDEL GMBH.")),
        case("eval12_amount_mismatch", "medium", "报关金额低报1.6%",
             make_docs(amount_customs=85000.00)),
        case("eval15_coo_and_route", "medium", "缺产地证+SMGS走土耳其线（1 FAIL+1 WARNING）",
             make_docs(with_coo=False, route=turkey)),
        # ---------- 高风险：≥2 FAIL 或 1 FAIL且≥2 WARNING ----------
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


def save_cases(cases: list, target_dir: Path = None) -> None:
    """保存场景文件（仅用于校准集；冻结留出集永不调用本函数）。"""
    target_dir = target_dir or CALIB_DIR
    target_dir.mkdir(exist_ok=True)
    for c in cases:
        (target_dir / f"{c['batch_id']}.json").write_text(
            json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def load_frozen_holdout() -> list:
    """读取冻结留出集（只读；评估过程绝不写回，F10）。"""
    files = sorted(SET_DIR.glob("eval*.json"))
    if not files:
        raise SystemExit(f"冻结留出集为空：{SET_DIR}（留出集是固定文件，不允许动态生成）")
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def holdout_hashes() -> dict:
    """冻结留出集的文件哈希（写入报告，供复核比对）。"""
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:16]
            for f in sorted(SET_DIR.glob("eval*.json"))}


def evaluate_batch(c: dict) -> dict:
    """单批次评估：模型判定 + 口径判定 + 状态计数；异常计为处理失败。"""
    try:
        v = run_verification(c)
    except Exception as exc:
        return {"batch_id": c.get("batch_id", "?"), "description": c.get("description", ""),
                "stored": c.get("ground_truth"), "rubric": None, "model": None,
                "score": None, "n_fail": None, "n_warn": None, "ok": False,
                "failed": True, "error": f"{type(exc).__name__}: {exc}"}
    n_fail, n_warn = v["summary"]["fail"], v["summary"]["warning"]
    rubric = ground_truth_label(n_fail, n_warn)
    model = v["risk"]["grade"]
    return {"batch_id": c.get("batch_id", "?"), "description": c.get("description", ""),
            "stored": c.get("ground_truth"), "rubric": rubric, "model": model,
            "score": v["risk"]["score"], "n_fail": n_fail, "n_warn": n_warn,
            "ok": (stored == rubric == model) if (stored := c.get("ground_truth")) else False,
            "failed": False, "error": None}


# ---------------------------------------------------------------- 字段提取评估（F10 指标）

# sample_pdfs 各文本型单证的应提取字段（信息在文档中明确载明的字段）
PDF_FIELD_EXPECTATIONS = {
    "invoice.pdf": ["invoice_no", "consignor_name", "consignee_name",
                    "goods_description", "total_packages", "gross_weight_kg",
                    "total_amount", "currency"],
    "packing_list.pdf": ["packing_list_no", "consignor_name", "consignee_name",
                         "goods_description", "total_packages", "gross_weight_kg",
                         "container_no"],
    "waybill.pdf": ["waybill_no", "waybill_type", "consignor_name", "consignee_name",
                    "route_countries", "goods_description", "total_packages",
                    "gross_weight_kg", "container_no"],
    "customs_declaration.pdf": ["declaration_no", "consignor_name", "consignee_name",
                                "goods_description", "total_packages", "gross_weight_kg",
                                "declared_value", "destination_country", "waybill_no",
                                "container_no"],
}


def evaluate_extraction() -> dict:
    """对 sample_pdfs 实跑摄取管线，统计字段识别率/待复核率/处理失败率。
    扫描型PDF依赖本机OCR：未安装tesseract时如实标注并跳过（不计入识别率）。"""
    import pdf_ingest

    from pathlib import Path as _P
    sys_path = _P(__file__).parent
    if str(sys_path) not in __import__("sys").path:
        pass  # 同目录运行时天然可导入
    results = {"files": [], "expected": 0, "identified": 0, "review": 0,
               "failed_files": 0, "ocr_available": pdf_ingest.ocr_available()}
    for fname, expected in PDF_FIELD_EXPECTATIONS.items():
        path = SAMPLE_PDF_DIR / fname
        if not path.exists():
            continue
        r = pdf_ingest.process_pdf(path.read_bytes(), fname)
        if r.error:
            results["failed_files"] += 1
            results["files"].append({"file": fname, "error": r.error})
            continue
        high = {k for k, v in r.field_confidence.items() if v == "high"}
        flagged = {k for k, v in r.field_confidence.items() if v in ("missing", "review")}
        results["expected"] += len(expected)
        results["identified"] += len(high & set(expected))
        results["review"] += len(flagged & set(expected))
        results["files"].append({"file": fname, "identified": sorted(high & set(expected)),
                                 "flagged": sorted(flagged & set(expected))})
    results["identification_rate"] = (results["identified"] / results["expected"]
                                      if results["expected"] else 0.0)
    results["review_rate"] = (results["review"] / results["expected"]
                              if results["expected"] else 0.0)
    return results


def main() -> int:
    rebuild_requested = "--rebuild-calibration" in sys.argv
    if rebuild_requested:
        save_cases(build_cases())
        print(f"校准集已重新生成到 {CALIB_DIR.name}/（冻结留出集未受影响）")

    cases = load_frozen_holdout()
    rows, hash_map = [], holdout_hashes()
    for c in cases:
        rows.append(evaluate_batch(c))

    total = len(rows)
    agree = sum(1 for r in rows if r["ok"])
    processed = [r for r in rows if not r["failed"]]
    failed_rate = (total - len(processed)) / total if total else 0.0
    # 漏报：模型分级低于标注（该拦的没拦住）；误报：模型分级高于标注
    under = sum(1 for r in processed if r["stored"] and r["model"]
                and SEVERITY[r["model"]] < SEVERITY[r["stored"]])
    over = sum(1 for r in processed if r["stored"] and r["model"]
               and SEVERITY[r["model"]] > SEVERITY[r["stored"]])

    print("=" * 100)
    print(f"{'批次':30s}{'标注':5s}{'口径':5s}{'模型':5s}{'风险分':6s}{'FAIL':5s}{'WARN':5s}一致")
    print("-" * 100)
    for r in rows:
        mark = "✅" if r["ok"] else ("💀" if r["failed"] else "❌")
        print(f"{r['batch_id']:30s}{GRADE_CN.get(r['stored'], '?'):5s}"
              f"{GRADE_CN.get(r['rubric'], '?'):5s}{GRADE_CN.get(r['model'], '?'):5s}"
              f"{str(r['score']):6s}{str(r['n_fail']):5s}{str(r['n_warn']):5s}{mark}")
    print("-" * 100)
    rate = agree / total * 100 if total else 0.0
    print(f"一致率（标注=口径=模型三方一致）：{agree}/{total} = {rate:.0f}%")
    print(f"漏报率（模型低于标注）：{under}/{len(processed)} = {under / len(processed) * 100:.1f}%"
          if processed else "漏报率：n/a")
    print(f"误报率（模型高于标注）：{over}/{len(processed)} = {over / len(processed) * 100:.1f}%"
          if processed else "误报率：n/a")
    print(f"处理失败率：{total - len(processed)}/{total} = {failed_rate * 100:.1f}%")

    # ---- 字段提取指标（对 sample_pdfs 实跑） ----
    ext = evaluate_extraction()
    print("-" * 100)
    print(f"字段识别率（高置信提取/应提取，文本型PDF实跑）："
          f"{ext['identified']}/{ext['expected']} = {ext['identification_rate'] * 100:.1f}%")
    print(f"字段待复核率（提取失败/口径存疑，需人工修正）：{ext['review']}/{ext['expected']}"
          f" = {ext['review_rate'] * 100:.1f}%")
    print(f"文件处理失败率（PDF）：{ext['failed_files']}/{len(PDF_FIELD_EXPECTATIONS)}；"
          f"OCR环境可用：{ext['ocr_available']}")

    # ---- markdown 报告 ----
    n_low = sum(1 for r in rows if r["stored"] == "low")
    n_med = sum(1 for r in rows if r["stored"] == "medium")
    n_high = sum(1 for r in rows if r["stored"] == "high")
    lines = [
        "# 风险分级量化评估报告", "",
        f"- 评估集：**冻结留出集** {total} 组（低/中/高 = {n_low}/{n_med}/{n_high}），"
        f"评估只读取、不重新生成不覆盖；文件SHA256前16位见文末，供复核比对",
        "- 标注口径（统一后，独立于评分权重）：低=0 FAIL且≤1 WARNING；"
        "中=恰好1 FAIL且≤1 WARNING，或0 FAIL且≥2 WARNING（叠加升级）；"
        "高=≥2 FAIL，或1 FAIL且≥2 WARNING",
        "- 校准集：`build_cases()` 生成于 `evaluation_set_calibration/`（权重迭代用，"
        "与留出集分离，不参与本报告一致率）", "",
        f"## 核心指标", "",
        f"| 指标 | 数值 |",
        f"|---|---|",
        f"| 分级一致率（标注=口径=模型） | {agree}/{total} = {rate:.0f}% |",
        f"| 漏报率（模型分级低于标注） | {under}/{len(processed)} |",
        f"| 误报率（模型分级高于标注） | {over}/{len(processed)} |",
        f"| 处理失败率 | {total - len(processed)}/{total} |",
        f"| 字段识别率（文本型PDF实跑） | {ext['identified']}/{ext['expected']} = "
        f"{ext['identification_rate'] * 100:.1f}% |",
        f"| 字段待复核率（需人工修正） | {ext['review']}/{ext['expected']} = "
        f"{ext['review_rate'] * 100:.1f}% |",
        f"| PDF处理失败率 | {ext['failed_files']}/{len(PDF_FIELD_EXPECTATIONS)} |",
        "", "## 逐批次判定", "",
        "| 批次 | 场景 | 人工标注 | 口径判定 | 模型判定 | 风险分 | FAIL | WARNING | 一致 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r, c in zip(rows, cases):
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"| {r['batch_id']} | {c['description']} | {GRADE_CN.get(r['stored'], '?')} "
                     f"| {GRADE_CN.get(r['rubric'], '?')} | {GRADE_CN.get(r['model'], '?')} "
                     f"| {r['score']} | {r['n_fail']} | {r['n_warn']} | {mark} |")
    lines += ["", "## 冻结留出集哈希（审计用）", "", "```",
              *[f"{k}: {v}" for k, v in hash_map.items()], "```", "",
              "## 诚实性边界", "",
              "- 本评估基于模拟单证批次，19组一致率**不代表真实单证或真实OCR的准确率**；",
              "- 真实业务准入需用真实脱敏单证（覆盖不同模板/语言/格式/扫描质量/异常组合）"
              "由业务人员独立标注后另行验证；",
              "- 标注口径与评分模型为两套实现：口径只看FAIL/WARNING计数，模型按权重打分，"
              "二者在冻结集上必须一致（不一致时评估退出码为1）；"
              "已知理论边界：1 FAIL+2条轻度WARNING(各5分)时口径判high而评分≤50分为medium，"
              "冻结集内无此类案例，出现即判失败并需重新校准。"]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n已写入 evaluation_report.md")
    return 0 if agree == total and not failed_rate else 1


if __name__ == "__main__":
    sys.exit(main())
