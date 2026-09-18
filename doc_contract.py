# -*- coding: utf-8 -*-
"""
单证数据契约层（审查整改 F01/F02/F04/F05/F08 的公共依赖）。

统一以下口径，供核验引擎、PDF摄取、API、Web编辑与评估脚本共用：
  1. 受支持的单证类型枚举与各类型必填字段；
  2. 数值字段解析契约：区分"合法数值 / 带单位的待复核值 / 非法数值"，
     拒绝 NaN/Inf/负数/零进入容差比较（修复 F01 全绿漏报）；
  3. 运单类型枚举（SMGS/CIM/统一运单）——枚举之外（如 AIR WAYBILL）
     一律判为"类型不支持"，不得进入路线合规判断（修复 F07 默认放过）；
  4. 路线规则版本与覆盖范围说明——每条路线判断结果附带，明示适用边界；
  5. 数据版本号（内容哈希）——编辑/换文件后派生材料随之失效（修复 F09）；
  6. 结构校验：documents 元素必须是 dict 且 fields 为 dict（修复 F08 崩溃）。

本模块为纯标准库实现，不依赖第三方包。
"""

from __future__ import annotations

import hashlib
import json
import math
import re

# ---------------------------------------------------------------- 版本

# 数据契约版本（字段枚举/必填口径变更时递增）
CONTRACT_VERSION = "contract v1.0"
# 路线合规规则版本（覆盖集合/判断逻辑变更时递增）
ROUTE_RULE_VERSION = "route-rules v2.0"

# ---------------------------------------------------------------- 单证类型与必填字段

SUPPORTED_DOC_TYPES = [
    "invoice",
    "packing_list",
    "railway_waybill",
    "export_customs_declaration",
    "certificate_of_origin",
]

DOC_TYPE_LABELS = {
    "invoice": "商业发票 Commercial Invoice",
    "packing_list": "装箱单 Packing List",
    "railway_waybill": "国际铁路运单 Railway Consignment Note",
    "export_customs_declaration": "出口报关单 Export Customs Declaration",
    "certificate_of_origin": "原产地证书 Certificate of Origin",
    "unknown": "无法识别（需人工指定）",
}

# 各类型必填字段。分两层使用：
#   - 身份字段（*_no / co_no / declaration_no）缺失 → 引擎 DOC-002 检查项 WARNING；
#   - 全部必填字段 → PDF摄取置信度标记与 Web/App 人工补齐表单（F04/F05）。
REQUIRED_FIELDS = {
    "invoice": ["invoice_no", "consignor_name", "consignee_name",
                "goods_description", "total_packages", "gross_weight_kg",
                "total_amount", "currency"],
    "packing_list": ["packing_list_no", "consignor_name", "consignee_name",
                     "goods_description", "total_packages", "gross_weight_kg",
                     "container_no"],
    "railway_waybill": ["waybill_no", "waybill_type", "consignor_name",
                        "consignee_name", "route_countries", "goods_description",
                        "total_packages", "gross_weight_kg", "container_no"],
    "export_customs_declaration": ["declaration_no", "consignor_name", "consignee_name",
                                   "goods_description", "total_packages", "gross_weight_kg",
                                   "declared_value", "currency", "waybill_no",
                                   "container_no", "departure_country", "destination_country"],
    "certificate_of_origin": ["co_no", "consignor_name", "consignee_name",
                              "goods_description", "total_packages"],
    "unknown": [],
}

# 身份字段（单证编号）：缺失即视为"单证不完整，需人工补齐"
IDENTITY_FIELDS = {
    "invoice": "invoice_no",
    "packing_list": "packing_list_no",
    "railway_waybill": "waybill_no",
    "export_customs_declaration": "declaration_no",
    "certificate_of_origin": "co_no",
}

NUMERIC_FIELDS = {"total_packages", "gross_weight_kg", "net_weight_kg",
                  "total_amount", "declared_value"}
INTEGER_FIELDS = {"total_packages"}          # 计件数按整数契约校验（F01）
LIST_FIELDS = {"route_countries"}
CURRENCY_PAIR_FIELDS = ("currency",)          # 发票与报关单需币种一致

FIELD_LABELS_ZH = {
    "invoice_no": "发票号", "packing_list_no": "装箱单号", "waybill_no": "运单号",
    "declaration_no": "报关单编号", "co_no": "产地证编号",
    "consignor_name": "发货人名称", "consignee_name": "收货人名称",
    "goods_description": "货物描述", "total_packages": "件数（箱数）",
    "gross_weight_kg": "毛重（kg）", "net_weight_kg": "净重（kg）",
    "total_amount": "发票总金额", "declared_value": "报关申报金额",
    "currency": "币种", "waybill_type": "运单类型", "route_countries": "经停国家",
    "container_no": "集装箱号", "departure_country": "起运国",
    "destination_country": "运抵国", "departure_station": "发站",
    "destination_station": "到站", "issuer": "签发机构", "origin_country": "原产国",
    # 必填项知识库扩充（doc-rules v2.0）：待启用规则涉及的新字段
    "hs_code": "HS编码", "invoice_date": "发票日期", "unit_price": "单价",
    "incoterm": "价格条款（Incoterms）", "contract_no": "合同号",
    "package_type": "包装方式", "marks": "唛头", "reference_invoice_no": "对应发票号",
}


# ---------------------------------------------------------------- 数值解析契约（F01/F08）

# 纯数字：可选正负号、千分位逗号、小数点
_NUM_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")
# 数字后紧跟字母/汉字单位（如 12300kg、12,300LB）→ 待复核而非崩溃
_WITH_UNIT_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?[^\s\d]|"
                           r"^[+-]?\d+(?:\.\d+)?\s*[A-Za-z\u4e00-\u9fa5]")

OK, WITH_UNIT, NOT_NUMERIC, MISSING = "ok", "with_unit", "not_numeric", "missing"


def parse_number(value) -> tuple[float | None, str]:
    """
    严格数值解析（F01）。
    返回 (数值, 错误码)：错误码为 None/"ok" 时数值可用；
      - "missing"      值为 None/空白（字段缺失，由调用方按缺失口径处理）
      - "with_unit"    数字后带单位/备注的字符串（如 "12300kg"）→ 待人工复核
      - "not_numeric"  NaN/Inf/纯非数字等非法值 → 明确未通过
    千分位逗号允许；布尔值视为非法（bool 是 int 子类，需显式排除）。
    """
    if value is None:
        return None, MISSING
    if isinstance(value, bool):
        return None, NOT_NUMERIC
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num):
            return None, NOT_NUMERIC
        return num, OK
    text = str(value).strip()
    if not text:
        return None, MISSING
    if _NUM_RE.match(text):
        try:
            num = float(text.replace(",", ""))
        except ValueError:
            return None, NOT_NUMERIC
        if not math.isfinite(num):
            return None, NOT_NUMERIC
        return num, OK
    if _WITH_UNIT_RE.match(text):
        return None, WITH_UNIT
    # "NaN"/"Infinity" 等可被 float() 解析但非法的值
    try:
        num = float(text)
        if math.isfinite(num):
            return None, NOT_NUMERIC
        return None, NOT_NUMERIC
    except ValueError:
        return None, NOT_NUMERIC


def parse_packages(value) -> tuple[float | None, str]:
    """件数契约：必须是正整数；480.5 之类小数计件 → 待复核（F04 反例）。"""
    num, err = parse_number(value)
    if err != OK:
        return num, err
    if num <= 0:
        return None, NOT_NUMERIC          # 0/负数件数：非法
    if num != int(num):
        return num, WITH_UNIT             # 非整数计件：截断会掩盖错报，转待复核
    return num, OK


def positive_or_none(num: float | None) -> float | None:
    """域校验：金额/毛重必须 > 0；0、负数返回 None（非法）。"""
    if num is None or num <= 0:
        return None
    return num


# ---------------------------------------------------------------- 运单类型枚举（F07）

# 受支持的运单类型（展示名；引擎按 SMGS/CIM 关键字识别）
SUPPORTED_WAYBILL_TYPES = ["SMGS国际货协运单", "CIM国际铁路运单", "CIM/SMGS统一运单"]

WAYBILL_KIND_SMGS = "smgs"
WAYBILL_KIND_CIM = "cim"
WAYBILL_KIND_COMPOSITE = "composite"


def classify_waybill_type(raw) -> str | None:
    """
    运单类型 → smgs/cim/composite；不受支持或为空返回 None（F07）。
    枚举外值（如 AIR WAYBILL / SEA BILL）一律 None，调用方必须标记
    "类型不支持/需人工复核"，不得继续做路线覆盖判断。
    """
    text = str(raw or "").upper()
    if not text.strip():
        return None
    has_smgs = "SMGS" in text
    has_cim = "CIM" in text
    if has_smgs and has_cim:
        return WAYBILL_KIND_COMPOSITE
    if has_smgs:
        return WAYBILL_KIND_SMGS
    if has_cim:
        return WAYBILL_KIND_CIM
    return None


# ---------------------------------------------------------------- 路线规则覆盖说明（F07）

ROUTE_RULE_COVERAGE = (
    "当前路线规则仅覆盖中欧班列国际铁路联运场景：SMGS（《国际货协》）25国、"
    "CIM（《国际铁路运输公约》）18国，两集合并集之外的国家（如美国等非铁路通道国家）"
    "一律触发告警；不适用于航空/海运/公路运输单证，非全球通用合规判断。"
)


# SMGS（《国际货协》/OSJD）覆盖集合（与 verification_engine 保持一份定义）
SMGS_COUNTRIES = {
    "中国", "蒙古", "越南", "哈萨克斯坦", "乌兹别克斯坦", "土库曼斯坦",
    "吉尔吉斯斯坦", "塔吉克斯坦", "俄罗斯", "白俄罗斯", "乌克兰", "摩尔多瓦",
    "拉脱维亚", "立陶宛", "爱沙尼亚", "波兰", "德国", "捷克", "斯洛伐克",
    "匈牙利", "罗马尼亚", "保加利亚", "阿塞拜疆", "格鲁吉亚", "伊朗",
}

# CIM（《国际铁路运输公约》/OTIF）覆盖集合
CIM_COUNTRIES = {
    "德国", "法国", "波兰", "捷克", "斯洛伐克", "匈牙利", "奥地利", "瑞士",
    "意大利", "荷兰", "比利时", "西班牙", "土耳其", "塞尔维亚", "罗马尼亚",
    "保加利亚", "希腊", "格鲁吉亚",
}


def coverage_set(kind: str) -> set:
    """运单类型对应的覆盖集合；统一运单取并集（F07）。"""
    if kind == WAYBILL_KIND_SMGS:
        return set(SMGS_COUNTRIES)
    if kind == WAYBILL_KIND_CIM:
        return set(CIM_COUNTRIES)
    return set(SMGS_COUNTRIES) | set(CIM_COUNTRIES)


# ---------------------------------------------------------------- 数据版本（F09）


def canonical_json(obj) -> str:
    """稳定序列化：排序键、中文不转义、浮点保留原样。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def data_version(documents: list) -> str:
    """单证内容哈希（F09）：任何字段编辑/文件替换都会改变版本号。
    邮件、对话、下载等派生材料以本版本号做缓存键，数据一变即失效。"""
    return hashlib.sha256(canonical_json(documents).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 人工编辑序列化（F05）

ROUTE_SPLIT_RE = re.compile(r"[、，,]|->|→")


def parse_edited_number(text: str, as_int: bool = False):
    """编辑框文本 → 数值（F05：编辑不得破坏数据类型）。
    解析成功返回数值；空返回 None（不设置）；非纯数字返回原字符串——
    交由引擎口径判"待复核/非法"，不静默丢弃也不静默转换。"""
    text = str(text or "").strip()
    if not text:
        return None
    num, err = parse_number(text)
    if err == OK:
        return int(num) if as_int and num == int(num) else round(num, 2)
    return text


def split_route_text(text: str) -> list | None:
    """经停国家编辑框文本 → 字符串列表（顿号/逗号/箭头分隔，F05）。
    空或全空白返回 None（不设置）。"""
    parts = [p.strip() for p in ROUTE_SPLIT_RE.split(str(text or "")) if p.strip()]
    return parts or None


# ---------------------------------------------------------------- 结构校验（F08）


def structural_issues(documents) -> list[str]:
    """批次 documents 的结构问题清单（供引擎产出显式结果、API 返回 4xx）。
    只做结构层判断：元素必须是 dict、fields 必须是 dict、doc_type 必须是字符串。"""
    issues: list[str] = []
    if not isinstance(documents, list):
        return ["documents 必须是列表"]
    for i, doc in enumerate(documents):
        if not isinstance(doc, dict):
            issues.append(f"documents[{i}] 不是对象（收到 {type(doc).__name__}）")
            continue
        fields = doc.get("fields")
        if fields is not None and not isinstance(fields, dict):
            issues.append(f"documents[{i}].fields 必须是对象（收到 {type(fields).__name__}）")
        doc_type = doc.get("doc_type")
        if doc_type is not None and not isinstance(doc_type, str):
            issues.append(f"documents[{i}].doc_type 必须是字符串（收到 {type(doc_type).__name__}）")
    return issues


def sanitize_documents(documents: list) -> tuple[list, list]:
    """返回 (可用单证列表, 被剔除的描述列表)：剔除结构非法的元素（不静默丢弃，
    剔除清单由引擎转成显式核验结果，修复 F08"坏数据拖垮整批"）。"""
    ok, dropped = [], []
    for i, doc in enumerate(documents if isinstance(documents, list) else []):
        if not isinstance(doc, dict):
            dropped.append(f"documents[{i}]（非对象）")
            continue
        clean = dict(doc)
        fields = clean.get("fields")
        if fields is not None and not isinstance(fields, dict):
            dropped.append(f"documents[{i}].fields（类型 {type(fields).__name__}）")
            clean["fields"] = {}
        ok.append(clean)
    return ok, dropped
