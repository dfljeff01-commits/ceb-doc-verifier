# -*- coding: utf-8 -*-
"""班列统一编号引擎测试（数据核对模块v1 任务书 §一）。

覆盖：9条真实记录编号回归（夹具）、10-10临/图专项、兜底去重后缀、
EST预估编号格式、反解析往返、非法输入拒绝。
运行：pytest test_train_number.py -v
"""

import json
from datetime import date
from pathlib import Path

import pytest

import train_number
from train_number import TrainNumberError

FIXTURE = json.loads((Path(__file__).parent / "sample_data" / "datacheck"
                      / "datacheck_trips_v1.json").read_text(encoding="utf-8"))
CODE_MAP = FIXTURE["code_map"]


# ---------------------------------------------------------------- 9条回归

def test_nine_records_number_regression():
    """任务书 §一：9条真实记录逐一断言编号（含脱敏口径说明见夹具README）。"""
    existing: set[str] = set()
    for rec, expected in zip(FIXTURE["records"], FIXTURE["expected_numbers"]):
        no, suffix = train_number.build_number(
            rec["dep_date"], rec["station"], rec["port"], rec["dest"],
            rec["train_type"], CODE_MAP, existing)
        assert no == expected, f"{rec} 期望 {expected} 实际 {no}"
        assert suffix == 0
        existing.add(no)


def test_ad_hoc_and_scheduled_same_day_distinct():
    """最重要的专项回归：10-10 临/图 日期、发站、口岸、目的地完全相同，
    仅 L/T 不同，必须生成两个不同编号，不能合并或冲突（任务书 §一）。"""
    l_no, _ = train_number.build_number("2025-10-10", "PW", "MZL", "RU", "L",
                                        CODE_MAP)
    t_no, _ = train_number.build_number("2025-10-10", "PW", "MZL", "RU", "T",
                                        CODE_MAP)
    assert l_no == "20251010-PW-MZL-RU-L"
    assert t_no == "20251010-PW-MZL-RU-T"
    assert l_no != t_no


def test_dedup_suffix_appended_only_on_conflict():
    base = train_number.build_base("2025-10-10", "PW", "MZL", "RU", "T", CODE_MAP)
    assert base == "20251010-PW-MZL-RU-T"
    # 无冲突：无后缀
    assert train_number.pick_unique(base, set()) == (base, 0)
    # 冲突：-01、-02；跳过已占用序号
    assert train_number.pick_unique(base, {base}) == (base + "-01", 1)
    assert train_number.pick_unique(
        base, {base, base + "-01", base + "-03"}) == (base + "-02", 2)


def test_est_number_format_and_official_projection():
    no, suffix = train_number.build_est("2025-10-15", "PW", "MZL", "RU", "T",
                                        CODE_MAP)
    assert no == "EST-20251015-PW-MZL-RU-T" and suffix == 0
    assert train_number.official_of(no) == "20251015-PW-MZL-RU-T"
    # EST 冲突同样加后缀
    no2, sfx = train_number.build_est("2025-10-15", "PW", "MZL", "RU", "T",
                                      CODE_MAP, {no})
    assert no2 == "EST-20251015-PW-MZL-RU-T-01" and sfx == 1


# ---------------------------------------------------------------- 反解析

def test_parse_number_roundtrip():
    parsed = train_number.parse_number("20250809-ZD-HGS-ZY-T")
    assert parsed == {"is_est": False, "dep_date": date(2025, 8, 9),
                      "station": "ZD", "port": "HGS", "dest": "ZY",
                      "train_type": "T", "suffix": 0}
    parsed_est = train_number.parse_number("EST-20251015-PW-MZL-RU-L-02")
    assert parsed_est["is_est"] is True and parsed_est["train_type"] == "L"
    assert parsed_est["suffix"] == 2


@pytest.mark.parametrize("bad", ["", "garbage", "2025-07-18-PW", 
                                 "20251332-PW-MZL-RU-T",        # 假日期
                                 "20250718-PW-MZL-RU-X",        # 类型非法
                                 "est-20250718-pw-mzl-ru-t"])   # 小写会被归一后通过
def test_parse_number_tolerance(bad):
    parsed = train_number.parse_number(bad)
    if bad == "est-20250718-pw-mzl-ru-t":
        assert parsed is not None and parsed["is_est"] is True   # 归一化大小写
    else:
        assert parsed is None


# ---------------------------------------------------------------- 非法输入

def test_invalid_inputs_rejected():
    with pytest.raises(TrainNumberError, match="发运日期"):
        train_number.build_number("20250230", "PW", "MZL", "RU", "T", CODE_MAP)
    with pytest.raises(TrainNumberError, match="发运日期"):
        train_number.build_number("not-a-date", "PW", "MZL", "RU", "T", CODE_MAP)
    with pytest.raises(TrainNumberError, match="车次性质"):
        train_number.build_number("2025-07-18", "PW", "MZL", "RU", "X", CODE_MAP)
    with pytest.raises(TrainNumberError, match="未登记"):
        train_number.build_number("2025-07-18", "XX", "MZL", "RU", "T", CODE_MAP)
    with pytest.raises(TrainNumberError, match="未登记"):
        train_number.build_number("2025-07-18", "PW", "MZL", "US", "T", CODE_MAP)
    with pytest.raises(TrainNumberError, match="不合法"):
        # 缩写含连字符：会破坏编号字段分隔，必须拒绝
        train_number.build_number("2025-07-18", "P-W", "MZL", "RU", "T", CODE_MAP)


def test_train_type_aliases():
    assert train_number.normalize_train_type("临") == "L"
    assert train_number.normalize_train_type("图") == "T"
    assert train_number.normalize_train_type("t") == "T"
    assert train_number.coerce_date("2025-07-18") == date(2025, 7, 18)
    assert train_number.coerce_date("20250718") == date(2025, 7, 18)
