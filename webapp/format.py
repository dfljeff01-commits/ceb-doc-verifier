# -*- coding: utf-8 -*-
"""业务时间格式化（P2-7）：全站统一按北京时间（Asia/Shanghai）展示。

约定：
  - 数据库时间列均为 TIMESTAMPTZ（psycopg 返回带时区的 datetime/UTC）；
  - 字符串时间按 ISO-8601 解析（兼容 ``...Z`` 后缀）；
  - 无时区的 naive 时间视为北京时间（本机写入的本地时间）；
  - 非时间文本原样返回，不做猜测解析。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: 业务时区：中国标准时间（UTC+8，无夏令时）
BUSINESS_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

#: 默认展示格式：到分钟（登录有效期、用户列表等）
DEFAULT_FMT = "%Y-%m-%d %H:%M"

#: 明细场景（操作日志等需要秒级排序的场景）
SECOND_FMT = "%Y-%m-%d %H:%M:%S"


def fmt_dt(value, fmt: str = DEFAULT_FMT, default: str = "—") -> str:
    """把 datetime/date/ISO 字符串格式化为北京时间文本；无法解析时原样返回。"""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = date.fromisoformat(text)
            except ValueError:
                return text          # 非时间文本：原样展示，不猜
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=BUSINESS_TZ)
        else:
            value = value.astimezone(BUSINESS_TZ)
        return value.strftime(fmt)
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)
