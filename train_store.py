# -*- coding: utf-8 -*-
"""数据核对模块存储层（班列统一编号 + 联运结算/补贴对账）。

职责（数据核对模块任务书v1 §一/§二/§三）：
  - 缩写代码字典与核对阈值配置的读写与种子（train_code_dict / app_config）；
  - 班列登记：编号生成（含兜底去重 -01/-02，数据库 UNIQUE 双保险+并发重试）；
  - 预估编号生命周期：创建 EST- → 锁定生成/匹配正式编号 → 该预估编号名下
    预付款单事务批量改挂正式编号（est_ref 留痕，映射行保留不删除）；
  - 结算明细 / 预付款 / 补贴测算的读写（字段允许暂缺，四状态不强制阻断）；
  - trip_detail 组装：基础+结算+预付+补贴+四类核对结果+编号沿革，
    API 与网页本地降级共用同一口径。

设计约定：
  - 正式编号 trip_no 是自然主键，登记后发运日期/线路/类型不可改（编号即身份；
    登记错误走删除重建，避免编号含义漂移）；
  - 预付款 trip_no 不设外键（锁定前可能挂预估编号），本模块写入时校验
    "正式或预估编号必有其一如"（应用层完整性）；
  - 金额字段一律宽容转换（train_recon.to_decimal），缺失按"未填写"处理。
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg2
import psycopg2.extras

import db
import train_number
import train_recon
from train_number import TrainNumberError

# ---------------------------------------------------------------- 种子数据

# 缩写代码字典种子（任务书 §一 已验证的真实缩写；后续增删走 PUT /datacheck/codes）
# 种子字典（任务书§四：10个高频站点；元组 = category, code, name, sort,
# aliases(逗号分隔), country）。dest 中 RU/ZY 为国家层兜底（对外班列编号口径），
# MSK/XLY/XEG/BLS 为具体到站（双表对账精确匹配口径）。
SEED_CODE_DICT = [
    # 发站（3）
    ("station", "PW", "平旺", 1, "", "中国"),
    ("station", "DT", "大同", 2, "", "中国"),
    ("station", "ZD", "中鼎", 3, "", "中国"),
    # 口岸（3）
    ("port", "MZL", "满洲里", 1, "", "中国"),
    ("port", "EL", "二连", 2, "二连浩特", "中国"),
    ("port", "HGS", "霍尔果斯", 3, "霍尔果斯口岸", "中国"),
    # 到站（国家层兜底2 + 具体车站4）
    ("dest", "RU", "俄罗斯", 1, "俄国", ""),
    ("dest", "ZY", "中亚", 2, "", ""),
    ("dest", "MSK", "莫斯科", 3, "Moscow,Москва", "俄罗斯"),
    ("dest", "XLY", "谢利亚季诺", 4, "Селятино,谢利亚季", "俄罗斯"),
    ("dest", "XEG", "谢尔盖利", 5, "Сергели,Сергели", "中亚"),
    ("dest", "BLS", "别雷拉斯特", 6, "Белый Раст,别雷", "俄罗斯"),
]

# 核对阈值种子（任务书 §三：暂定超5%或超5000元触发提示，可配置）
SEED_CONFIG = [
    ("datacheck.recon_warn_pct", 5, "三方对账差异告警百分比阈值（%，超过即提示人工关注）"),
    ("datacheck.recon_warn_amount", 5000, "三方对账差异告警绝对金额阈值（元，超过即提示人工关注）"),
]

CONFIG_PCT_KEY = "datacheck.recon_warn_pct"
CONFIG_AMOUNT_KEY = "datacheck.recon_warn_amount"

# 基础信息允许编辑的字段（日期/线路/类型不在内——编号即身份，见模块 docstring）
TRIP_BASIC_FIELDS = ("wagon_count", "container_40hd", "container_20hd",
                     "teu_total", "goods_name", "route_label", "remark")

# 结算明细可写列（数值列 patch 语义：None=未提供，保留原值）
SETTLEMENT_AMOUNT_COLS = ("rail_freight", "customs_fee", "service_fee",
                          "other_fee", "settle_total", "actual_freight")

# 补贴测算数值列
SUBSIDY_AMOUNT_COLS = tuple(train_recon.SUBSIDY_AMOUNT_FIELDS)


class TrainStoreError(ValueError):
    """数据核对存储层错误（编号不存在、预估已锁定、参照完整性等）。"""


# ---------------------------------------------------------------- 种子与字典

def seed_refs_if_absent() -> dict:
    """字典与配置种子：逐行 ON CONFLICT DO NOTHING（幂等，不覆盖人工修改）。"""
    inserted = {"codes": 0, "config": 0}
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO train_code_dict
                       (category, code, name, sort, aliases, country)
                   VALUES (%s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''))
                   ON CONFLICT (category, code) DO UPDATE SET
                       aliases = COALESCE(train_code_dict.aliases,
                                          EXCLUDED.aliases),
                       country = COALESCE(train_code_dict.country,
                                          EXCLUDED.country)""",
                SEED_CODE_DICT)
            inserted["codes"] = cur.rowcount
            cur.executemany(
                """INSERT INTO app_config (key, value, description)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO NOTHING""",
                [(k, db.jsonb(v), d) for k, v, d in SEED_CONFIG])
            inserted["config"] = cur.rowcount
    return inserted


def load_code_map(active_only: bool = True) -> dict[str, dict[str, str]]:
    """代码字典 → {category: {code: name}}（train_number 编号引擎的入参口径）。"""
    rows = db.query(
        "SELECT category, code, name FROM train_code_dict"
        + (" WHERE active = TRUE" if active_only else "")
        + " ORDER BY category, sort, code")
    code_map: dict[str, dict[str, str]] = {}
    for r in rows:
        code_map.setdefault(r["category"], {})[r["code"]] = r["name"]
    return code_map


def list_codes(active_only: bool = False) -> list[dict]:
    rows = db.query(
        "SELECT category, code, name, sort, active, aliases, country,"
        " updated_by, updated_at FROM train_code_dict"
        + (" WHERE active = TRUE" if active_only else "")
        + " ORDER BY category, sort, code")
    return rows


def upsert_code(category: str, code: str, name: str, sort: int = 0,
                active: bool = True, by: str = "", aliases: str = "",
                country: str = "") -> dict:
    """登记/更新缩写（任务书§一/§四：支持后续增删站点/口岸及别名，不硬编码）。"""
    if category not in train_number.CODE_CATEGORIES:
        raise TrainStoreError(
            f"字典类别不合法：{category!r}（应为 station/port/dest）")
    code = str(code or "").strip().upper()
    if not train_number.is_valid_code(code):
        raise TrainStoreError(
            f"缩写不合法：{code!r}（1-8位大写字母/数字，不含连字符）")
    name = str(name or "").strip()
    if not name:
        raise TrainStoreError("名称不能为空")
    db.query(
        """INSERT INTO train_code_dict (category, code, name, sort, active,
                                       aliases, country, updated_by)
           VALUES (%s, %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''), %s)
           ON CONFLICT (category, code) DO UPDATE SET
               name = EXCLUDED.name, sort = EXCLUDED.sort,
               active = EXCLUDED.active, aliases = EXCLUDED.aliases,
               country = EXCLUDED.country, updated_by = EXCLUDED.updated_by,
               updated_at = now()""",
        (category, code, name, int(sort), bool(active),
         str(aliases or ""), str(country or ""), by or None))
    return {"category": category, "code": code, "name": name,
            "sort": int(sort), "active": bool(active),
            "aliases": str(aliases or ""), "country": str(country or "")}


# ---------------------------------------------------------------- 配置（阈值）

def get_config() -> dict:
    """全部配置项（JSONB 已解码）。"""
    rows = db.query("SELECT key, value, description FROM app_config ORDER BY key")
    return {r["key"]: {"value": r["value"], "description": r["description"]}
            for r in rows}


def set_config(key: str, value, by: str = "") -> None:
    if key not in {k for k, _, _ in SEED_CONFIG}:
        raise TrainStoreError(f"未知配置项：{key!r}")
    db.query(
        """INSERT INTO app_config (key, value, updated_by, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (key) DO UPDATE SET
               value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
               updated_at = now()""",
        (key, db.jsonb(value), by or None))


def thresholds() -> dict:
    """核对阈值（Decimal；缺行回退 DEFAULT_THRESHOLDS 兜底值）。"""
    cfg = get_config()
    pct = train_recon.to_decimal((cfg.get(CONFIG_PCT_KEY) or {}).get("value"))
    amount = train_recon.to_decimal((cfg.get(CONFIG_AMOUNT_KEY) or {}).get("value"))
    return {
        "pct": pct if pct is not None else train_recon.DEFAULT_THRESHOLDS["recon_warn_pct"],
        "amount": amount if amount is not None else train_recon.DEFAULT_THRESHOLDS["recon_warn_amount"],
    }


# ---------------------------------------------------------------- 班列主档

_COMPONENT_COLS = ("dep_date", "station_code", "port_code", "dest_code", "train_type")


def _validate_components(dep_date, station, port, dest, train_type,
                         code_map: dict) -> tuple:
    """编号要素校验并归一（date 对象 + 大写缩写 + L/T），返回五元组。"""
    d = train_number.coerce_date(dep_date)
    ttype = train_number.normalize_train_type(train_type)
    station = train_number._check_code(station, "station", code_map)
    port = train_number._check_code(port, "port", code_map)
    dest = train_number._check_code(dest, "dest", code_map)
    return d, station, port, dest, ttype


def _existing_trip_numbers(cur, d, station, port, dest, ttype) -> set[str]:
    cur.execute(
        "SELECT trip_no FROM train_trips WHERE dep_date = %s AND station_code = %s"
        " AND port_code = %s AND dest_code = %s AND train_type = %s",
        (d, station, port, dest, ttype))
    return {r["trip_no"] for r in cur.fetchall()}


def _int_or_none(value):
    """车数/柜量净化：空串/None→NULL（允许暂缺），数值字符串→整数（导入路径）。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return int(float(text))


def create_trip(dep_date, station_code, port_code, dest_code, train_type,
                created_by: str, basic: dict | None = None,
                code_map: dict | None = None) -> dict:
    """登记班列并生成正式编号（兜底去重：冲突自动 -01/-02）。

    并发口径：应用层先查后插，数据库 UNIQUE 五元组兜底；唯一键冲突时
    重读占用集合再试（最多5次），仍失败则向上抛错。
    """
    cmap = code_map if code_map is not None else load_code_map()
    d, station, port, dest, ttype = _validate_components(
        dep_date, station_code, port_code, dest_code, train_type, cmap)
    basic = basic or {}
    last_error: Exception | None = None
    for _ in range(5):
        with db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                existing = _existing_trip_numbers(cur, d, station, port, dest, ttype)
                number, suffix = train_number.pick_unique(
                    train_number.build_base(d, station, port, dest, ttype, cmap),
                    existing)
                try:
                    cur.execute(
                        """INSERT INTO train_trips
                               (trip_no, dep_date, station_code, port_code, dest_code,
                                train_type, suffix, route_label, wagon_count,
                                container_40hd, container_20hd, teu_total,
                                goods_name, remark, created_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING *""",
                        (number, d, station, port, dest, ttype, suffix,
                         str(basic.get("route_label") or ""),
                         _int_or_none(basic.get("wagon_count")),
                         _int_or_none(basic.get("container_40hd")),
                         _int_or_none(basic.get("container_20hd")),
                         train_recon.to_decimal(basic.get("teu_total")),
                         str(basic.get("goods_name") or ""),
                         str(basic.get("remark") or ""), created_by or None))
                    trip = dict(cur.fetchone())
                except psycopg2.errors.UniqueViolation as exc:
                    last_error = exc
                    continue  # 并发抢占：重读占用集合再生成
                # 匹配到的未锁定预估编号（供界面提示"是否锁定关联"）
                cur.execute(
                    """SELECT est_no FROM train_est_numbers
                       WHERE official_no IS NULL AND dep_date_est = %s
                         AND station_code = %s AND port_code = %s
                         AND dest_code = %s AND train_type = %s
                       ORDER BY created_at""",
                    (d, station, port, dest, ttype))
                trip["matching_est_nos"] = [r["est_no"] for r in cur.fetchall()]
                return trip
    raise TrainStoreError(
        f"编号生成并发冲突，重试多次仍失败：{last_error}")


def get_trip(trip_no: str) -> dict | None:
    rows = db.query("SELECT * FROM train_trips WHERE trip_no = %s", (trip_no,))
    return rows[0] if rows else None


def list_trips(date_from=None, date_to=None, station: str | None = None,
               port: str | None = None, dest: str | None = None,
               limit: int = 200) -> list[dict]:
    conditions, params = [], []
    if date_from:
        conditions.append("dep_date >= %s")
        params.append(train_number.coerce_date(date_from))
    if date_to:
        conditions.append("dep_date <= %s")
        params.append(train_number.coerce_date(date_to))
    if station:
        conditions.append("station_code = %s")
        params.append(str(station).upper())
    if port:
        conditions.append("port_code = %s")
        params.append(str(port).upper())
    if dest:
        conditions.append("dest_code = %s")
        params.append(str(dest).upper())
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(max(1, min(int(limit), 1000)))
    return db.query(
        "SELECT * FROM train_trips " + where +
        " ORDER BY dep_date DESC, train_type, suffix LIMIT %s", tuple(params))


def update_trip_basic(trip_no: str, fields: dict, by: str) -> tuple[dict, dict]:
    """编辑基础信息（仅 wagon/柜量/TEU/品名/路线标签/备注），返回 (before, after)。"""
    before = get_trip(trip_no)
    if not before:
        raise TrainStoreError(f"班列编号不存在：{trip_no}")
    sets, params = [], []
    for col in TRIP_BASIC_FIELDS:
        if col in fields:
            sets.append(f"{col} = %s")
            value = fields[col]
            if col in ("wagon_count", "container_40hd", "container_20hd"):
                value = int(value) if str(value or "").strip() else None
            elif col == "teu_total":
                value = train_recon.to_decimal(value)
            else:
                value = str(value if value is not None else "")
            params.append(value)
    if not sets:
        return before, before
    params.extend([by or None, trip_no])
    rows = db.query(
        f"UPDATE train_trips SET {', '.join(sets)}, updated_by = %s,"
        " updated_at = now() WHERE trip_no = %s RETURNING *", tuple(params))
    return before, rows[0]


# ---------------------------------------------------------------- 结算明细

def get_settlement(trip_no: str) -> dict | None:
    rows = db.query("SELECT * FROM train_settlements WHERE trip_no = %s", (trip_no,))
    return rows[0] if rows else None


def upsert_settlement(trip_no: str, fields: dict, by: str) -> tuple[dict, dict]:
    """写结算明细（patch 语义：只更新 fields 中提供的键；数值/日期 None=未填写）。

    返回 (before, after) 供调用方审计留痕；record_status 四状态：
    confirmed/pending/not_found/business_missing。
    """
    if not get_trip(trip_no):
        raise TrainStoreError(f"班列编号不存在：{trip_no}")
    before = get_settlement(trip_no)

    def _num(col):
        if col not in fields:
            return None
        return train_recon.to_decimal(fields[col])

    status = fields.get("record_status")
    if status is not None and status not in train_recon.RECORD_STATUS_LABELS:
        raise TrainStoreError(f"记录状态不合法：{status!r}")
    paid_at = fields.get("actual_paid_at")
    paid_at = train_number.coerce_date(paid_at) if str(paid_at or "").strip() else None
    diff_reason = (str(fields["diff_reason"]) if "diff_reason" in fields else None)
    field_status = fields.get("field_status")

    if before is None:      # 首次写入：未提供的键落列默认值
        row = db.query(
            """INSERT INTO train_settlements
                   (trip_no, rail_freight, customs_fee, service_fee, other_fee,
                    settle_total, actual_freight, actual_paid_at, diff_reason,
                    record_status, field_status, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (trip_no, _num("rail_freight"), _num("customs_fee"),
             _num("service_fee"), _num("other_fee"), _num("settle_total"),
             _num("actual_freight"), paid_at, diff_reason or "",
             status or train_recon.RECORD_PENDING,
             db.jsonb(field_status) if field_status is not None else db.jsonb({}),
             by or None))
        return {}, row[0]
    # 已有记录：动态 UPDATE（只动提供的键，patch 不清空）
    sets, params = [], []
    for col in SETTLEMENT_AMOUNT_COLS:
        if col in fields:
            sets.append(f"{col} = %s")
            params.append(_num(col))
    if "actual_paid_at" in fields:
        sets.append("actual_paid_at = %s")
        params.append(paid_at)
    if diff_reason is not None:
        sets.append("diff_reason = %s")
        params.append(diff_reason)
    if status is not None:
        sets.append("record_status = %s")
        params.append(status)
    if field_status is not None:
        sets.append("field_status = %s")
        params.append(db.jsonb(field_status))
    params.extend([by or None, trip_no])
    row = db.query(
        f"UPDATE train_settlements SET {', '.join(sets)}, updated_by = %s,"
        " updated_at = now() WHERE trip_no = %s RETURNING *", tuple(params))
    return before, row[0]


# ---------------------------------------------------------------- 预付款

def _require_trip_or_est(trip_no: str) -> bool:
    """预付款归属校验：正式编号或（未锁定/已锁定）预估编号至少存在其一。"""
    if str(trip_no or "").startswith(train_number.EST_PREFIX):
        rows = db.query("SELECT 1 AS ok FROM train_est_numbers WHERE est_no = %s",
                        (trip_no,))
    else:
        if not train_number.parse_number(trip_no):
            raise TrainStoreError(
                f"编号格式不合法：{trip_no!r}（正式编号或 EST- 预估编号）")
        rows = db.query("SELECT 1 AS ok FROM train_trips WHERE trip_no = %s",
                        (trip_no,))
    if not rows:
        raise TrainStoreError(
            f"编号不存在（班列或预估编号均未登记）：{trip_no}")
    return str(trip_no).startswith(train_number.EST_PREFIX)


def add_prepayment(trip_no: str, paid_at, amount, remark: str,
                   by: str) -> dict:
    """登记一笔预付款；发运日期未定时 trip_no 传预估编号（est_ref 自动留痕）。"""
    is_est = _require_trip_or_est(trip_no)
    paid = train_number.coerce_date(paid_at) if str(paid_at or "").strip() else None
    value = train_recon.to_decimal(amount)
    if value is None:
        raise TrainStoreError("预付金额不能为空且必须是数值")
    rows = db.query(
        """INSERT INTO train_prepayments (trip_no, est_ref, paid_at, amount,
                                          remark, created_by)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (trip_no, trip_no if is_est else None, paid, value,
         str(remark or ""), by or None))
    return rows[0]


def update_prepayment(pid: int, fields: dict, by: str = "") -> tuple[dict, dict]:
    rows = db.query("SELECT * FROM train_prepayments WHERE id = %s", (pid,))
    before = rows[0] if rows else None
    if not before:
        raise TrainStoreError(f"预付款记录不存在：id={pid}")
    paid_at = fields.get("paid_at", before["paid_at"])
    paid = train_number.coerce_date(paid_at) if str(paid_at or "").strip() else None
    amount = (train_recon.to_decimal(fields["amount"])
              if "amount" in fields else before["amount"])
    remark = str(fields.get("remark", before["remark"]) or "")
    after = db.query(
        """UPDATE train_prepayments SET paid_at = %s, amount = %s, remark = %s,
           updated_at = now() WHERE id = %s RETURNING *""",
        (paid, amount, remark, pid))[0]
    return before, after


def delete_prepayment(pid: int) -> dict:
    rows = db.query("DELETE FROM train_prepayments WHERE id = %s RETURNING *", (pid,))
    if not rows:
        raise TrainStoreError(f"预付款记录不存在：id={pid}")
    return rows[0]


def list_prepayments(trip_no: str) -> list[dict]:
    return db.query(
        "SELECT * FROM train_prepayments WHERE trip_no = %s ORDER BY paid_at, id",
        (trip_no,))


# ---------------------------------------------------------------- 补贴测算

def get_subsidy(trip_no: str) -> dict | None:
    rows = db.query("SELECT * FROM train_subsidies WHERE trip_no = %s", (trip_no,))
    return rows[0] if rows else None


def upsert_subsidy(trip_no: str, fields: dict, by: str) -> tuple[dict, dict]:
    """写补贴测算（patch 语义同结算明细：只更新提供的键），返回 (before, after)。"""
    if not get_trip(trip_no):
        raise TrainStoreError(f"班列编号不存在：{trip_no}")
    before = get_subsidy(trip_no)

    def _num(col):
        if col not in fields:
            return None
        return train_recon.to_decimal(fields[col])

    status = fields.get("record_status")
    if status is not None and status not in train_recon.RECORD_STATUS_LABELS:
        raise TrainStoreError(f"记录状态不合法：{status!r}")
    diff_reason = (str(fields["diff_reason"]) if "diff_reason" in fields else None)
    field_status = fields.get("field_status")

    if before is None:
        row = db.query(
            """INSERT INTO train_subsidies
                   (trip_no, dt_supply_100, dt_supply_70, ly_advance_100,
                    ly_recover_70, auth_confirm_100, auth_advance_70,
                    auth_remain_30, forecast_diff, diff_reason,
                    record_status, field_status, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (trip_no, _num("dt_supply_100"), _num("dt_supply_70"),
             _num("ly_advance_100"), _num("ly_recover_70"),
             _num("auth_confirm_100"), _num("auth_advance_70"),
             _num("auth_remain_30"), _num("forecast_diff"),
             diff_reason or "", status or train_recon.RECORD_PENDING,
             db.jsonb(field_status) if field_status is not None else db.jsonb({}),
             by or None))
        return {}, row[0]
    sets, params = [], []
    for col in SUBSIDY_AMOUNT_COLS:
        if col in fields:
            sets.append(f"{col} = %s")
            params.append(_num(col))
    if diff_reason is not None:
        sets.append("diff_reason = %s")
        params.append(diff_reason)
    if status is not None:
        sets.append("record_status = %s")
        params.append(status)
    if field_status is not None:
        sets.append("field_status = %s")
        params.append(db.jsonb(field_status))
    params.extend([by or None, trip_no])
    row = db.query(
        f"UPDATE train_subsidies SET {', '.join(sets)}, updated_by = %s,"
        " updated_at = now() WHERE trip_no = %s RETURNING *", tuple(params))
    return before, row[0]


def mark_suspect(trip_no: str, review_status: str, note: str,
                 by: str) -> tuple[dict, dict]:
    """存疑标记（任务书 §三：可标记"存疑，需人工复核"，不直接采信原始数值）。"""
    if review_status not in train_recon.REVIEW_STATUS_LABELS:
        raise TrainStoreError(f"复核状态不合法：{review_status!r}")
    if not get_subsidy(trip_no):
        upsert_subsidy(trip_no, {}, by)   # 尚无补贴行时先建空行（字段全空=未填写）
    before = get_subsidy(trip_no)
    after = db.query(
        """UPDATE train_subsidies SET review_status = %s, suspect_note = %s,
           updated_by = %s, updated_at = now()
           WHERE trip_no = %s RETURNING *""",
        (review_status, str(note or ""), by or None, trip_no))[0]
    return before, after


def set_anomalies(trip_no: str, anomalies: list, by: str = "") -> None:
    """写入系统检测异常快照（导入/保存时刷新；历史留痕，界面另做实时计算）。"""
    db.query(
        """UPDATE train_subsidies SET anomalies = %s, updated_by = %s,
           updated_at = now() WHERE trip_no = %s""",
        (db.jsonb(anomalies or []), by or None, trip_no))


# ---------------------------------------------------------------- 预估编号

def create_est(dep_date, station_code, port_code, dest_code, train_type,
               created_by: str, code_map: dict | None = None) -> dict:
    """创建预估编号（EST- 前缀；冲突同样 -01/-02 兜底）。"""
    cmap = code_map if code_map is not None else load_code_map()
    d, station, port, dest, ttype = _validate_components(
        dep_date, station_code, port_code, dest_code, train_type, cmap)
    last_error: Exception | None = None
    for _ in range(5):
        with db.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT est_no FROM train_est_numbers
                       WHERE dep_date_est=%s AND station_code=%s AND port_code=%s
                         AND dest_code=%s AND train_type=%s""",
                    (d, station, port, dest, ttype))
                existing = {r["est_no"] for r in cur.fetchall()}
                est_no, suffix = train_number.pick_unique(
                    train_number.EST_PREFIX + train_number.build_base(
                        d, station, port, dest, ttype, cmap),
                    existing)
                try:
                    cur.execute(
                        """INSERT INTO train_est_numbers
                               (est_no, dep_date_est, station_code, port_code,
                                dest_code, train_type, created_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (est_no, d, station, port, dest, ttype, created_by or None))
                    return dict(cur.fetchone())
                except psycopg2.errors.UniqueViolation as exc:
                    last_error = exc
                    continue
    raise TrainStoreError(f"预估编号生成并发冲突，重试多次仍失败：{last_error}")


def get_est(est_no: str) -> dict | None:
    rows = db.query("SELECT * FROM train_est_numbers WHERE est_no = %s", (est_no,))
    return rows[0] if rows else None


def list_est(locked: bool | None = None, limit: int = 200) -> list[dict]:
    conditions = [] if locked is None else \
        ["official_no IS NOT NULL" if locked else "official_no IS NULL"]
    where = ("WHERE " + conditions[0]) if conditions else ""
    return db.query(
        f"SELECT * FROM train_est_numbers {where}"
        " ORDER BY created_at DESC LIMIT %s", (max(1, min(int(limit), 1000)),))


def lock_est(est_no: str, dep_date, by: str,
             code_map: dict | None = None) -> dict:
    """锁定预估编号：生成或匹配正式编号并建立映射，单事务内把该预估编号
    名下预付款批量改挂正式编号（est_ref 保留原始编号留痕）。

    映射行不删除（任务书 §一：留痕 预估编号/正式编号/锁定时间/操作人）。
    发运日期确定后若已有人工登记了同要素班列，直接关联已存在编号。
    """
    cmap = code_map if code_map is not None else load_code_map()
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM train_est_numbers WHERE est_no = %s FOR UPDATE",
                        (est_no,))
            est = cur.fetchone()
            if not est:
                raise TrainStoreError(f"预估编号不存在：{est_no}")
            if est["official_no"]:
                raise TrainStoreError(
                    f"预估编号已锁定为 {est['official_no']}，不可重复锁定")
            d = train_number.coerce_date(dep_date)
            station, port, dest, ttype = (est["station_code"], est["port_code"],
                                          est["dest_code"], est["train_type"])
            # 已存在同要素正式班列 → 直接关联（优先无后缀主干）
            cur.execute(
                """SELECT trip_no FROM train_trips
                   WHERE dep_date=%s AND station_code=%s AND port_code=%s
                     AND dest_code=%s AND train_type=%s
                   ORDER BY suffix LIMIT 1""",
                (d, station, port, dest, ttype))
            hit = cur.fetchone()
            if hit:
                official, created = hit["trip_no"], False
            else:
                existing = _existing_trip_numbers(cur, d, station, port, dest, ttype)
                official, _suffix = train_number.pick_unique(
                    train_number.build_base(d, station, port, dest, ttype, cmap),
                    existing)
                cur.execute(
                    """INSERT INTO train_trips (trip_no, dep_date, station_code,
                           port_code, dest_code, train_type, suffix, created_by,
                           route_label, remark)
                       VALUES (%s,%s,%s,%s,%s,%s,0,%s,'','(由预估编号锁定生成)')""",
                    (official, d, station, port, dest, ttype, by or None))
                created = True
            cur.execute(
                """UPDATE train_est_numbers
                   SET official_no=%s, locked_at=now(), locked_by=%s
                   WHERE est_no=%s""",
                (official, by or None, est_no))
            cur.execute(
                "UPDATE train_prepayments SET trip_no=%s WHERE trip_no=%s",
                (official, est_no))
            relinked = cur.rowcount
    return {"est_no": est_no, "official_no": official,
            "created_trip": created, "relinked_prepays": relinked,
            "locked_by": by or "", "locked_at": datetime.now().astimezone()
            .isoformat(timespec="seconds")}


# ---------------------------------------------------------------- 查询与组装

def lookup(dep_date=None, station: str | None = None, port: str | None = None,
           dest: str | None = None) -> dict:
    """财务编号查询（任务书 §四）：返回正式编号；无正式编号时给出预估编号提示。"""
    trips = list_trips(date_from=dep_date, date_to=dep_date,
                       station=station, port=port, dest=dest, limit=100)
    est_conditions, est_params = [], []
    if dep_date:
        est_conditions.append("dep_date_est = %s")
        est_params.append(train_number.coerce_date(dep_date))
    if station:
        est_conditions.append("station_code = %s")
        est_params.append(str(station).upper())
    if port:
        est_conditions.append("port_code = %s")
        est_params.append(str(port).upper())
    if dest:
        est_conditions.append("dest_code = %s")
        est_params.append(str(dest).upper())
    where = ("WHERE " + " AND ".join(est_conditions)) if est_conditions else ""
    ests = db.query(
        f"SELECT est_no, dep_date_est, station_code, port_code, dest_code,"
        f" train_type, official_no, locked_at FROM train_est_numbers {where}"
        " ORDER BY created_at LIMIT 100", tuple(est_params))
    return {"trips": trips, "ests": ests,
            "message": (f"尚无正式编号，仅有预估编号 "
                        f"{ '、'.join(e['est_no'] for e in ests if not e['official_no']) }"
                        if not trips and any(not e["official_no"] for e in ests) else "")}


def trips_with_subsidies(date_from=None, date_to=None,
                         limit: int = 500) -> list[dict]:
    """台账联查（三方对账/重复值检测页的数据源）：班列主档 LEFT JOIN 补贴。"""
    conditions, params = [], []
    if date_from:
        conditions.append("t.dep_date >= %s")
        params.append(train_number.coerce_date(date_from))
    if date_to:
        conditions.append("t.dep_date <= %s")
        params.append(train_number.coerce_date(date_to))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(max(1, min(int(limit), 2000)))
    return db.query(
        """SELECT t.trip_no, t.dep_date, t.station_code, t.port_code, t.dest_code,
                  t.train_type, t.suffix, t.goods_name, t.route_label,
                  s.dt_supply_100, s.dt_supply_70, s.ly_advance_100,
                  s.ly_recover_70, s.auth_confirm_100, s.auth_advance_70,
                  s.auth_remain_30, s.forecast_diff, s.diff_reason,
                  s.review_status, s.suspect_note, s.anomalies, s.record_status
           FROM train_trips t
           LEFT JOIN train_subsidies s ON s.trip_no = t.trip_no
           """ + where +
        " ORDER BY t.dep_date, t.train_type, t.suffix LIMIT %s",
        tuple(params))


def est_history_for_trip(trip_no: str) -> list[dict]:
    """编号沿革：锁定到该正式编号的全部预估编号（映射留痕回查）。"""
    return db.query(
        """SELECT est_no, dep_date_est, locked_at, locked_by, created_at
           FROM train_est_numbers WHERE official_no = %s ORDER BY locked_at""",
        (trip_no,))


def refresh_anomalies(by: str = "", trip_nos: list[str] | None = None) -> int:
    """重算重复值异常快照并写入 subsidies.anomalies（导入/保存后调用）。

    对全部班列（或指定子集）按发运日期做相邻重复值检测——快照仅留痕，
    界面展示以实时计算为准。返回更新的行数。
    """
    rows = trips_with_subsidies()
    if trip_nos is not None:
        wanted = set(trip_nos)
        rows = [r for r in rows if r["trip_no"] in wanted]
    flags = train_recon.duplicate_neighbor_flags(rows)
    updated = 0
    for trip_no, items in flags.items():
        set_anomalies(trip_no, items, by)
        updated += 1
    return updated


def trip_detail(trip_no: str) -> dict | None:
    """班列完整详情：基础+结算+预付+补贴+核对结果+编号沿革（API与网页共用）。"""
    trip = get_trip(trip_no)
    if not trip:
        return None
    settlement = get_settlement(trip_no) or {}
    prepays = list_prepayments(trip_no)
    subsidy = get_subsidy(trip_no) or {}
    thr = thresholds()
    checks = {
        "prepay": train_recon.prepay_check(settlement.get("settle_total"), prepays),
        "settle_actual": train_recon.settle_actual_check(
            settlement.get("settle_total"), settlement.get("actual_freight"),
            settlement.get("diff_reason")),
        "three_way": train_recon.three_way_check(subsidy, thr["pct"], thr["amount"]),
        "missing_settlement_fields": train_recon.missing_field_names(
            settlement, train_recon.SETTLEMENT_AMOUNT_FIELDS),
        "missing_subsidy_fields": train_recon.missing_field_names(subsidy),
    }
    # 实时重复值检测（针对该班列与相邻班列）
    neighbors = trips_with_subsidies(date_from=trip["dep_date"],
                                     date_to=trip["dep_date"])
    dup = train_recon.duplicate_neighbor_flags(neighbors).get(trip_no, [])
    return {"trip": trip, "settlement": settlement, "prepays": prepays,
            "subsidy": subsidy, "checks": checks,
            "duplicate_flags": dup, "est_history": est_history_for_trip(trip_no),
            "thresholds": {"pct": str(thr["pct"]), "amount": str(thr["amount"])}}
