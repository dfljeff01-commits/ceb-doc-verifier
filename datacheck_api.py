# -*- coding: utf-8 -*-
"""数据核对模块v1 路由（班列统一编号 + 联运结算/补贴对账，任务书v1）。

独立于 api.py 的路由模块（APIRouter 前缀 /datacheck），由 api.py 挂载：
    import datacheck_api; datacheck_api.router 挂到主 app。

权限口径（与 webapp.ROLE_PAGES 同口径）：
  - 读写班列/结算/补贴/导入/查询：finance + admin（业务角色 403）；
  - 管理面（缩写代码字典、核对阈值）：仅 admin。
留痕口径：
  - 编辑类操作逐字段 before/after（复用 audit.EDIT_FIELD，与单据核对一致）；
  - 查询/复制按任务书"可选"定位尽力留痕（失败不阻断）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

import audit
import datacheck_import
import dual_recon
import fund_store
import train_number
import train_recon
import train_store
from api_deps import require_roles

router = APIRouter(prefix="/datacheck")

_DATACHECK_ROLES = ("finance", "admin")     # 与 ROLE_PAGES：财务+管理员
_DATACHECK_ADMIN_ROLES = ("admin",)         # 字典/阈值等管理面


class TripCreateIn(BaseModel):
    """班列登记契约：日期+线路+类型生成编号；基础信息可选。"""
    dep_date: str
    station_code: str
    port_code: str
    dest_code: str
    train_type: str = "T"
    goods_name: str = ""
    wagon_count: int | None = None
    container_40hd: int | None = None
    container_20hd: int | None = None
    teu_total: float | None = None
    route_label: str = ""
    remark: str = ""


class FieldsIn(BaseModel):
    """通用补丁契约：只更新提供的键；未提供的键保留原值（patch 语义）。"""
    fields: dict[str, Any]

    @field_validator("fields")
    @classmethod
    def _fields_must_be_dict(cls, v):
        if not isinstance(v, dict):
            raise ValueError("fields 必须是对象")
        return v


class PrepayIn(BaseModel):
    paid_at: str = ""
    amount: Any = None
    remark: str = ""


class SuspectIn(BaseModel):
    review_status: str
    note: str = ""


class EstCreateIn(BaseModel):
    dep_date: str
    station_code: str
    port_code: str
    dest_code: str
    train_type: str = "T"


class EstLockIn(BaseModel):
    dep_date: str


class CodeIn(BaseModel):
    category: str
    code: str
    name: str
    sort: int = 0
    active: bool = True


class ImportDecisionIn(BaseModel):
    row_index: int = 0
    decision: str = "keep"       # create / overwrite / keep
    values: dict[str, Any] = Field(default_factory=dict)


class ImportApplyIn(BaseModel):
    kind: str
    decisions: list[ImportDecisionIn] = Field(min_length=1)


# ---------------------------------------------------------------- v1.1 资金/费用批次

class BatchTripIn(BaseModel):
    trip_no: str
    allocated_amount: Any | None = None


class FundBatchCreateIn(BaseModel):
    """资金/费用批次登记契约（多对多）。"""
    batch_type: str
    total_amount: Any
    paid_at: str = ""
    fund_purpose: str = "预付运费"
    counterparty: str = ""
    cost_category: str | None = None
    trips: list[BatchTripIn] = Field(min_length=1)
    remark: str = ""


class AllocationIn(BaseModel):
    amount: Any | None = None


class ParticipatesIn(BaseModel):
    participates: bool
    reason: str


def _dc_user(user: dict = Depends(require_roles(*_DATACHECK_ROLES))) -> dict:
    return user


def _dc_admin(user: dict = Depends(require_roles(*_DATACHECK_ADMIN_ROLES))) -> dict:
    return user


class DualApplyDecisionIn(BaseModel):
    match_key: str = ""
    decision: str = "skip"          # use_agent / use_own / skip
    own: dict | None = None
    agent: dict | None = None


class DualApplyIn(BaseModel):
    decisions: list[DualApplyDecisionIn] = Field(min_length=1)


def _dc_error(exc: Exception):
    """存储/编号/导入域错误 → 422（其余异常向上抛 500）。"""
    if isinstance(exc, (train_number.TrainNumberError, train_store.TrainStoreError,
                        datacheck_import.ImportError_)):
        raise HTTPException(status_code=422, detail=str(exc))
    raise exc


def _audit_field_changes(username: str, object_type: str, object_id: str,
                         before: dict, after: dict) -> None:
    """逐字段 before/after 留痕（口径与单据核对 _audit_field_edit 一致）。"""
    for field in set(before) | set(after):
        old_v, new_v = before.get(field), after.get(field)
        if old_v != new_v:
            audit.record(username, audit.EDIT_FIELD, object_type,
                         f"{object_id}/{field}",
                         before={"value": str(old_v) if old_v is not None else None},
                         after={"value": str(new_v) if new_v is not None else None})


# ---------------------------------------------------------------- 字典与配置

@router.get("/codes")
def dc_list_codes(user: dict = Depends(_dc_user)):
    """缩写代码字典（发站/口岸/目的地，编号生成与查询页下拉共用）。"""
    return {"codes": train_store.list_codes()}


@router.put("/codes")
def dc_upsert_code(body: CodeIn, mode: str = "upsert",
                   user: dict = Depends(_dc_admin)):
    """登记/修改缩写（任务书 §一：支持后续增删站点/口岸，不硬编码）。

    mode=create（代码字典维护页"新增"用）：同类别同缩写已存在（不论启用
    还是停用）→ 422 明确提示，不静默覆盖；
    默认 upsert：修改名称/排序/停用启用。
    审计动作按语义区分：新增=DC_CODE_CREATE、停用=DC_CODE_DISABLE、
    其余修改=DC_CODE_UPDATE（含 before/after）。"""
    code = str(body.code or "").strip().upper()
    existing = next((c for c in train_store.list_codes()
                     if c["category"] == body.category and c["code"] == code),
                    None)
    if mode == "create" and existing is not None:
        state = "启用" if existing.get("active") else "已停用"
        raise HTTPException(
            status_code=422,
            detail=f"{body.category}类别下缩写 {code} 已登记"
                   f"（{state}，名称：{existing.get('name')}），不能重复登记；"
                   f"如需修改请用编辑功能。")
    try:
        row = train_store.upsert_code(body.category, code, body.name,
                                      body.sort, body.active, user["username"])
    except Exception as exc:
        _dc_error(exc)
    if existing is None:
        action = audit.DC_CODE_CREATE
    elif existing.get("active") and not body.active:
        action = audit.DC_CODE_DISABLE
    else:
        action = audit.DC_CODE_UPDATE
    audit.record(user["username"], action, "train_code_dict",
                 f"{body.category}/{code}",
                 before=existing if existing is not None else None,
                 after=row)
    return row


@router.get("/config")
def dc_get_config(user: dict = Depends(_dc_user)):
    return {"config": train_store.get_config(),
            "thresholds": {k: str(v) for k, v in train_store.thresholds().items()}}


@router.put("/config")
def dc_set_config(body: FieldsIn, user: dict = Depends(_dc_admin)):
    """修改核对阈值（任务书 §三：暂定超5%或超5000元，可配置）。"""
    before = train_store.get_config()
    changed = {}
    try:
        for key, value in body.fields.items():
            train_store.set_config(key, value, user["username"])
            changed[key] = value
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_CONFIG_UPDATE, "app_config",
                 ",".join(changed),
                 before={k: (before.get(k) or {}).get("value") for k in changed},
                 after=changed)
    return {"config": train_store.get_config()}


# ---------------------------------------------------------------- 查询与台账

@router.get("/lookup")
def dc_lookup(dep_date: str = "", station: str = "", port: str = "",
              dest: str = "", user: dict = Depends(_dc_user)):
    """财务编号查询（任务书 §四）：按日期/线路返回正式编号；
    尚无正式编号时提示仅有预估编号 XXX。查询动作尽力留痕（失败不阻断）。"""
    if not any((dep_date, station, port, dest)):
        raise HTTPException(status_code=422,
                            detail="至少提供一个查询条件（发运日期/发站/口岸/目的地）")
    try:
        result = train_store.lookup(
            dep_date=dep_date or None, station=station or None,
            port=port or None, dest=dest or None)
    except Exception as exc:
        _dc_error(exc)
    try:
        audit.record(user["username"], audit.DC_QUERY_NUMBER, "trip_lookup",
                     dep_date or f"{station}-{port}-{dest}".strip("-"),
                     detail={"trips": len(result["trips"]),
                             "ests": len(result["ests"])})
    except Exception:
        pass
    return result


@router.get("/trips")
def dc_list_trips(date_from: str = "", date_to: str = "", station: str = "",
                  port: str = "", dest: str = "", limit: int = 200,
                  user: dict = Depends(_dc_user)):
    try:
        return {"trips": train_store.list_trips(
            date_from or None, date_to or None, station or None,
            port or None, dest or None, limit)}
    except Exception as exc:
        _dc_error(exc)


@router.post("/trips")
def dc_create_trip(body: TripCreateIn, user: dict = Depends(_dc_user)):
    """登记班列并生成正式编号（同日同线路同类型冲突自动 -01/-02 兜底）。"""
    basic = {"goods_name": body.goods_name, "wagon_count": body.wagon_count,
             "container_40hd": body.container_40hd,
             "container_20hd": body.container_20hd, "teu_total": body.teu_total,
             "route_label": body.route_label, "remark": body.remark}
    try:
        trip = train_store.create_trip(body.dep_date, body.station_code,
                                       body.port_code, body.dest_code,
                                       body.train_type, user["username"], basic)
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_CREATE_TRIP, "trip", trip["trip_no"],
                 after={"dep_date": str(body.dep_date),
                        "train_type": body.train_type.upper()})
    return trip


@router.get("/trips/{trip_no}")
def dc_trip_detail(trip_no: str, user: dict = Depends(_dc_user)):
    """班列完整详情：基础+结算+预付+补贴+核对结果+重复值提示+编号沿革。"""
    detail = train_store.trip_detail(trip_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"班列编号不存在：{trip_no}")
    return detail


@router.put("/trips/{trip_no}/basic")
def dc_update_basic(trip_no: str, body: FieldsIn, user: dict = Depends(_dc_user)):
    try:
        before, after = train_store.update_trip_basic(trip_no, body.fields,
                                                      user["username"])
    except Exception as exc:
        _dc_error(exc)
    _audit_field_changes(user["username"], "trip", trip_no, before, after)
    return after


@router.put("/trips/{trip_no}/settlement")
def dc_update_settlement(trip_no: str, body: FieldsIn,
                         user: dict = Depends(_dc_user)):
    """结算明细编辑；结算合计≠实付运费且差异原因为空时，返回的核对结果带
    "待人工填写差异原因"提示（不阻断保存，任务书 §二）。"""
    try:
        before, after = train_store.upsert_settlement(trip_no, body.fields,
                                                      user["username"])
    except Exception as exc:
        _dc_error(exc)
    _audit_field_changes(user["username"], "trip_settlement", trip_no,
                         before, after)
    return {"settlement": after,
            "check": train_recon.settle_actual_check(
                after.get("settle_total"), after.get("actual_freight"),
                after.get("diff_reason"))}


@router.put("/trips/{trip_no}/subsidy")
def dc_update_subsidy(trip_no: str, body: FieldsIn, user: dict = Depends(_dc_user)):
    try:
        before, after = train_store.upsert_subsidy(trip_no, body.fields,
                                                   user["username"])
    except Exception as exc:
        _dc_error(exc)
    _audit_field_changes(user["username"], "trip_subsidy", trip_no, before, after)
    train_store.refresh_anomalies(user["username"], [trip_no])
    return after


@router.post("/trips/{trip_no}/prepays")
def dc_add_prepay(trip_no: str, body: PrepayIn, user: dict = Depends(_dc_user)):
    """登记预付款（发运日期未定时 trip_no 传预估编号，锁定后自动改挂）。"""
    try:
        row = train_store.add_prepayment(trip_no, body.paid_at, body.amount,
                                         body.remark, user["username"])
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.EDIT_FIELD, "prepay",
                 f"{trip_no}/新增预付款", after=row)
    return row


@router.put("/prepays/{pid}")
def dc_update_prepay(pid: int, body: PrepayIn, user: dict = Depends(_dc_user)):
    try:
        before, after = train_store.update_prepayment(
            pid, {"paid_at": body.paid_at, "amount": body.amount,
                  "remark": body.remark}, user["username"])
    except Exception as exc:
        _dc_error(exc)
    _audit_field_changes(user["username"], "prepay", str(pid), before, after)
    return after


@router.delete("/prepays/{pid}")
def dc_delete_prepay(pid: int, user: dict = Depends(_dc_user)):
    try:
        removed = train_store.delete_prepayment(pid)
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.EDIT_FIELD, "prepay", str(pid),
                 before=removed, after=None)
    return {"deleted": removed}


# ---------------------------------------------------------------- 补贴存疑标记

@router.post("/trips/{trip_no}/suspect")
def dc_mark_suspect(trip_no: str, body: SuspectIn, user: dict = Depends(_dc_user)):
    """补贴记录 存疑/复核 标记（任务书 §三：不直接采信原始数值）。"""
    try:
        before, after = train_store.mark_suspect(trip_no, body.review_status,
                                                 body.note, user["username"])
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_SUSPECT_MARK, "trip_subsidy",
                 trip_no,
                 before={"review_status": (before.get("review_status")
                                           if before else None),
                         "suspect_note": (before.get("suspect_note")
                                          if before else None)},
                 after={"review_status": after["review_status"],
                        "suspect_note": after["suspect_note"]})
    return after


# ---------------------------------------------------------------- 预估编号

@router.post("/est")
def dc_create_est(body: EstCreateIn, user: dict = Depends(_dc_user)):
    """创建预估编号（发运日期未确定时登记预付款等信息用，任务书 §一）。"""
    try:
        est = train_store.create_est(body.dep_date, body.station_code,
                                     body.port_code, body.dest_code,
                                     body.train_type, user["username"])
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_CREATE_TRIP, "trip_est",
                 est["est_no"], after={"dep_date_est": str(body.dep_date)})
    return est


@router.get("/ests")
def dc_list_ests(locked: str = "", limit: int = 200,
                 user: dict = Depends(_dc_user)):
    locked_flag = {"true": True, "false": False}.get(locked.lower())
    return {"ests": train_store.list_est(locked_flag, limit)}


@router.post("/est/{est_no}/lock")
def dc_lock_est(est_no: str, body: EstLockIn, user: dict = Depends(_dc_user)):
    """锁定预估编号：生成/匹配正式编号并保留映射（预估/正式/锁定时间/操作人）；
    该预估编号名下预付款单事务批量改挂正式编号（est_ref 留痕）。"""
    try:
        result = train_store.lock_est(est_no, body.dep_date, user["username"])
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_EST_LOCK, "trip_est", est_no,
                 detail={"official_no": result["official_no"],
                         "created_trip": result["created_trip"],
                         "relinked_prepays": result["relinked_prepays"]})
    return result


# ---------------------------------------------------------------- 三方对账

@router.get("/recon")
def dc_recon(date_from: str = "", date_to: str = "",
             user: dict = Depends(_dc_user)):
    """三方对账页数据：台账联查 + 逐班列三方核对（阈值可配置）+
    实时相邻重复值检测（只提示不下结论）。"""
    rows = train_store.trips_with_subsidies(date_from or None, date_to or None)
    thr = train_store.thresholds()
    enriched = []
    for row in rows:
        item = dict(row)
        item["three_way"] = train_recon.three_way_check(row, thr["pct"],
                                                        thr["amount"])
        enriched.append(item)
    dup = train_recon.duplicate_neighbor_flags(rows)
    return {"trips": enriched, "duplicate_flags": dup,
            "thresholds": {"pct": str(thr["pct"]), "amount": str(thr["amount"])},
            "review_labels": train_recon.REVIEW_STATUS_LABELS,
            "status_labels": train_recon.RECORD_STATUS_LABELS}


# ---------------------------------------------------------------- Excel 导入

@router.post("/import/preview")
async def dc_import_preview(kind: str, file: UploadFile = File(...),
                            user: dict = Depends(_dc_user)):
    """Excel 导入预览：解析+与既有记录比对，标注 新增/一致/冲突/错误；
    只读不写库（任务书 §五：冲突交人工确认，不静默覆盖）。"""
    data = await file.read()
    try:
        result = datacheck_import.detect(kind, data)
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_IMPORT_PREVIEW, "import",
                 kind, detail=result["stats"])
    return result


@router.post("/import/apply")
def dc_import_apply(body: ImportApplyIn, user: dict = Depends(_dc_user)):
    """应用导入：逐行携带人工决定（create/overwrite/keep）；应用端二次校验。"""
    try:
        result = datacheck_import.apply(
            body.kind, [d.model_dump() for d in body.decisions],
            user["username"])
    except Exception as exc:
        _dc_error(exc)
    audit.record(user["username"], audit.DC_IMPORT_APPLY, "import",
                 body.kind, detail=result["counts"],
                 after={"results": result["results"]})
    return result


@router.get("/import/template")
def dc_import_template(kind: str, user: dict = Depends(_dc_user)):
    """下载导入模板（字段命名与任务书数据结构一致，内含脱敏示例行）。"""
    try:
        data = datacheck_import.build_template(kind)
    except Exception as exc:
        _dc_error(exc)
    from urllib.parse import quote
    filename = "班列结算导入模板.xlsx" if kind == "trip" else "补贴测算导入模板.xlsx"
    # RFC 5987：非 ASCII 文件名必须百分号编码（HTTP 头仅允许 latin-1）
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"})


# ============================================================================
# v1.1 资金/费用批次路由（多对多关联；权限同 datacheck：finance+admin）
# ============================================================================

def _fund_error(exc: Exception):
    if isinstance(fund_store.FundStoreError):
        raise HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/fund-batches")
def dc_list_fund_batches(batch_type: str = "", user: dict = Depends(_dc_user)):
    """批次列表（含每批次覆盖核对结果）。"""
    return {"batches": fund_store.list_batches(batch_type or None)}


@router.get("/fund-batches/{batch_id}")
def dc_get_fund_batch(batch_id: str, user: dict = Depends(_dc_user)):
    detail = fund_store.get_batch(batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"批次不存在：{batch_id}")
    return detail


@router.post("/fund-batches")
def dc_create_fund_batch(body: FundBatchCreateIn, user: dict = Depends(_dc_user)):
    """登记资金/费用批次：主档+多趟班列关联（分摊可选）。"""
    trip_nos = [t.trip_no for t in body.trips]
    allocations = {t.trip_no: t.allocated_amount for t in body.trips
                   if t.allocated_amount is not None}
    try:
        return fund_store.create_batch(
            batch_type=body.batch_type, total_amount=body.total_amount,
            paid_at=body.paid_at or None, fund_purpose=body.fund_purpose,
            counterparty=body.counterparty, cost_category=body.cost_category,
            trip_nos=trip_nos, allocations=allocations,
            remark=body.remark, by=user["username"])
    except Exception as exc:
        _fund_error(exc)


@router.put("/fund-batches/{batch_id}/trips/{trip_no}/allocation")
def dc_update_allocation(batch_id: str, trip_no: str, body: AllocationIn,
                         user: dict = Depends(_dc_user)):
    try:
        return fund_store.update_allocation(
            batch_id, trip_no, body.amount, user["username"])
    except Exception as exc:
        _fund_error(exc)


@router.put("/fund-batches/{batch_id}/participates")
def dc_set_participates(batch_id: str, body: ParticipatesIn,
                        user: dict = Depends(_dc_user)):
    """人工改写'参与运费核对'标志（需原因，EDIT_FIELD 留痕）。"""
    try:
        return fund_store.set_participates(
            batch_id, body.participates, body.reason, user["username"])
    except Exception as exc:
        _fund_error(exc)


@router.delete("/fund-batches/{batch_id}")
def dc_delete_fund_batch(batch_id: str, user: dict = Depends(_dc_user)):
    try:
        fund_store.delete_batch(batch_id, user["username"])
    except Exception as exc:
        _fund_error(exc)
    return {"deleted": batch_id}


@router.get("/fund-batches/orphan-check/run")
def dc_orphan_check(user: dict = Depends(_dc_user)):
    """孤儿/重复登记检测（安全网，非阻断）。"""
    return fund_store.run_orphan_check()


# ============================================================================
# Issue #2 双表对账导入（己方台账 × 联运对账单）
# ============================================================================

@router.post("/dual-recon/preview")
async def dc_dual_preview(own_file: UploadFile = File(...),
                          agent_file: UploadFile = File(...),
                          user: dict = Depends(_dc_user)):
    """上传两份 Excel → 解析、站编匹配、四状态预览（只读不写库）。"""
    own_data = await own_file.read()
    agent_data = await agent_file.read()
    try:
        result = dual_recon.build_preview(own_data, agent_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record(user["username"], audit.DC_IMPORT_PREVIEW, "dual_recon",
                 "preview", detail=result["stats"])
    return result


@router.post("/dual-recon/apply")
def dc_dual_apply(body: DualApplyIn, user: dict = Depends(_dc_user)):
    """按人工决定把双表配对结果写入 train_trips/settlements/prepayments。"""
    decisions = [d.model_dump() for d in body.decisions]
    result = dual_recon.apply_preview(decisions, user["username"])
    audit.record(user["username"], audit.DC_IMPORT_APPLY, "dual_recon",
                 "apply", detail=result["counts"])
    return result
