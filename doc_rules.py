# -*- coding: utf-8 -*-
"""
单据类型规则知识库加载与单据级检查（任务书问题三）。

规则全部来自项目根目录的 doc_rules.yaml（业务同事可直接编辑，含中文注释
与"如何新增一条规则"的说明），本模块负责：
  1. 加载并校验规则（enabled/类型/正则合法性），配置错误显式上报而非崩溃；
  2. check_document(doc)：对一份单据执行其类型适用的全部规则，返回
     {status: PASS/FAIL/WARNING, field_issues: [字段级问题], suggestions: [修改建议]}；
  3. 引擎把每份单据的检查结果聚合成一条 DOC-101 结果（"运单#2 单据规范检查"），
     字段级明细放在 field_issues 里供分层展示。

依赖说明：优先用 PyYAML 读取配置；环境未安装 PyYAML 时回退到内置默认规则
（与 doc_rules.yaml 第一批启用规则同内容），保证引擎在任何环境可运行。
"""

from __future__ import annotations

import re
from pathlib import Path

import doc_contract

RULES_VERSION = "doc-rules v1.0"
RULES_FILE = Path(__file__).parent / "doc_rules.yaml"

STATUS_FAIL = "FAIL"
STATUS_WARNING = "WARNING"
STATUS_PASS = "PASS"

# yaml 缺失时的内置默认规则（内容与 doc_rules.yaml 第一批 enabled 规则一致；
# 修改线上规则口径请改 doc_rules.yaml，不要改这里——这里只是离线兜底）
EMBEDDED_DEFAULT_RULES = [
    {"rule_id": "waybill_consignee_required", "doc_type": "railway_waybill",
     "field": "consignee_name", "rule_type": "required", "severity": "fail",
     "message": "运单缺少收货人信息",
     "suggestion": "运单缺少收货人，请核实原始单据并补充收货人名称及地址后再提交"},
    {"rule_id": "invoice_total_amount_required", "doc_type": "invoice",
     "field": "total_amount", "rule_type": "required", "severity": "fail",
     "message": "发票缺少总金额",
     "suggestion": "发票缺少总金额，请核实原始单据并补填金额合计栏后再提交"},
    {"rule_id": "invoice_total_amount_positive", "doc_type": "invoice",
     "field": "total_amount", "rule_type": "range", "min": 0.01, "severity": "fail",
     "message": "发票总金额必须为正数（0或负数不成立）",
     "suggestion": "发票总金额为0或负数，请核对金额合计栏与币种，按贸易合同金额更正后重新提交"},
    {"rule_id": "invoice_currency_required", "doc_type": "invoice",
     "field": "currency", "rule_type": "required", "severity": "warning",
     "message": "发票未填写币种",
     "suggestion": "发票未填写币种，无法确认金额口径，请核实后补填币种（如USD/EUR/CNY）"},
    {"rule_id": "customs_declared_value_required", "doc_type": "export_customs_declaration",
     "field": "declared_value", "rule_type": "required", "severity": "fail",
     "message": "报关单缺少申报金额",
     "suggestion": "报关单缺少申报金额（总价栏），请按发票金额如实补填后重新申报"},
]

_VALID_RULE_TYPES = {"required", "format", "range"}


def _parse_rules(raw: dict, source: str) -> tuple[list, list]:
    """解析+校验规则列表，返回 (生效规则, 配置问题清单)。非法规则跳过并上报。"""
    rules, problems = [], []
    for i, item in enumerate(raw.get("rules") or []):
        if not isinstance(item, dict):
            problems.append(f"{source} 第{i + 1}条规则不是键值对象，已跳过")
            continue
        rid = item.get("rule_id") or f"rules[{i}]"
        if item.get("enabled") is False:
            continue
        if item.get("rule_type") not in _VALID_RULE_TYPES:
            problems.append(f"规则 {rid}：rule_type 必须是 required/format/range，已跳过")
            continue
        if not item.get("doc_type") or not item.get("field"):
            problems.append(f"规则 {rid}：缺少 doc_type 或 field，已跳过")
            continue
        if item.get("rule_type") == "format":
            try:
                re.compile(str(item.get("pattern", "")))
            except re.error as exc:
                problems.append(f"规则 {rid}：正则表达式不合法（{exc}），已跳过")
                continue
        rule = dict(item)
        rule.setdefault("severity", "warning")
        rule.setdefault("message", f"字段 {rule['field']} 不满足规则 {rid}")
        rule.setdefault("suggestion", f"请核对并更正 {doc_contract.FIELD_LABELS_ZH.get(rule['field'], rule['field'])}")
        rules.append(rule)
    return rules, problems


_CACHE: dict = {"mtime": None, "rules": [], "problems": [], "source": ""}


def load_rules(force: bool = False) -> dict:
    """加载规则集（按文件修改时间缓存）。返回 {version, rules, problems, source}。"""
    if not RULES_FILE.exists():
        rules, problems = _parse_rules({"rules": EMBEDDED_DEFAULT_RULES}, "内置默认")
        return {"version": RULES_VERSION, "rules": rules, "problems": problems,
                "source": "embedded（doc_rules.yaml 不存在）"}
    mtime = RULES_FILE.stat().st_mtime
    if not force and _CACHE["mtime"] == mtime:
        return {"version": RULES_VERSION, "rules": _CACHE["rules"],
                "problems": _CACHE["problems"], "source": _CACHE["source"]}
    try:
        import yaml
        raw = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
        source = "doc_rules.yaml"
    except ImportError:
        # 环境未装 PyYAML：回退内置默认规则（口径与YAML第一批一致），显式标注来源
        raw = {"rules": EMBEDDED_DEFAULT_RULES}
        source = "embedded（未安装PyYAML，使用内置默认规则）"
    except Exception as exc:
        raw = {"rules": EMBEDDED_DEFAULT_RULES}
        source = f"embedded（doc_rules.yaml 解析失败：{exc}）"
    rules, problems = _parse_rules(raw, source)
    if isinstance(raw, dict) and isinstance(raw.get("version"), str):
        version = raw["version"]
    else:
        version = RULES_VERSION
    _CACHE.update({"mtime": mtime, "rules": rules, "problems": problems, "source": source})
    return {"version": version, "rules": rules, "problems": problems, "source": source}


def rules_version() -> str:
    return load_rules().get("version", RULES_VERSION)


def config_problems() -> list:
    """规则配置的问题清单（供界面提示"哪条规则写错了"），无问题时为空。"""
    return load_rules().get("problems", [])


def _field_missing(value) -> bool:
    """字段缺失口径与引擎一致：None/空白字符串/空列表 视为缺失。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def check_document(doc: dict) -> dict:
    """对一份单据执行其类型的全部启用规则。
    返回 {status, field_issues, suggestions}：
      field_issues: [{field, field_label, rule_id, severity, message}]（fail 排前）
      suggestions: 与 field_issues 对应的修改建议文本（fail 排前）"""
    doc_type = doc.get("doc_type") or "unknown"
    fields = doc.get("fields") or {}
    rule_set = load_rules()
    fails, warns = [], []
    for rule in rule_set["rules"]:
        if rule["doc_type"] != doc_type:
            continue
        field = rule["field"]
        value = fields.get(field)
        violated = False
        if rule["rule_type"] == "required":
            if _field_missing(value) and doc_contract.has_field_meta(doc):
                # 四状态口径（P0任务书A1）：带识别证据的单据，仅"业务确认缺失"
                # 触发规则；not_found/needs_review 是识别状态，不能等同业务缺失
                status = doc_contract.field_status(doc, field)
                violated = status == "business_missing"
            else:
                violated = _field_missing(value)
        elif rule["rule_type"] == "format":
            if not _field_missing(value):
                violated = re.fullmatch(str(rule["pattern"]), str(value).strip()) is None
        elif rule["rule_type"] == "range":
            num, err = doc_contract.parse_number(value)
            if err == doc_contract.OK:
                lo = rule.get("min")
                hi = rule.get("max")
                violated = ((lo is not None and num < float(lo))
                            or (hi is not None and num > float(hi)))
            # 数值非法/缺失不在此判定：缺失由 required 管，非法值由引擎数值契约管
        if not violated:
            continue
        issue = {"field": field,
                 "field_label": doc_contract.FIELD_LABELS_ZH.get(field, field),
                 "rule_id": rule["rule_id"],
                 "severity": rule["severity"],
                 "message": rule["message"]}
        (fails if rule["severity"] == "fail" else warns).append(issue)

    field_issues = fails + warns
    suggestions = []
    by_id = {r["rule_id"]: r for r in rule_set["rules"]}
    for issue in field_issues:
        suggestions.append(by_id.get(issue["rule_id"], {}).get("suggestion", issue["message"]))
    if fails:
        status = STATUS_FAIL
    elif warns:
        status = STATUS_WARNING
    else:
        status = STATUS_PASS
    return {"status": status, "field_issues": field_issues, "suggestions": suggestions}
