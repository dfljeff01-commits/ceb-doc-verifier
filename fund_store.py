# -*- coding: utf-8 -*-
"""资金/费用批次存储层（数据核对模块v1.1）。

职责：
  - 批次登记：批次号生成（含兜底去重）、创建批次主档 + 班列关联（一次写入）；
  - 批次读写：列表/详情、分摊金额更新、删除（关联 ON DELETE CASCADE）；
  - participates_in_freight_recon 人工改写：按用途带默认值，改写需原因+留痕；
  - 核对组装：批次详情附带 batch_coverage_check（取关联班列结算/费用科目）；
  - 单趟车视角：trip_batch_links 供台账详情标注"款项来自批次"；
  - 孤儿检测：汇总批次关联/在册编号/旧表预付编号，交 fund_recon 判定。

与 v1 共存：本模块不改动 train_prepayments；简单1:1仍可走旧表录入。
金额宽容转换（train_recon.to_decimal）；编号沿用 v1 口径（正式/EST均可）。
"""

from __future__ import annotations

from datetime import date

import psycopg2

import db
import fund_recon
import train_number
import train_recon

# cost 批次允许的费用类目（与 db CHECK 口径一致）
COST_CATEGORIES = ["铁路运费", "报关费", "服务费", "其他"]


class FundStoreError(ValueError):
    """批次存储层错误（批次号冲突、编号缺失、用途不合法等）。"""


# ---------------------------------------------------------------- 批次号

def generate_batch_id(batch_type: str, paid_at, purpose: str,
                      seq: int = 1) -> str:
    """生成批次号：BATCH-日期-类型标记-序号。
    类型标记：prepay=PREPAY，cost=COST。日期取打款/开票日，缺省用当天。"""
    d = train_number.coerce_date(paid_at) if paid_at else date.today()
    tag = "PREPAY" if batch_type == "prepay" else "COST"
    return f"BATCH-{d.strftime('%Y%m%d')}-{tag}-{seq:02d}"


def _pick_unique_batch_id(base_like: str, existing: set) -> tuple[str, int]:
    """批次号去重：已占用则递增序号（最多99）。"""
    for seq in range(1, 100):
        candidate = f"{base_like}-{seq:02d}"
        if candidate not in existing:
            return candidate, seq
    raise FundStoreError(f"当日同类型批次序号已用尽：{base_like}")


# ---------------------------------------------------------------- 创建

def create_batch(*, batch_type: str, total_amount, paid_at=None,
                 fund_purpose: str = fund_recon.PURPOSE_FREIGHT,
                 counterparty: str = "", cost_category: str | None = None,
                 trip_nos: list[str] | None = None,
                 allocations: dict | None = None,
                 remark: str = "", by: str = "") -> dict:
    """登记一个资金/费用批次（主档+班列关联，单事务）。

    trip_nos：关联班列编号列表（正式/EST均可，至少1个）；
    allocations：{trip_no: 分摊金额}，可整体或部分缺失（不强求拆分）；
    fund_purpose 默认预付运费；participates 按用途自动带默认值。
    total_amount 是唯一权威金额，不校验分摊加总。
    """
    if batch_type not in ("prepay", "cost"):
        raise FundStoreError(f"批次类型不合法：{batch_type!r}")
    amount = train_recon.to_decimal(total_amount)
    if amount is None:
        raise FundStoreError("批次总金额不能为空且必须是数值")
    purpose_error = fund_recon.validate_purpose(fund_purpose, remark)
    if purpose_error:
        raise FundStoreError(purpose_error)

    trips = list(dict.fromkeys(str(n).strip() for n in (trip_nos or [])))
    if not trips:
        raise FundStoreError("批次至少关联1趟班列编号")
    for no in trips:
        _validate_trip_ref(no)

    if batch_type == "prepay" and cost_category:
        cost_category = None
    if batch_type == "cost" and not cost_category:
        raise FundStoreError("费用批次必须选择费用类目（铁路运费/报关费/服务费/其他）")
    if cost_category and cost_category not in COST_CATEGORIES:
        raise FundStoreError(f"费用类目不合法：{cost_category!r}")

    paid = train_number.coerce_date(paid_at) if paid_at else None
    allocations = allocations or {}
    participates = fund_recon.default_participates(fund_purpose)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT batch_id FROM train_fund_batches"
                        " WHERE batch_id LIKE %s",
                        (f"BATCH-{(paid or date.today()).strftime('%Y%m%d')}-"
                         f"{'PREPAY' if batch_type == 'prepay' else 'COST'}-%",))
            existing = {r[0] for r in cur.fetchall()}
            base = (f"BATCH-{(paid or date.today()).strftime('%Y%m%d')}-"
                    f"{'PREPAY' if batch_type == 'prepay' else 'COST'}")
            batch_id, _seq = _pick_unique_batch_id(base, existing)

            cur.execute(
                """INSERT INTO train_fund_batches
                       (batch_id, batch_type, fund_purpose, counterparty,
                        cost_category, paid_at, total_amount,
                        participates_in_freight_recon, remark, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (batch_id, batch_type, fund_purpose,
                 str(counterparty or "") or None, cost_category, paid, amount,
                 participates, str(remark or ""), by or None))
            for no in trips:
                alloc = train_recon.to_decimal(allocations.get(no))
                cur.execute(
                    """INSERT INTO train_fund_batch_trips
                           (batch_id, trip_no, allocated_amount)
                       VALUES (%s,%s,%s)""",
                    (batch_id, no, alloc))
    detail = get_batch(batch_id)
    db_safe_audit(by or "system", "批次登记", batch_id,
                  detail={"batch_type": batch_type, "purpose": fund_purpose,
                          "total": str(amount), "trips": len(trips)})
    return detail


def _validate_trip_ref(trip_no: str) -> None:
    """编号格式校验（正式或EST）；存在性不在写入时强卡（允许先关联后补登记，
    由孤儿检测兜底提示）。"""
    text = str(trip_no or "").strip()
    if not text:
        raise FundStoreError("班列编号不能为空")
    if text.startswith(train_number.EST_PREFIX):
        return
    if not train_number.parse_number(text):
        raise FundStoreError(f"编号格式不合法：{text!r}（正式编号或 EST- 预估编号）")


# ---------------------------------------------------------------- 查询

def _row_to_batch(row: dict, links: list[dict] | None = None) -> dict:
    batch = dict(row)
    if links is not None:
        batch["trips"] = links
    return batch


def _batch_links(cur, batch_id: str) -> list[dict]:
    cur.execute(
        """SELECT id, trip_no, allocated_amount
           FROM train_fund_batch_trips WHERE batch_id = %s
           ORDER BY id""", (batch_id,))
    # 上游 cursor 为 RealDictCursor（连接默认），按键访问；同时兼容普通tuple游标
    out = []
    for r in cur.fetchall():
        out.append({"id": r["id"] if isinstance(r, dict) else r[0],
                    "trip_no": r["trip_no"] if isinstance(r, dict) else r[1],
                    "allocated_amount": (r["allocated_amount"] if isinstance(r, dict)
                                        else r[2])})
    return out


def get_batch(batch_id: str) -> dict | None:
    """批次详情：主档 + 关联班列（含分摊）+ 覆盖核对结果。"""
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM train_fund_batches WHERE batch_id = %s",
                        (batch_id,))
            row = cur.fetchone()
            if not row:
                return None
            batch = dict(row)
            links = _batch_links(cur, batch_id)
            batch["trips"] = links

            # 取关联班列的结算/费用科目（缺失的班列以空行参与核对）
            trip_rows = _settlement_rows(cur, [l["trip_no"] for l in links])
    batch["coverage"] = fund_recon.batch_coverage_check(batch, trip_rows)
    return batch


def _settlement_rows(cur, trip_nos: list[str]) -> list[dict]:
    """按关联顺序取各班列的结算口径行；无结算记录的返回仅含 trip_no 的空行。"""
    if not trip_nos:
        return []
    cur.execute(
        """SELECT trip_no, rail_freight, customs_fee, service_fee, other_fee,
                  settle_total
           FROM train_settlements WHERE trip_no = ANY(%s)""",
        (trip_nos,))
    found = {r["trip_no"]: dict(r) for r in cur.fetchall()}
    return [found.get(no, {"trip_no": no}) for no in trip_nos]


def list_batches(batch_type: str | None = None, limit: int = 200) -> list[dict]:
    """批次列表：主档 + 覆盖核对结果（批量取关联班列口径）。"""
    where, params = "", []
    if batch_type:
        where, params = "WHERE batch_type = %s", [batch_type]
    params.append(max(1, min(int(limit), 1000)))
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM train_fund_batches {where}"
                " ORDER BY paid_at DESC NULLS LAST, batch_id LIMIT %s",
                tuple(params))
            batches = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM train_fund_batch_trips")
            all_links = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT trip_no, rail_freight, customs_fee, service_fee,"
                " other_fee, settle_total FROM train_settlements")
            settle_map = {r["trip_no"]: dict(r) for r in cur.fetchall()}

    links_by_batch: dict = {}
    for link in all_links:
        links_by_batch.setdefault(link["batch_id"], []).append(link)
    result = []
    for batch in batches:
        links = sorted(links_by_batch.get(batch["batch_id"], []),
                       key=lambda l: l["id"])
        batch["trips"] = [{"id": l["id"], "trip_no": l["trip_no"],
                           "allocated_amount": l["allocated_amount"]}
                          for l in links]
        trip_rows = [settle_map.get(l["trip_no"], {"trip_no": l["trip_no"]})
                     for l in links]
        batch["coverage"] = fund_recon.batch_coverage_check(batch, trip_rows)
        result.append(batch)
    return result


# ---------------------------------------------------------------- 更新/删除

def update_allocation(batch_id: str, trip_no: str, amount,
                      by: str = "") -> dict:
    """更新某趟车在批次中的分摊金额（空=清除分摊，不影响覆盖关系）。"""
    value = train_recon.to_decimal(amount)
    rows = db.query(
        """UPDATE train_fund_batch_trips SET allocated_amount = %s
           WHERE batch_id = %s AND trip_no = %s
           RETURNING *""", (value, batch_id, trip_no))
    if not rows:
        raise FundStoreError(
            f"关联不存在：批次 {batch_id} / 班列 {trip_no}")
    db_safe_audit(by, "批次分摊调整", f"{batch_id}/{trip_no}",
                  after={"allocated_amount": str(value)})
    return rows[0]


def set_participates(batch_id: str, participates: bool,
                     reason: str, by: str) -> dict:
    """人工改写 participates_in_freight_recon（修订版：默认值由用途带出，
    允许特殊情况下改写，必须填写原因 + EDIT_FIELD 留痕）。"""
    if not str(reason or "").strip():
        raise FundStoreError("人工改写核对参与标志必须填写原因（需留痕）")
    rows = db.query(
        """UPDATE train_fund_batches
           SET participates_in_freight_recon = %s,
               recon_override_reason = %s, updated_by = %s,
               updated_at = now()
           WHERE batch_id = %s RETURNING *""",
        (bool(participates), str(reason).strip(), by or None, batch_id))
    if not rows:
        raise FundStoreError(f"批次不存在：{batch_id}")
    db_safe_audit(by, "批次核对标志改写", batch_id,
                  after={"participates": bool(participates), "reason": reason})
    return rows[0]


def delete_batch(batch_id: str, by: str = "") -> None:
    before = get_batch(batch_id)
    if not before:
        raise FundStoreError(f"批次不存在：{batch_id}")
    db.query("DELETE FROM train_fund_batches WHERE batch_id = %s",
              (batch_id,))
    db_safe_audit(by, "批次删除", batch_id,
                  before={"total": str(before.get("total_amount"))})


# ---------------------------------------------------------------- 单趟车视角

def trip_batch_links(trip_no: str) -> list[dict]:
    """某趟车关联的全部批次（台账详情标注'款项来自批次'用）。"""
    rows = db.query(
        """SELECT b.batch_id, b.batch_type, b.fund_purpose,
                  b.participates_in_freight_recon,
                  t.allocated_amount,
                  (SELECT count(*) FROM train_fund_batch_trips x
                    WHERE x.batch_id = b.batch_id) AS trip_count
           FROM train_fund_batch_trips t
           JOIN train_fund_batches b ON b.batch_id = t.batch_id
          WHERE t.trip_no = %s
          ORDER BY b.paid_at, b.batch_id""", (trip_no,))
    links = []
    for r in rows:
        links.append({
            "batch_id": r["batch_id"],
            "batch_type": r["batch_type"],
            "fund_purpose": r["fund_purpose"],
            "trip_count": r["trip_count"],
            "allocated_amount": r["allocated_amount"],
            "participates": r["participates_in_freight_recon"],
        })
    # 附批次核对结论（批量取各批次覆盖结果）
    if links:
        coverages = {b["batch_id"]: b["coverage"]["verdict"]
                     for b in list_batches()}
        for link in links:
            link["batch_verdict"] = coverages.get(link["batch_id"])
    return links


# ---------------------------------------------------------------- 孤儿检测

def run_orphan_check() -> dict:
    """汇总检测数据并交 fund_recon.orphan_check 判定（非阻断安全网）。"""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT batch_id, trip_no FROM train_fund_batch_trips")
            links_raw = [{"batch_id": r[0], "trip_no": r[1]}
                         for r in cur.fetchall()]
            cur.execute("SELECT trip_no FROM train_trips")
            known = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT est_no FROM train_est_numbers")
            known |= {r[0] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT trip_no FROM train_prepayments")
            legacy = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT batch_id, batch_type,"
                        " participates_in_freight_recon"
                        " FROM train_fund_batches")
            batch_meta = {r[0]: {"batch_type": r[1], "participates": r[2]}
                          for r in cur.fetchall()}
    for link in links_raw:
        meta = batch_meta.get(link["batch_id"], {})
        link["batch_type"] = meta.get("batch_type", "prepay")
        link["participates"] = meta.get("participates", True)
    return fund_recon.orphan_check(links_raw, known, legacy)


# ---------------------------------------------------------------- 审计兜底

def db_safe_audit(username: str, action_label: str, object_id, **kw) -> None:
    """批次操作审计（统一记 EDIT_FIELD；失败不阻断）。"""
    try:
        import audit
        audit.record(username, audit.EDIT_FIELD, "fund_batch", object_id, **kw)
    except Exception as exc:
        print(f"[fund_store] 审计写入失败（{action_label}）: {exc}")
