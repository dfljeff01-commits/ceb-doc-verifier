# -*- coding: utf-8 -*-
"""班列统一编号引擎（数据核对模块v1）——纯函数，不依赖数据库。

编号规则（数据核对模块任务书v1 §一）：
    [发运日期YYYYMMDD]-[发站缩写]-[口岸缩写]-[目的国/地区缩写]-[L/T]
    L = 临时增开列车，T = 计划内（图定）列车
兜底去重（§一）：同"日期+发站+口岸+目的地+L/T"已存在时，末尾追加
    -01 / -02 序号后缀区分；正常情况不显示后缀，仅真正冲突时触发。
预估编号（§一）：发运日期未确定前登记预付款等信息用，正式编号前加
    EST- 前缀；发运日期确定后生成正式编号并保留映射（见 train_store）。

缩写代码表由调用方传入（DB train_code_dict 表，见 train_store.load_code_map，
本模块零硬编码）；缩写内不允许出现连字符（连字符是编号分隔符）。
"""

from __future__ import annotations

import re
from datetime import date, datetime

EST_PREFIX = "EST-"

TYPE_AD_HOC = "L"    # 临时增开列车
TYPE_SCHEDULED = "T"  # 计划内（图定）列车
TRAIN_TYPES = (TYPE_AD_HOC, TYPE_SCHEDULED)

# 代码类别（与 train_code_dict.category 一致）
CODE_CATEGORIES = ("station", "port", "dest")

# 类别中文名（报错提示用）
CATEGORY_LABELS = {"station": "发站", "port": "口岸", "dest": "目的地"}

# 车次性质标记归一：L/T 及常见中文写法（导入解析与手工录入共用同一口径）
_TRAIN_TYPE_ALIASES = {
    "L": TYPE_AD_HOC, "T": TYPE_SCHEDULED,
    "临": TYPE_AD_HOC, "临时": TYPE_AD_HOC, "临线": TYPE_AD_HOC,
    "图": TYPE_SCHEDULED, "图定": TYPE_SCHEDULED, "计划": TYPE_SCHEDULED,
}

# 缩写只允许大写字母/数字——连字符是编号字段分隔符，绝不能出现在缩写里
_CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")

# 编号反解析（含 EST- 前缀与 -NN 序号后缀两种可选成分）
_NUMBER_RE = re.compile(
    r"^(EST-)?(\d{8})-([A-Z0-9]{1,8})-([A-Z0-9]{1,8})-([A-Z0-9]{1,8})"
    r"-([LT])(?:-(\d{2}))?$")


class TrainNumberError(ValueError):
    """编号生成/校验错误（未登记缩写、非法日期等）。"""


def is_valid_code(code) -> bool:
    """缩写格式校验（不查字典）：1-8 位大写字母/数字，不含连字符。
    字典登记入口（train_store.upsert_code）与编号生成共用同一格式口径。"""
    return bool(_CODE_RE.match(str(code or "").strip().upper()))


def normalize_train_type(value) -> str:
    """车次性质标记归一为 L/T；支持 临/图 等中文写法，其余报错。"""
    text = str(value or "").strip()
    mapped = _TRAIN_TYPE_ALIASES.get(text) or _TRAIN_TYPE_ALIASES.get(text.upper())
    if not mapped:
        raise TrainNumberError(
            f"车次性质标记不合法：{value!r}（应为 L=临时增开 / T=计划内图定）")
    return mapped


def coerce_date(value) -> date:
    """发运日期归一为 date：接受 date/datetime、YYYY-MM-DD、YYYYMMDD。

    2025.7.18、2025/10/10临 等 Excel 口径的宽容解析在 datacheck_import
    （导入域）；本函数保持严格，避免把模糊输入静默变成错误编号。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise TrainNumberError(
        f"发运日期不合法：{value!r}（应为 YYYY-MM-DD 或 YYYYMMDD 的真实日历日期）")


def _check_code(value: str, category: str, code_map: dict) -> str:
    """缩写合法性双重校验：格式（无连字符）+ 已在代码字典登记且启用。"""
    code = str(value or "").strip().upper()
    if not _CODE_RE.match(code):
        raise TrainNumberError(
            f"{CATEGORY_LABELS[category]}缩写不合法：{value!r}"
            f"（只允许 1-8 位大写字母/数字，不含连字符）")
    known = code_map.get(category) or {}
    if code not in known:
        sample = "、".join(f"{c}={n}" for c, n in sorted(known.items())) or "（字典为空）"
        raise TrainNumberError(
            f"未登记的{CATEGORY_LABELS[category]}缩写：{code}，"
            f"请先在代码字典中登记（现有：{sample}）")
    return code


def build_base(dep_date, station_code, port_code, dest_code, train_type,
               code_map: dict) -> str:
    """生成无序号后缀的正式编号主干（含 EST- 前缀的预估主干用 build_est）。"""
    d = coerce_date(dep_date)
    ttype = normalize_train_type(train_type)
    station = _check_code(station_code, "station", code_map)
    port = _check_code(port_code, "port", code_map)
    dest = _check_code(dest_code, "dest", code_map)
    return f"{d:%Y%m%d}-{station}-{port}-{dest}-{ttype}"


def suffix_part(suffix: int) -> str:
    """序号后缀文本：0=无后缀（正常情况不显示），1 起 = -01 / -02 …"""
    return "" if int(suffix) <= 0 else f"-{int(suffix):02d}"


def pick_unique(base: str, existing: set[str]) -> tuple[str, int]:
    """兜底去重：base 空闲直接用；否则取最小空闲 -01/-02… 序号。

    返回 (完整编号, 序号)。existing 为同主干已占用编号的集合。
    """
    if base not in existing:
        return base, 0
    n = 1
    while f"{base}-{n:02d}" in existing:
        n += 1
    return f"{base}-{n:02d}", n


def build_number(dep_date, station_code, port_code, dest_code, train_type,
                 code_map: dict, existing: set[str] | None = None) -> tuple[str, int]:
    """生成正式编号（含兜底去重），返回 (编号, 序号)。"""
    base = build_base(dep_date, station_code, port_code, dest_code,
                      train_type, code_map)
    return pick_unique(base, existing or set())


def build_est(dep_date, station_code, port_code, dest_code, train_type,
              code_map: dict, existing: set[str] | None = None) -> tuple[str, int]:
    """生成预估编号（EST- 前缀 + 预估发运日期主干，冲突同样加序号后缀）。"""
    base = build_base(dep_date, station_code, port_code, dest_code,
                      train_type, code_map)
    return pick_unique(EST_PREFIX + base, existing or set())


def parse_number(number: str) -> dict | None:
    """反解析编号 → {is_est, dep_date, station, port, dest, train_type, suffix}。

    兼容 EST- 前缀与 -NN 后缀；格式不符返回 None（导入匹配的静默路径，
    不抛错——调用方自行决定如何提示）。
    """
    m = _NUMBER_RE.match(str(number or "").strip().upper())
    if not m:
        return None
    est, ymd, station, port, dest, ttype, suffix = m.groups()
    try:
        d = datetime.strptime(ymd, "%Y%m%d").date()
    except ValueError:            # 形如 20251332 的假日期
        return None
    return {
        "is_est": bool(est),
        "dep_date": d,
        "station": station,
        "port": port,
        "dest": dest,
        "train_type": ttype,
        "suffix": int(suffix) if suffix else 0,
    }


def official_of(est_no: str) -> str | None:
    """预估编号 → 对应正式编号主干（去 EST- 前缀与序号后缀）。

    仅表示"预估时点预期的正式主干"，实际正式编号以锁定时生成/匹配的为准
    （发运日期可能变化，映射以 train_est_numbers 表留痕为准）。
    """
    parsed = parse_number(est_no)
    if not parsed or not parsed["is_est"]:
        return None
    d = parsed["dep_date"]
    return f"{d:%Y%m%d}-{parsed['station']}-{parsed['port']}-{parsed['dest']}-{parsed['train_type']}"
