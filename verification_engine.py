# -*- coding: utf-8 -*-
"""
中欧班列单证核验规则引擎（Demo 版）。

职责：对一组"模拟OCR提取结果"的单证数据做四类核验：
  0. 输入完整性  —— 批次结构合法（dict/fields 类型契约，修复 F08 崩溃路径）
  1. 单证齐全性  —— 必需单证是否齐全（发票/装箱单/铁路运单/出口报关单/原产地证）、
                    单证字段完整性（空单证/缺编号）、同类型重复单证冲突（修复 F02）
  2. 字段一致性  —— 货物描述（语义相似度+关键实体守卫）、件数（整数契约）、
                    毛重（1%容差+数值合法性）、收发货人、运单号/箱号、
                    金额（0.5%容差+币种一致性）（修复 F01）
  3. 路线合规性  —— 运单类型限定 SMGS/CIM/统一运单枚举（枚举外判"类型不支持"）、
                    覆盖集合按运单类型取对应集合/并集、集合外国家触发告警，
                    报关单起运/目的国与运单路线端点一致性（修复 F07）；
                    每条路线判断结果附带规则版本与覆盖范围说明字段。

本引擎为纯标准库实现，不依赖 streamlit / pandas，便于单独自测与后续接入真实OCR。
规则为竞赛演示用简化版，不代表实际铁路/海关作业口径。
诚实性边界：路线规则仅覆盖中欧班列国际铁路联运场景，非全球通用合规判断。

对外主入口：run_verification(batch: dict) -> dict
"""

from __future__ import annotations

import re

import doc_contract
import doc_rules
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
    "smgs_rail_waybill": "国际货协运单（СМГС SMGS Rail Waybill）",
    "export_customs_declaration": "出口报关单（Export Customs Declaration）",
    "certificate_of_origin": "原产地证书（Certificate of Origin）",
}

DOC_TYPE_NAMES = {**REQUIRED_DOC_NAMES, "unknown": "未识别单证（需人工指定）"}

# 单据实例短名（"运单#1"式标签，任务书问题四：结果定位到"哪份单据"）
DOC_TYPE_SHORT = {
    "invoice": "发票",
    "packing_list": "装箱单",
    "railway_waybill": "运单",
    "smgs_rail_waybill": "运单",
    "export_customs_declaration": "报关单",
    "certificate_of_origin": "产地证",
    "unknown": "单据",
}

# 检查项对应的主字段（任务书问题四：每条问题标注"哪个字段"）
CHECK_FIELD = {
    "CONS-001": "goods_description",
    "CONS-002": "total_packages",
    "CONS-003": "gross_weight_kg",
    "CONS-004": "consignor_name",
    "CONS-005": "consignee_name",
    "CONS-006": "waybill_no",
    "CONS-007": "container_no",
    "CONS-008": "total_amount",
    "ROUTE-002": "destination_country",
}

# 兼容旧引用：覆盖集合统一定义在 doc_contract（修复 F07 后的唯一事实来源）
SMGS_COUNTRIES = doc_contract.SMGS_COUNTRIES
CIM_COUNTRIES = doc_contract.CIM_COUNTRIES

# 路线规则元信息（F07：每条路线判断结果必须附带版本与覆盖范围）
ROUTE_RULE_VERSION = doc_contract.ROUTE_RULE_VERSION
ROUTE_RULE_COVERAGE = doc_contract.ROUTE_RULE_COVERAGE
_ROUTE_RULE_NOTE = (f"【适用边界：中欧班列国际铁路联运（SMGS {len(SMGS_COUNTRIES)}国/"
                    f"CIM {len(CIM_COUNTRIES)}国），非全球通用合规判断；"
                    f"规则版本 {ROUTE_RULE_VERSION}】")

# 关键实体矛盾类型的中文说明（F03）
_GUARD_LABELS = {
    "numeric_spec": "规格数字/单位不一致",
    "bare_number": "数字不一致",
    "negation": "否定表述不一致",
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


def build_instance_labels(documents: list) -> dict:
    """单据实例标签（任务书问题四）：按类型出现顺序编号，
    如 {doc_id: "运单#1"}。同类型多份（多车厢/分批）由此可区分。"""
    counters: dict = {}
    labels: dict = {}
    for doc in documents:
        dtype = doc.get("doc_type") or "unknown"
        counters[dtype] = counters.get(dtype, 0) + 1
        doc_id = doc.get("doc_id") or f"{dtype}-{counters[dtype]}"
        labels[doc_id] = f"{DOC_TYPE_SHORT.get(dtype, dtype)}#{counters[dtype]}"
    return labels


# ---------------------------------------------------------------- 输入结构检查（F08）


def check_input_structure(raw_documents) -> dict:
    """批次结构契约检查：documents 元素必须为对象且 fields 为对象。
    非法输入在此显式 FAIL，而不是让某个检查项抛异常变成 500。"""
    issues = doc_contract.structural_issues(raw_documents)
    _, dropped = doc_contract.sanitize_documents(raw_documents)
    if issues or dropped:
        detail = "；".join(issues[:6])
        if dropped:
            detail = (detail + "；" if detail else "") + "已剔除非法单证：" + "、".join(dropped[:6])
        return _make_result(
            "SYS-001", "输入完整性", "批次数据结构检查", STATUS_FAIL,
            f"输入数据结构异常：{detail}", [],
            "请修正请求中标注字段的类型后重试（API 调用方对结构错误会收到 422 提示）。")
    return _make_result(
        "SYS-001", "输入完整性", "批次数据结构检查", STATUS_PASS,
        f"批次结构合法（{len(raw_documents)}份单证，字段结构完整）。", [], None)


# ---------------------------------------------------------------- 齐全性检查


def check_completeness(documents: list) -> dict:
    present = {doc.get("doc_type") for doc in documents}
    # SMGS运单是铁路运单的一种版式：任一运单类型即满足"铁路运单"席位（任务书A2）
    if present & set(doc_contract.RAIL_WAYBILL_TYPES):
        present.add("railway_waybill")
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


def check_field_completeness(documents: list) -> dict:
    """单证字段完整性（修复 F02）：空单证不得静默全绿；
    缺身份编号字段、类型无法识别的单证显式转人工。
    involved_docs 记录问题所属单据（任务书问题四：问题可归到"哪份单据"）。"""
    problems = []   # (status, 单证名, 问题, doc_id)
    involved = []
    for doc in documents:
        dtype = doc.get("doc_type") or "unknown"
        fields = doc.get("fields") or {}
        name = _doc_name(doc)
        doc_id = doc.get("doc_id")
        if dtype == "unknown":
            problems.append((STATUS_WARNING, name, "单证类型无法识别，需人工指定类型后再核验", doc_id))
            continue
        if not fields:
            problems.append((STATUS_FAIL, name, "空单证（未提取到任何字段），无法核验", doc_id))
            continue
        id_field = doc_contract.IDENTITY_FIELDS.get(dtype)
        if id_field and _field(doc, id_field) is None:
            label = doc_contract.FIELD_LABELS_ZH.get(id_field, id_field)
            status_id = doc_contract.field_status(doc, id_field)
            if status_id == doc_contract.FIELD_BUSINESS_MISSING:
                problems.append((STATUS_FAIL, name,
                                 f"已人工确认缺失单证编号（{label}）", doc_id))
            elif status_id == doc_contract.FIELD_NEEDS_REVIEW:
                problems.append((STATUS_WARNING, name,
                                 f"单证编号（{label}）识别置信度低，待人工确认", doc_id))
            else:
                # not_found：未找到候选 ≠ 业务缺失（P0任务书A1），措辞中性
                problems.append((STATUS_WARNING, name,
                                 f"未提取到单证编号（{label}）——可能是未识别到，"
                                 f"请补录或人工确认", doc_id))
    if problems:
        involved = [p[3] for p in problems if p[3]]
        worst = STATUS_FAIL if any(p[0] == STATUS_FAIL for p in problems) else STATUS_WARNING
        detail = "；".join(f"{name}：{msg}" for _, name, msg, _ in problems)
        return _make_result(
            "DOC-002", "单证齐全性", "单证字段完整性检查", worst, detail,
            involved, "请在预览区人工补齐缺失字段（或指定单证类型）后重新核验；空单证不得进入放行流程。")
    return _make_result(
        "DOC-002", "单证齐全性", "单证字段完整性检查", STATUS_PASS,
        f"各单证字段完整（含单证编号，共{len(documents)}份）。", [], None)


def check_duplicate_documents(documents: list, declared_composition: dict | None = None) -> dict:
    """同类型重复单证检查（修复 F02）：重复类型必须显式覆盖——
    字段冲突判 FAIL（不再只采信第一份）；字段完全一致也提示去重。

    声明构成感知（任务书问题一/二）：用户在第1步申报"运单3份"（多车厢/分批）
    时，该类型份数不超过申报数即为合法实例，不判重复；超过申报数仍按重复
    逻辑处理。未申报时维持原有口径（同类型多份即重复）。"""
    declared = {t: n for t, n in (declared_composition or {}).items()
                if isinstance(n, int) and n > 0}
    groups: dict = {}
    for doc in documents:
        dtype = doc.get("doc_type")
        if dtype:
            groups.setdefault(dtype, []).append(doc)
    legit_multi = {t: n for t, n in declared.items()
                   if n > 1 and len(groups.get(t, [])) <= n}
    dups = {t: ds for t, ds in groups.items() if len(ds) > declared.get(t, 1)}
    if not dups:
        if legit_multi:
            notes = "、".join(f"{doc_contract.DOC_TYPE_LABELS.get(t, t)}×{n}"
                              for t, n in sorted(legit_multi.items()))
            return _make_result(
                "DOC-003", "单证齐全性", "单证重复与冲突检查", STATUS_PASS,
                f"同类型多份单证（{notes}）与申报构成一致，属合法多实例"
                f"（多车厢/分批场景），已按独立单据分别核验。", [], None)
        return _make_result(
            "DOC-003", "单证齐全性", "单证重复与冲突检查", STATUS_PASS,
            "无同类型重复单证。", [], None)
    conflicts, identical = [], []
    dup_doc_ids = []
    for dtype, ds in dups.items():
        type_name = doc_contract.DOC_TYPE_LABELS.get(dtype, dtype)
        dup_doc_ids.extend(d.get("doc_id") for d in ds)
        shared = set((ds[0].get("fields") or {}).keys())
        for other in ds[1:]:
            shared &= set((other.get("fields") or {}).keys())
        diff = []
        for key in sorted(shared):
            v1 = (ds[0].get("fields") or {}).get(key)
            v2 = (other.get("fields") or {}).get(key)
            if doc_contract.canonical_json(v1) != doc_contract.canonical_json(v2):
                diff.append(f"{key}（{v1} ≠ {v2}）")
        if diff:
            conflicts.append(f"{type_name}（{doc_ids}）字段冲突：{'；'.join(diff[:6])}"
                             if (doc_ids := "、".join(str(d.get("doc_id") or _doc_name(d)) for d in ds)) else type_name)
        else:
            identical.append(f"{type_name}（{doc_ids}，{len(ds)}份字段完全相同）"
                             if (doc_ids := "、".join(str(d.get("doc_id") or _doc_name(d)) for d in ds)) else type_name)
    if conflicts:
        return _make_result(
            "DOC-003", "单证齐全性", "单证重复与冲突检查", STATUS_FAIL,
            "存在同类型重复单证且字段相互冲突：" + "；".join(conflicts), dup_doc_ids,
            "同一批次每种单证超出申报份数的部分应去重；请删除错误版本单证或更正一致后再核验——"
            "在去重之前，本系统不采信该类型单证的任何『一致』结论。")
    return _make_result(
        "DOC-003", "单证齐全性", "单证重复与冲突检查", STATUS_WARNING,
        "存在同类型重复单证：" + "；".join(identical), dup_doc_ids,
        "申报份数之外存在重复提交，请确认并去重；若为正本+副本请人工确认后忽略本提示。")


def check_declared_composition(documents: list, declared_composition: dict | None) -> list:
    """实收构成与申报构成核对（任务书问题二第1步的闭环）：
    用户申报"发票1、运单3"后，实际识别/上传的构成与之不符时显式告警，
    提示回上一步调整声明或补传，不让构成差异静默进入核验。"""
    if not declared_composition:
        return []
    declared = {t: n for t, n in declared_composition.items()
                if isinstance(n, int) and n >= 0}
    actual: dict = {}
    for doc in documents:
        dtype = doc.get("doc_type") or "unknown"
        actual[dtype] = actual.get(dtype, 0) + 1
    notes = []
    for t, n in declared.items():
        got = actual.get(t, 0)
        if got != n:
            tname = doc_contract.DOC_TYPE_LABELS.get(t, t)
            if got < n:
                notes.append(f"{tname}申报{n}份，实际仅{got}份（少{-(got - n)}份，请回上一步补传）")
            else:
                notes.append(f"{tname}申报{n}份，实际{got}份（多{got - n}份，请核对是否重复上传）")
    undeclared = [t for t in actual if t not in declared and actual[t] > 0]
    for t in undeclared:
        notes.append(f"{doc_contract.DOC_TYPE_LABELS.get(t, t)}×{actual[t]}未在第一步申报"
                     f"（请回第一步调整构成声明，或人工指定类型）")
    if notes:
        return [_make_result(
            "DOC-004", "单证齐全性", "实收构成与申报构成核对", STATUS_WARNING,
            "实际单证构成与申报不一致：" + "；".join(notes), [],
            "请回到上一步调整构成声明或补传缺失单据；构成不符时，齐全性与交叉比对的结论不完整。")]
    pretty = "、".join(f"{doc_contract.DOC_TYPE_LABELS.get(t, t)}×{n}"
                       for t, n in sorted(declared.items()))
    return [_make_result(
        "DOC-004", "单证齐全性", "实收构成与申报构成核对", STATUS_PASS,
        f"实际单证构成与申报一致：{pretty}（共{len(documents)}份）。", [], None)]


def check_document_rules(documents: list) -> list:
    """单据级规范检查（任务书问题三/四）：对每一份单据执行 doc_rules.yaml
    知识库中该类型的必填/格式/数值范围规则（如"运单缺少收货人"判FAIL），
    每份单据产出一条聚合结果，字段级问题放在 field_issues 供分层展示。
    每条结果附带规则知识库版本号。"""
    labels = build_instance_labels(documents)
    rule_set = doc_rules.load_rules()
    results = []
    for doc in documents:
        dtype = doc.get("doc_type") or "unknown"
        if dtype == "unknown":
            continue      # 类型未知的单证由 DOC-002 提示人工指定，规范规则无从适用
        doc_id = doc.get("doc_id") or ""
        label = labels.get(doc_id, dtype)
        report = doc_rules.check_document(doc)
        if report["status"] == doc_rules.STATUS_PASS:
            results.append(_make_result(
                "DOC-101", "单据规范", f"单据规范检查（{label}）", STATUS_PASS,
                f"{label}（{_doc_name(doc)}）通过单据级规范检查"
                f"（适用规则 {sum(1 for r in rule_set['rules'] if r['doc_type'] == dtype)} 条）。",
                [doc_id], None,
                doc_label=label, field_issues=[], doc_id=doc_id,
                rules_version=rule_set["version"]))
            continue
        issues = report["field_issues"]
        fail_issues = [i for i in issues if i["severity"] == "fail"]
        warn_issues = [i for i in issues if i["severity"] != "fail"]
        status = STATUS_FAIL if fail_issues else STATUS_WARNING
        parts = [f"缺少{i['field_label']}（{i['message']}）" if i["message"].startswith("缺少")
                 else f"{i['field_label']}：{i['message']}" for i in issues]
        results.append(_make_result(
            "DOC-101", "单据规范", f"单据规范检查（{label}）", status,
            f"{label}（{_doc_name(doc)}）存在{len(fail_issues)}项硬性不规范、{len(warn_issues)}项待复核："
            + "；".join(parts),
            [doc_id],
            "；".join(report["suggestions"]),
            doc_label=label, field_issues=issues, doc_id=doc_id,
            rules_version=rule_set["version"]))
    return results


# ---------------------------------------------------------------- 一致性检查


def check_goods_description(documents: list) -> dict:
    """
    货物描述一致性（语义相似度分级 + 关键实体守卫，修复 F03）。

    以多数单证的描述为基准，与其余描述逐一计算语义相似度：
      ≥0.85  一致（视为同一表述）
      0.60-0.85  存疑 WARNING（灰色区，转人工复核/AI二次判断）
      <0.60  不一致 FAIL
    守卫：数字+单位规格、数字、否定词独立核对——存在关键实体矛盾时，
    无论字符相似度多高都判不一致（如"钢板厚度1.5mm" vs "钢板厚度15mm"）。
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

    # 与基准逐一计算语义相似度+关键实体守卫
    conflicts, suspects, matched_but_diff, pair_entries = [], [], [], []
    for desc, docs in groups.items():
        if desc == base_desc:
            continue
        cmp = semantic.compare(str(base_text), str(docs[0]["fields"]["goods_description"]))
        score, grade, guard = cmp["score"], cmp["grade"], cmp["guard"]
        doc_names = "、".join(_doc_name(d) for d in docs)
        entry = {
            "docs": [d.get("doc_id") for d in docs],
            "text": docs[0]["fields"].get("goods_description"),
            "score": score,
            "grade": grade,
            "guard": guard,
        }
        pair_entries.append(entry)
        if grade == "mismatch":
            note = f"{doc_names}（「{desc}」，语义相似度 {score:.2f}"
            if guard:
                note += f"，关键实体矛盾：{_GUARD_LABELS.get(guard, guard)}，字符相似度不作为放行依据"
            note += "）"
            conflicts.append(note)
        elif grade == "suspect":
            suspects.append(f"{doc_names}（「{desc}」，语义相似度 {score:.2f}）")
        else:
            matched_but_diff.append(f"{doc_names}（「{desc}」，相似度 {score:.2f}）")

    similarity_notes = conflicts + suspects
    involved = [d.get("doc_id") for g in groups.values() for d in g
                if _norm_text(g[0]["fields"].get("goods_description")) != base_desc]
    similarities_payload = [{"docs": g["docs"], "text": g["text"],
                             "score": g["score"], "grade": g["grade"], "guard": g["guard"]}
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
    """件数一致性 + 整数契约（修复 F01）：0/负数/非数字判 FAIL，
    小数计件（如480.5）与带单位字符串判"待复核"，均不参与容差比较。"""
    pairs = _group_by_field(documents, "total_packages")
    if len(pairs) < 2:
        return _make_result(
            "CONS-002", "字段一致性", "件数（箱数）一致性", STATUS_WARNING,
            "载有件数的单证不足2份，无法交叉比对。", [],
            "请确认发票、装箱单、报关单均已填写总件数栏。",
        )
    valid, illegal, review = [], [], []
    for doc, value in pairs:
        num, err = doc_contract.parse_packages(value)
        name = _doc_name(doc)
        if err == doc_contract.OK:
            valid.append((doc, num))
        elif err == doc_contract.WITH_UNIT:
            review.append(f"{name}（件数「{value}」不是合法整数计件，待人工复核）")
        else:
            illegal.append(f"{name}（件数「{value}」为非法数值：0/负数/非数字）")
    if illegal or review:
        notes = illegal + review
        status = STATUS_FAIL if illegal else STATUS_WARNING
        return _make_result(
            "CONS-002", "字段一致性", "件数（箱数）一致性", status,
            "件数字段存在非法/存疑值，本次未做一致性放行：" + "；".join(notes),
            [d.get("doc_id") for d, _ in pairs],
            "件数必须为正整数。请按现场装箱计数更正各单证件数后再核验；"
            "小数或带单位的计件值会被判待复核，不会被静默截断。")

    values: dict = {}
    for doc, num in valid:
        values.setdefault(int(num), []).append(doc)
    if len(values) == 1:
        count = next(iter(values))
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
            wrong.append("、".join(f"{_doc_name(d)}（{value}件）" for d in docs))
    detail = f"件数不一致：多数单证为{base}件，{'；'.join(wrong)}"
    return _make_result(
        "CONS-002", "字段一致性", "件数（箱数）一致性", STATUS_FAIL, detail,
        [d.get("doc_id") for k, docs in values.items() if k != base for d in docs],
        f"请核对现场装箱计数：若实际为{base}件，则更正{'、'.join(wrong)}并重新提交报关；"
        f"若实际与其他单证一致，则需同步更正发票/装箱单。件数不符将导致口岸过机查验与铅封核对异常。",
    )


def check_gross_weight(documents: list) -> dict:
    """毛重一致性（容差1%）+ 数值合法性门槛（修复 F01）：
    基准非法（0/负数/NaN）判 FAIL；带单位字符串（如"12300kg"）判待复核；
    只有全部数值合法后才进入1%容差比较。"""
    pairs = _group_by_field(documents, "gross_weight_kg")
    if len(pairs) < 2:
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_WARNING,
            "载有毛重的单证不足2份，无法交叉比对。", [],
            "请确认发票、装箱单均已填写毛重（kg）。",
        )
    illegal, review, valid = [], [], []
    for doc, value in pairs:
        num, err = doc_contract.parse_number(value)
        name = _doc_name(doc)
        if err == doc_contract.OK:
            if num <= 0:
                illegal.append(f"{name}（毛重「{value}」为非正数，物理上不成立）")
            else:
                valid.append((doc, num))
        elif err == doc_contract.WITH_UNIT:
            review.append(f"{name}（毛重「{value}」数字后带单位/备注，无法确认单位为kg，待人工复核）")
        else:
            illegal.append(f"{name}（毛重「{value}」无法解析为合法数值）")
    if illegal:
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_FAIL,
            "毛重字段存在非法数值，本次未做一致性放行：" + "；".join(illegal + review),
            [d.get("doc_id") for d, _ in pairs],
            "毛重必须为正数（kg）。请以衡器记录为准更正非法毛重值后重新核验。")
    if len(valid) < 2:
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_WARNING,
            "可解析的毛重数值不足2份：" + "；".join(review), [],
            "请人工核对带单位/无法解析的毛重栏位并补齐为纯数值（kg）后再核验。")

    baseline_doc, baseline_value = next(
        ((d, n) for d, n in valid if d.get("doc_type") == "invoice"), valid[0])

    over = []
    for doc, weight in valid:
        diff_pct = abs(weight - baseline_value) / baseline_value if baseline_value else 0
        if diff_pct > WEIGHT_TOLERANCE:
            over.append((_doc_name(doc), weight, diff_pct))

    if not over:
        names = "、".join(f"{_doc_name(d)} {n:.0f}kg" for d, n in valid)
        result_detail = f"各单证毛重一致或差异在1%容差内：{names}"
        if review:
            return _make_result(
                "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_WARNING,
                result_detail + "；另有：" + "；".join(review),
                [d.get("doc_id") for d, _ in pairs],
                "部分毛重值待人工复核（见明细），确认前不建议放行。")
        return _make_result(
            "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_PASS,
            result_detail, [d.get("doc_id") for d, _ in pairs], None,
        )
    bad_items = [f"{name}（{weight:.0f}kg，偏离基准{diff_pct * 100:.2f}%）"
                 for name, weight, diff_pct in over]
    detail = (f"毛重超出1%容差：基准为{_doc_name(baseline_doc)} {baseline_value:.0f}kg，"
              f"{'；'.join(bad_items)}")
    if review:
        detail += "；另有：" + "；".join(review)
    return _make_result(
        "CONS-003", "字段一致性", "毛重一致性（容差1%）", STATUS_FAIL, detail,
        [d.get("doc_id") for d, v in valid
         if abs(v - baseline_value) / (baseline_value or 1) > WEIGHT_TOLERANCE],
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
    """报关单『运单号』应与铁路运单号一致（交叉引用，覆盖每一份报关单——修复 F02）。"""
    waybills = [d for d in documents
                if d.get("doc_type") in doc_contract.RAIL_WAYBILL_TYPES]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not waybills or not customs:
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_WARNING,
            "缺少铁路运单或出口报关单，无法交叉核对运单号。", [],
            "请先补齐铁路运单与出口报关单。",
        )
    wb_nos = {_norm_text(_field(wb, "waybill_no") or "").upper() for wb in waybills}
    if not any(wb_nos):
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_WARNING,
            "运单号字段缺失（运单未填写），无法交叉核对。",
            [wb.get("doc_id") for wb in waybills],
            "请在铁路运单『运单号』栏补填运单号。")
    mismatched, missing = [], []
    for cus in customs:
        cus_no = _field(cus, "waybill_no")
        if cus_no is None:
            missing.append(_doc_name(cus))
        elif _norm_text(cus_no).upper() not in wb_nos:
            mismatched.append(f"{_doc_name(cus)}（报关单运单号 {cus_no}）")
    if mismatched or missing:
        notes = [f"{name}未填写运单号" for name in missing] + [
            f"{m} 与铁路运单号（{'、'.join(sorted(wb_nos))}）不一致" for m in mismatched]
        return _make_result(
            "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_FAIL,
            "报关单运单号交叉核对未通过：" + "；".join(notes),
            [c.get("doc_id") for c in customs],
            f"请将各报关单『随附单证-运单号』栏更正为铁路运单号{'、'.join(sorted(wb_nos))}后重新申报。")
    return _make_result(
        "CONS-006", "字段一致性", "报关单运单号交叉核对", STATUS_PASS,
        f"全部报关单运单号与铁路运单一致：{'、'.join(sorted(wb_nos))}",
        [wb.get("doc_id") for wb in waybills] + [c.get("doc_id") for c in customs], None,
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
    """申报金额核对（修复 F01/F02）：
    1. 币种先行——缺失判待复核、发票与报关单币种冲突判 FAIL，禁止跨币种比数字；
    2. 金额必须为正数，非法数值/带单位字符串不进入容差比较；
    3. 覆盖每一份报关单（不再只读第一份）。"""
    invoices = [d for d in documents if d.get("doc_type") == "invoice"]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not invoices or not customs:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_WARNING,
            "缺少发票或报关单，无法核对申报金额。", [], "请先补齐商业发票与出口报关单。",
        )
    invoice = invoices[0]
    inv_amt, inv_err = doc_contract.parse_number(_field(invoice, "total_amount"))
    currency = _norm_text(_field(invoice, "currency") or "")
    doc_ids = [invoice.get("doc_id")] + [c.get("doc_id") for c in customs]

    # --- 数值合法性门槛 ---
    if inv_err == doc_contract.WITH_UNIT:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_WARNING,
            f"发票总金额「{_field(invoice, 'total_amount')}」数字后带单位/备注，无法解析，待人工复核。",
            doc_ids, "请将发票总金额更正为纯数值后重新核验。")
    if inv_err != doc_contract.OK or (inv_amt is not None and inv_amt <= 0):
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_FAIL,
            f"发票总金额「{_field(invoice, 'total_amount')}」为非法数值（0/负数/非数字），无法作为比对基准。",
            doc_ids, "发票总金额必须为正数，请更正后重新核验。")

    # --- 币种一致性（修复 F01 币种矛盾全绿）---
    cus_currencies = {_norm_text(_field(c, "currency") or "").upper() for c in customs}
    cus_currencies.discard("")
    inv_cur = currency.upper()
    if inv_cur and cus_currencies and cus_currencies != {inv_cur}:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_FAIL,
            f"币种矛盾：发票为{currency}，报关单为{'、'.join(sorted(cus_currencies))}；"
            f"跨币种金额不可直接比对，本次不做金额一致性放行。",
            doc_ids, "请统一发票与报关单币种（或注明折算汇率与折算值）后重新核验。")
    if not inv_cur or not cus_currencies:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_WARNING,
            "币种字段缺失（发票或报关单未填写币种），无法证明金额口径一致，待人工复核。",
            doc_ids, "请补填发票与报关单币种后重新核验。")

    # --- 逐份报关单金额核对 ---
    bad_amounts, illegal = [], []
    for cus in customs:
        cus_amt, err = doc_contract.parse_number(_field(cus, "declared_value"))
        if err == doc_contract.WITH_UNIT:
            illegal.append(f"{_doc_name(cus)}（申报金额「{_field(cus, 'declared_value')}」带单位，待人工复核）")
            continue
        if err != doc_contract.OK or cus_amt is None or cus_amt <= 0:
            illegal.append(f"{_doc_name(cus)}（申报金额「{_field(cus, 'declared_value')}」为非法数值）")
            continue
        diff_pct = abs(cus_amt - inv_amt) / inv_amt if inv_amt else 0
        if diff_pct > AMOUNT_TOLERANCE:
            bad_amounts.append(f"{_doc_name(cus)}（{cus_amt:,.2f}，偏离{diff_pct * 100:.2f}%）")
    if illegal:
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_FAIL,
            "申报金额存在非法/存疑值：" + "；".join(illegal), doc_ids,
            "申报金额必须为纯数值正数，请更正后重新核验。")
    if bad_amounts:
        detail = (f"申报金额不符：发票{inv_amt:,.2f} {currency}，"
                  f"{'；'.join(bad_amounts)}")
        return _make_result(
            "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_FAIL, detail,
            [c.get("doc_id") for c in customs],
            f"请以发票金额{inv_amt:,.2f} {currency}为准更正报关单申报金额；低报/高报均可能被认定为申报不实。")
    return _make_result(
        "CONS-008", "字段一致性", "报关金额与发票金额核对", STATUS_PASS,
        f"全部报关单申报金额与发票金额一致：{inv_amt:,.2f} {currency}（容差0.5%）",
        doc_ids, None)


# ---------------------------------------------------------------- 路线合规检查


def _route_rule_meta() -> dict:
    """路线判断结果的规则元信息（F07：版本号+覆盖范围说明必须随结果输出）。"""
    return {"rule_version": ROUTE_RULE_VERSION, "rule_coverage": ROUTE_RULE_COVERAGE}


def check_waybill_type_route(documents: list) -> list:
    """运单类型与线路匹配（修复 F07；任务书问题四按实例核验）：
    - 对每一份铁路运单独立判断（多运单批次下，问题能定位到具体哪份运单）；
    - 运单类型限定受支持枚举（SMGS / CIM / CIM-SMGS统一运单）；
      枚举外值（如 AIR WAYBILL）判"类型不支持，需人工复核"，不进入路线覆盖判断；
    - 覆盖集合按运单类型取对应集合，统一运单取 SMGS∪CIM 并集；
    - 并集之外的途经国家（如美国）判 FAIL，类型自身集合外国家判 WARNING；
    - 每条结果附带规则版本与覆盖范围说明。"""
    waybills = [d for d in documents
                if d.get("doc_type") in doc_contract.RAIL_WAYBILL_TYPES]
    if not waybills:
        return [_make_result(
            "ROUTE-001", "路线合规性", "运单类型与线路匹配", STATUS_WARNING,
            f"缺少铁路运单，无法核验路线合规性。{_ROUTE_RULE_NOTE}", [], "请先补齐国际铁路运单。",
            **_route_rule_meta())]
    labels = build_instance_labels(documents)
    results = []
    for wb in waybills:
        label = labels.get(wb.get("doc_id") or "", "运单")
        results.append(_check_single_waybill_route(wb, label))
    return results


def _check_single_waybill_route(wb: dict, label: str) -> dict:
    wb_type = _norm_text(_field(wb, "waybill_type") or "")
    route = _field(wb, "route_countries") or []
    if wb.get("doc_type") == "smgs_rail_waybill" and not wb_type:
        # СМГС版式单据的运单类型自明（任务书A3）：不再要求人工补填类型栏
        wb_type = "SMGS国际货协运单"
    if not wb_type or not route:
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_WARNING,
            f"{label}缺少运单类型或经停国家信息，无法核验。{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")], f"请在{label}上补填运单类型（SMGS/CIM）及经停国家。",
            **_route_rule_meta())
    if isinstance(route, str) or not all(isinstance(c, str) for c in route):
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_WARNING,
            f"{label}经停国家字段格式异常（应为字符串列表，收到：{route!r}），需人工复核。{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")], "请将经停国家改为国家列表（如 [\"中国\", \"德国\"]）后重新核验。",
            **_route_rule_meta())

    # 类型枚举校验：枚举外一律不支持，直接转人工，不做任何"匹配"结论
    kind = doc_contract.classify_waybill_type(wb_type)
    if kind is None:
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_FAIL,
            f"{label}运单类型「{wb_type}」不在受支持枚举内（仅支持：SMGS国际货协运单 / "
            f"CIM国际铁路运单 / CIM-SMGS统一运单），无法进行路线合规判断，需人工复核更正。"
            f"{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")],
            "请人工确认该运单的真实类型：若为非铁路运输单证（如航空运单），本系统路线结论不适用，"
            "应更换为国际铁路运单；若为铁路运单，请更正运单类型栏后再核验。",
            supported_waybill_types=doc_contract.SUPPORTED_WAYBILL_TYPES,
            **_route_rule_meta())

    covered = doc_contract.coverage_set(kind)
    union = set(SMGS_COUNTRIES) | set(CIM_COUNTRIES)
    outside = [c for c in route if c not in union]
    if outside:
        bad = "、".join(outside)
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_FAIL,
            f"{label}途经国家 {bad} 不在本系统路线规则覆盖范围（SMGS∪CIM 铁路联运集合）内，"
            f"运单类型「{wb_type}」的路线合规结论不适用，需人工核实实际运输路径。{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")],
            f"{bad}不在中欧班列常见铁路通道覆盖集合内：请人工确认是否为真实路线；"
            f"若属实，需按实际适用的运输公约人工判断，本系统的路线合规结论不作为依据。",
            outside_coverage_countries=outside,
            **_route_rule_meta())

    uncovered = [c for c in route if c not in covered]
    if uncovered and kind == doc_contract.WAYBILL_KIND_SMGS:
        bad = "、".join(uncovered)
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_WARNING,
            f"{label}线路含{bad}，但运单类型为{wb_type}（SMGS运单不覆盖{bad}段）。{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")],
            f"本线路经由{bad}（中间走廊/巴库-第比利斯-卡尔斯段），建议改用CIM/SMGS统一运单，"
            f"或提前与承运人确认卡尔斯换装段的运单转换与补单安排，避免边境段无有效运单凭证。",
            **_route_rule_meta())
    if uncovered and kind == doc_contract.WAYBILL_KIND_CIM:
        bad = "、".join(uncovered)
        return _make_result(
            "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_WARNING,
            f"{label}线路含{bad}，但运单类型为{wb_type}（CIM运单不覆盖{bad}段）。{_ROUTE_RULE_NOTE}",
            [wb.get("doc_id")],
            f"CIM运单不覆盖{bad}段，建议改用CIM/SMGS统一运单或分段衔接安排。",
            **_route_rule_meta())
    return _make_result(
        "ROUTE-001", "路线合规性", f"运单类型与线路匹配（{label}）", STATUS_PASS,
        f"{label}运单类型「{wb_type}」与经停国家（{'、'.join(route)}）匹配。{_ROUTE_RULE_NOTE}",
        [wb.get("doc_id")], None,
        **_route_rule_meta())


def check_route_ends(documents: list) -> dict:
    """报关起运/目的国与运单路线端点核对（覆盖每一份报关单；附带规则元信息）。
    多运单批次下以第一份载有路线信息的运单为基准（同批多车厢运单路线一致）。"""
    waybills = [d for d in documents
                if d.get("doc_type") in doc_contract.RAIL_WAYBILL_TYPES]
    customs = [d for d in documents if d.get("doc_type") == "export_customs_declaration"]
    if not waybills or not customs:
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_WARNING,
            "缺少铁路运单或报关单，无法核对路线端点。", [], "请先补齐铁路运单与出口报关单。",
            **_route_rule_meta())
    baseline = next((wb for wb in waybills if _field(wb, "route_countries")), waybills[0])
    route = _field(baseline, "route_countries") or []
    if not route:
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_WARNING,
            "运单经停国家信息缺失，无法核对路线端点。",
            [baseline.get("doc_id")],
            "请补填运单经停国家。", **_route_rule_meta())
    if isinstance(route, str) or not all(isinstance(c, str) for c in route):
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_WARNING,
            f"运单经停国家字段格式异常（应为字符串列表，收到：{route!r}），无法核对端点。",
            [waybills[0].get("doc_id")],
            "请将经停国家改为国家列表后重新核验。", **_route_rule_meta())

    mismatched, missing = [], []
    for cus in customs:
        dep_cus = _field(cus, "departure_country")
        dst_cus = _field(cus, "destination_country")
        if dep_cus is None or dst_cus is None:
            missing.append(f"{_doc_name(cus)}（起运国/运抵国缺失）")
        elif not (route[0] == dep_cus and route[-1] == dst_cus):
            mismatched.append(
                f"{_doc_name(cus)}（报关起运/运抵国 {dep_cus}/{dst_cus}，"
                f"运单路线端点 {route[0]}/{route[-1]}）")
    if mismatched or missing:
        detail = ("路线端点核对未通过：" + "；".join(missing + mismatched)
                  + f"。{_ROUTE_RULE_NOTE}")
        return _make_result(
            "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_FAIL, detail,
            [c.get("doc_id") for c in customs],
            "请核对货物实际运输路径，修改报关单起运国/运抵国（或运单路线）后重新申报。",
            **_route_rule_meta())
    return _make_result(
        "ROUTE-002", "路线合规性", "报关起运/目的国与运单路线核对", STATUS_PASS,
        f"全部报关单起运国/运抵国与运单路线端点（{route[0]} → {route[-1]}）一致。{_ROUTE_RULE_NOTE}",
        [waybills[0].get("doc_id")] + [c.get("doc_id") for c in customs], None,
        **_route_rule_meta())


# ---------------------------------------------------------------- 主入口

CHECK_RUNNERS = [
    check_goods_description,
    check_total_packages,
    check_gross_weight,
    check_consignor,
    check_consignee,
    check_waybill_no_crossref,
    check_container_no,
    check_declared_amount,
    check_route_ends,
]


def build_document_groups(documents: list, results: list) -> tuple[list, list]:
    """把扁平的检查结果按单据实例分组（任务书问题四）。
    返回 (document_groups, batch_level_issues)：
      document_groups: 每份单据一组 {doc_id, label, doc_type, title, fail_count,
                         warning_count, issues:[问题列表(含字段与建议)]}；
      batch_level_issues: 无法归属到单份单据的批次级问题（缺单证/结构/构成核对等）。
    交叉比对类问题按 involved_docs 归入每份涉及单据的组（如"毛重与发票不符"
    同时出现在偏差单据与基准单据的组里，两侧都能看到）。"""
    labels = build_instance_labels(documents)
    known_ids = {doc.get("doc_id") for doc in documents}
    groups = []
    for doc in documents:
        doc_id = doc.get("doc_id") or ""
        groups.append({
            "doc_id": doc_id,
            "label": labels.get(doc_id, doc.get("doc_type", "单据")),
            "doc_type": doc.get("doc_type") or "unknown",
            "title": _doc_name(doc),
            "fail_count": 0,
            "warning_count": 0,
            "issues": [],
        })
    by_id = {g["doc_id"]: g for g in groups if g["doc_id"]}
    batch_issues = []
    for r in results:
        if r["status"] == STATUS_PASS:
            continue
        involved = [d for d in (r.get("involved_docs") or []) if d in by_id]
        issue = {
            "check_id": r.get("check_id"),
            "check_name": r.get("check_name"),
            "category": r.get("category"),
            "status": r.get("status"),
            "detail": r.get("detail"),
            "field": r.get("field"),
            "suggestion": r.get("suggestion"),
        }
        if involved:
            for doc_id in involved:
                g = by_id[doc_id]
                g["issues"].append(issue)
                if r["status"] == STATUS_FAIL:
                    g["fail_count"] += 1
                else:
                    g["warning_count"] += 1
        else:
            batch_issues.append(issue)
    return groups, batch_issues


def run_verification(batch: dict) -> dict:
    """
    对一个批次（解析后的JSON dict，含 documents 列表）执行全部核验。

    返回结构：
    {
        "batch_id": ...,
        "batch_name": ...,
        "rule_version": 路线规则版本,
        "results": [ {check_id, category, check_name, status, detail, involved_docs, suggestion}, ... ],
        "summary": {"total": n, "pass": p, "warning": w, "fail": f},
        "risk": { "score": 0-100, "grade": "low"|"medium"|"high",
                  "grade_label": ..., "breakdown": [可解释分数构成] },
        "suggestions": [ 仅为 FAIL/WARNING 项动态生成的修正建议文本, ... ],
        # ---- 任务书问题四新增：分层结构（按单据实例分组，API与页面共用） ----
        "document_groups": [ {doc_id, label, doc_type, title, fail_count,
                              warning_count, issues:[...]} , ...],
        "batch_level_issues": [ 无法归属单份单据的批次级问题 ],
        "declared_composition": {申报构成原样回显，未申报时为 null},
        "doc_rules_version": 单据级规则知识库版本,
    }
    """
    raw_documents = batch.get("documents", [])
    declared = batch.get("declared_composition") or None
    documents, _dropped = doc_contract.sanitize_documents(raw_documents)

    results = [check_input_structure(raw_documents)]
    results.append(check_completeness(documents))
    results.append(check_field_completeness(documents))
    results.append(check_duplicate_documents(documents, declared))
    results.extend(check_declared_composition(documents, declared))
    results.extend(check_document_rules(documents))
    results.extend(runner(documents) for runner in CHECK_RUNNERS)
    # ROUTE-001 对每份运单独立产出结果（多运单批次返回列表）
    results.extend(check_waybill_type_route(documents))

    for r in results:
        if r.get("field") is None and r.get("check_id") in CHECK_FIELD:
            r["field"] = CHECK_FIELD[r["check_id"]]

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
    document_groups, batch_level_issues = build_document_groups(documents, results)
    return {
        "batch_id": batch.get("batch_id", ""),
        "batch_name": batch.get("batch_name", ""),
        "batch_description": batch.get("description", ""),
        "rule_version": doc_contract.ROUTE_RULE_VERSION,
        "results": results,
        "summary": summary,
        "risk": risk,
        "suggestions": suggestions,
        "document_groups": document_groups,
        "batch_level_issues": batch_level_issues,
        "declared_composition": declared,
        "doc_rules_version": doc_rules.rules_version(),
    }


def sort_results_by_severity(results: list) -> list:
    """按 FAIL > WARNING > PASS 稳定排序（供展示层使用）。"""
    return sorted(results, key=lambda r: SEVERITY_ORDER.get(r["status"], 99))
