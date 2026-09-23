# -*- coding: utf-8 -*-
"""
核验服务 API（架构升级：PostgreSQL存储 + 登录鉴权 + 操作日志）。

端点：
  GET  /health         健康检查（免鉴权，供容器探活）
  POST /auth/login     用户名+密码登录 → JWT令牌（操作日志：登录/登录失败）
  GET  /auth/me        当前登录用户信息
  POST /auth/logout    登出（操作日志：登出；无状态令牌由客户端丢弃）
  POST /verify         批次JSON核验（返回明细+风险评分+建议+按单据分组分层结果）
  POST /ingest/image   图片摄取（App拍照/相册）：OCR → 类型识别 → 字段解析
  POST /ingest/pdf     PDF摄取：按页判型 → 文本直取/OCR → 字段解析（整份一份单据）
  POST /ingest/pdf/split  多单据PDF拆分摄取：识别单据边界，逐份返回（任务书问题一）

App专用端点（产品定位"电脑端是大脑、手机端是触手"）：
  POST /mobile/quick-check  现场拍照即传：OCR → 核验 → PostgreSQL持久化完整报告，
                            只返回轻量摘要（风险等级红黄绿 + 一句话关键问题 + 批次编号），
                            不返回核验明细/分数构成/AI建议（这些留给电脑端）。
  GET  /mobile/batch/{id}   按批次编号取历史结果（默认轻量视图；include_full=true
                            附完整核验报告与原始单据，供电脑端网页复查展示）。
  GET  /mobile/lookup        现场速查：按运单号/单证编号/箱号/批次编号检索
                            历史核验结论（轻量视图，按时间倒序）。

鉴权口径（内部系统，用户名+密码）：
  - 除 /health 外全部要求 Bearer 令牌（网页端登录后携带、App登录后本地保存）；
  - 业务/管理员可用单据核验相关端点；财务角色本轮仅开放"数据核对"（占位），
    对单据核验数据一律 403（边界待业务侧确认后再放开）；
  - 关键操作写操作日志（audit_logs，只增不改）：登录/登出/登录失败、上传、
    核验、查看完整报告。

请求契约（修复 F08）：
  /verify 使用 Pydantic 模型严格校验请求体——结构错误（缺字段、类型错、null、
  fields 非对象）由框架层直接返回 422 与可读的"哪个字段什么问题"，不再 500；
  上传端点有文件大小、PDF页数、处理耗时三重限制，OCR 等阻塞操作在
  有限工作线程池中执行（超时返回 504），不会拖垮事件循环。

运行：uvicorn api:app --host 0.0.0.0 --port 8000
文档：http://localhost:8000/docs （FastAPI 自动生成的 Swagger UI）
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

import audit
import auth_service
import db
import doc_contract
import mobile_store
import pdf_ingest
from verification_engine import run_verification

# 鉴权依赖抽到 api_deps（数据核对路由模块共用，避免循环导入）；此处保持原名可用
from api_deps import get_client_ip, require_roles, require_user

# 数据核对模块v1 路由（/datacheck/*：班列编号+联运结算/补贴对账，财务/管理员）
from datacheck_api import router as datacheck_router

API_VERSION = "2.1.0"

app = FastAPI(
    title="中欧班列单证智能核验 API",
    description="上传单证批次JSON或PDF/图片文件，返回核验明细、风险评分与AI修正建议。",
    version=API_VERSION,
)

app.include_router(datacheck_router)


@app.on_event("startup")
def _startup_init_db():
    """服务进程启动时确保 schema 就绪（建表幂等；数据库不可达则快速失败，
    由编排层重启——避免"网页假活、数据无处可写"的静默降级）。"""
    db.init_schema()


# ---------------------------------------------------------------- 登录鉴权

# 可以使用单据核验功能的角色（财务角色对单据核验数据的可见性待业务侧确认，
# 本轮默认不开放——只保留"数据核对"入口，见任务书 §2/§4）
_DOC_VERIFY_ROLES = ("business", "admin")

# 数据核对端点的角色口径在 datacheck_api（finance+admin；字典/阈值仅admin），
# 与 webapp.ROLE_PAGES 同矩阵。

# 鉴权依赖（require_user/require_roles/get_client_ip）已抽至 api_deps.py，
# 由 api.py 与 datacheck_api.py 共用；上方 from api_deps import 保持原名可用。


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/auth/login")
def auth_login(body: LoginIn, request: Request):
    """用户名+密码登录（内部系统口径）。成功返回JWT与用户信息；
    失败返回401，并记录 LOGIN_FAILED 审计（含尝试用户名）。"""
    ip = get_client_ip(request)
    try:
        user = auth_service.authenticate(body.username, body.password)
    except auth_service.AuthError as exc:
        audit.record(body.username or "anonymous", audit.LOGIN_FAILED,
                     "user", body.username or "", detail={"reason": exc.message},
                     ip=ip)
        raise HTTPException(status_code=401, detail=exc.message)
    token = auth_service.create_token(user)
    audit.record(user["username"], audit.LOGIN, "user", user["username"], ip=ip)
    return {**token, "user": user}


@app.get("/auth/me")
def auth_me(user: dict = Depends(require_user)):
    return user


@app.post("/auth/logout")
def auth_logout(user: dict = Depends(require_user), request: Request = None):
    """无状态JWT的登出=客户端丢弃令牌；服务端留痕审计。"""
    audit.record(user["username"], audit.LOGOUT, "user", user["username"],
                 ip=get_client_ip(request) if request else "")
    return {"ok": True, "detail": "令牌已由客户端丢弃，请清除本地凭证"}

# ---------------------------------------------------------------- 上传资源限制（F08）
MAX_PDF_BYTES = 20 * 1024 * 1024        # 20MB
MAX_IMAGE_BYTES = 10 * 1024 * 1024      # 10MB
MAX_PDF_PAGES = pdf_ingest.MAX_PDF_PAGES
INGEST_TIMEOUT_SECONDS = 120            # 单文件处理耗时上限

# 受限工作线程池：阻塞型OCR不占用事件循环，且并发有上限
_INGEST_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")


async def _run_ingest(fn, data: bytes, filename: str):
    """把阻塞的摄取/OCR处理放到受限线程池执行，超时返回504（F08）。"""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_INGEST_EXECUTOR, fn, data, filename)
    try:
        return await asyncio.wait_for(future, timeout=INGEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"文件处理超时（>{INGEST_TIMEOUT_SECONDS}s，可能页数过多或扫描件过大），"
                   f"请拆分后重试；服务未受影响。")


# ---------------------------------------------------------------- 请求契约（F08）


class DocumentIn(BaseModel):
    """单证对象契约：doc_type 必须是受支持口径内的字符串，fields 必须是对象。
    field_meta（可选，P0任务书A1）为字段四状态证据，随单据进入存储与展示。"""
    doc_type: str = "unknown"
    doc_id: str = ""
    title: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    field_meta: dict[str, Any] | None = None

    @field_validator("doc_type")
    @classmethod
    def _doc_type_must_be_string_with_content(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("doc_type 不能为空白（unknown 表示未识别类型）")
        return str(v)

    @field_validator("fields")
    @classmethod
    def _fields_must_be_dict(cls, v):
        if not isinstance(v, dict):
            raise ValueError("fields 必须是对象（{\"字段名\": 值}），不能是字符串/列表")
        return v


class BatchIn(BaseModel):
    """批次请求契约（与 sample_data/*.json 同构）。
    declared_composition（可选，任务书问题一/二）：用户申报的单据构成
    {"railway_waybill": 3, ...}——申报后同类型多份不再判"重复单证"，
    实收与申报不符时产出 DOC-004 告警；不传则维持原有单份口径。"""
    batch_id: str = ""
    batch_name: str = ""
    description: str = ""
    destination_summary: str = ""
    declared_composition: dict[str, int] | None = None
    documents: list[DocumentIn] = Field(min_length=1, description="至少1份单证")

    @field_validator("documents")
    @classmethod
    def _documents_non_empty(cls, v):
        if not v:
            raise ValueError("documents 不能为空")
        return v

    @field_validator("declared_composition")
    @classmethod
    def _composition_counts_positive(cls, v):
        if v is None:
            return v
        for doc_type, count in v.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(
                    f"declared_composition[{doc_type}] 必须是非负整数（收到 {count!r}）")
        return v


@app.get("/health")
def health():
    return {"status": "ok", "service": "ceb-doc-verifier", "version": API_VERSION,
            "rule_version": doc_contract.ROUTE_RULE_VERSION,
            "ocr_available": pdf_ingest.ocr_available()}


@app.post("/verify")
def verify(batch: BatchIn,
           user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES)),):
    """
    请求体即批次JSON（与 sample_data/*.json 同构）：
    {"batch_id": "...", "documents": [{"doc_type", "doc_id", "title", "fields": {...}}, ...],
     "declared_composition": {"railway_waybill": 3, ...}}   # 可选申报构成

    结构错误（documents 缺失/为空/含null、fields 类型错误等）由 Pydantic 校验
    返回 422，detail 指明具体字段；不做静默修正。
    返回：results（明细）、summary（计数）、risk（评分+构成）、suggestions（修正建议），
    以及按单据实例分组的分层结果（任务书问题四）：
      document_groups: [{doc_id, label(如"运单#2"), doc_type, fail_count,
                         warning_count, issues:[{check_id, check_name, status,
                         detail, field, suggestion}]}],
      batch_level_issues: [无法归属单份单据的批次级问题]
    """
    verification = run_verification(batch.model_dump())
    audit.record(user["username"], audit.VERIFY, "batch",
                 batch.batch_id or "(未编号)",
                 detail={"summary": verification.get("summary"),
                         "risk_score": verification.get("risk", {}).get("score")})
    return verification


def _ingest_result_payload(r) -> dict:
    return {
        "filename": r.filename,
        "doc_type": r.doc_type,
        "type_score": r.type_score,
        "fields": r.fields,
        "field_confidence": r.field_confidence,
        "needs_review": r.needs_review,
        "warnings": r.warnings,
        "elapsed_seconds": round(r.elapsed_seconds, 3),
        "error": r.error,
        "document": None if r.error else r.to_document(),
    }


@app.post("/ingest/image")
async def ingest_image(file: UploadFile = File(...),
                       user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    图片摄取（App拍照/相册）：上传单张图片（jpg/png），后端OCR识别并解析字段。

    返回 doc_type（自动判断）、fields、field_confidence（missing=提取失败/需人工核对）
    以及组装好的 document（可直接并入 /verify 的 documents）。
    本机未安装OCR时返回 503 与安装提示；超过大小限制返回 413。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"图片超过大小限制（{len(data) // 1024 // 1024}MB > "
                                   f"{MAX_IMAGE_BYTES // 1024 // 1024}MB），请压缩后上传")
    r = await _run_ingest(pdf_ingest.process_image, data, file.filename or "upload.jpg")
    if r.error and not pdf_ingest.ocr_available():
        raise HTTPException(status_code=503, detail=r.error)
    return _ingest_result_payload(r)


@app.post("/ingest/pdf")
async def ingest_pdf(file: UploadFile = File(...),
                     user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    PDF摄取：按页判型（文本直取/扫描OCR）→ 类型识别 → 字段解析。
    整份PDF按一份单据处理；多单据混合PDF请用 /ingest/pdf/split。
    大小限制 413；页数超限 413；处理超时 504（OCR在受限线程池执行）。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"PDF超过大小限制（{len(data) // 1024 // 1024}MB > "
                                   f"{MAX_PDF_BYTES // 1024 // 1024}MB），请拆分后上传")
    r = await _run_ingest(pdf_ingest.process_pdf, data, file.filename or "upload.pdf")
    if r.error and "页数超限" in (r.error or ""):
        raise HTTPException(status_code=413, detail=r.error)
    return _ingest_result_payload(r)


@app.post("/ingest/pdf/split")
async def ingest_pdf_split(file: UploadFile = File(...),
                           user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    多单据PDF摄取（任务书问题一）：按页信号识别单据边界，把一份包含多份
    独立单据（如多份运单）的PDF拆成逐份独立的识别结果。

    返回 groups: [{filename, doc_type, identity_value, page_nos, fields,
                   field_confidence, needs_review, warnings, document}]，
    每组可直接并入 /verify 的 documents（document.doc_id 已按页码区分）。
    needs_confirmation=True 表示边界不确定（如部分页未提取到单据编号），
    调用方应提示用户人工确认拆分边界，不应直接采用。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"PDF超过大小限制（{len(data) // 1024 // 1024}MB），请拆分后上传")
    r = await _run_ingest(pdf_ingest.split_pdf, data, file.filename or "upload.pdf")
    if r.error and "页数超限" in (r.error or ""):
        raise HTTPException(status_code=413, detail=r.error)
    if r.error:
        raise HTTPException(status_code=422, detail=r.error)
    return {
        "filename": r.filename,
        "needs_confirmation": r.needs_confirmation,
        "reason": r.reason,
        "groups": [
            {
                "doc_type": g.doc_type,
                "identity_value": g.identity_value,
                "page_nos": [p.page_no for p in g.pages],
                "fields": g.fields,
                "field_confidence": g.field_confidence,
                "needs_review": g.needs_review,
                "warnings": g.warnings,
                "document": g.to_document(
                    doc_id_suffix=f"P{g.pages[0].page_no}" if len(r.groups) > 1 else ""),
            }
            for g in r.groups
        ],
    }


# ---------------------------------------------------------------- App专用端点（现场触手）



MAX_QUICK_CHECK_PHOTOS = 5   # 现场一次拍摄整套单证（发票/箱单/运单/报关单/产地证）上限


def _mobile_lite_payload(record: dict) -> dict:
    """App轻量摘要响应：批次编号 + 风险等级红黄绿 + 一句话关键问题。"""
    return {
        "batch_id": record["batch_id"],
        "created_at": record["created_at"],
        "created_by": record.get("created_by") or "",
        **record["lite"],
        "identity_numbers": record["identity_numbers"],
        "detail_hint": f"请在电脑端核验网页输入批次编号 {record['batch_id']} 查看完整报告",
    }


@app.post("/mobile/quick-check")
async def mobile_quick_check(
        request: Request,
        files: list[UploadFile] = File(..., description="现场拍摄的单证照片（1~5张）"),
        user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    现场拍照即传（App专用）：上传1~5张单证照片，后端OCR识别 → 组装批次 →
    完整核验（与电脑端同一引擎）→ 持久化完整报告 → 只返回轻量摘要。

    响应只含：批次编号、风险等级（green/yellow/red 与 grade/grade_label/risk_score）、
    一句话关键问题（one_line）、单证类型清单与编号索引，不含核验明细、分数构成、
    AI建议——完整报告请在电脑端网页输入批次编号查看。

    超过照片数量/大小限制返回 413/422；OCR环境缺失返回 503；单张处理超时 504。
    """
    if not files:
        raise HTTPException(status_code=422, detail="请至少上传1张照片")
    if len(files) > MAX_QUICK_CHECK_PHOTOS:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多上传{MAX_QUICK_CHECK_PHOTOS}张照片（收到{len(files)}张），"
                   f"整套单证建议分两批拍摄")
    payloads = []
    for f in files:
        data = await f.read()
        if not data:
            raise HTTPException(status_code=422, detail=f"照片为空：{f.filename}")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"照片超过大小限制（{f.filename}，"
                       f"{len(data) // 1024 // 1024}MB > {MAX_IMAGE_BYTES // 1024 // 1024}MB）")
        payloads.append((data, f.filename or "photo.jpg"))

    batch_id = mobile_store.new_batch_id()
    results = []
    for data, name in payloads:
        results.append(await _run_ingest(pdf_ingest.process_image, data, name))
    if any(r.error for r in results) and not pdf_ingest.ocr_available():
        raise HTTPException(status_code=503, detail=next(r.error for r in results if r.error))

    # 单张识别异常的以unknown占位进入核验，让引擎显式告警（用户仍能得到一句话反馈，
    # 不至于白跑一趟）；识别正常的照片 doc_id 按批次编号+序号，可回溯到具体照片。
    documents = []
    for i, r in enumerate(results, start=1):
        if r.error:
            documents.append({
                "doc_type": "unknown", "doc_id": f"{batch_id}-{i}",
                "title": f"未识别照片 {r.filename}", "fields": {},
            })
        else:
            doc = r.to_document()
            doc["doc_id"] = f"{batch_id}-{i}"
            documents.append(doc)

    verification = run_verification({
        "batch_id": batch_id,
        "batch_name": f"App现场拍摄 {batch_id}",
        "documents": documents,
    })
    record = mobile_store.save_batch(documents, verification,
                                     created_by=user["username"])
    audit.record(user["username"], audit.UPLOAD_DOCS, "batch",
                 record["batch_id"],
                 detail={"photos": len(files), "source": "mobile_app",
                         "risk_grade": record["lite"]["risk_grade"],
                         "one_line": record["lite"]["one_line"]},
                 ip=get_client_ip(request))
    return _mobile_lite_payload(record)


@app.get("/mobile/batch/{batch_id}")
def mobile_get_batch(batch_id: str, include_full: bool = False,
                     user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    按批次编号查询历史结果。
    默认返回轻量视图（App现场速查口径：风险等级+一句话结论+编号索引）；
    include_full=true 时附带完整核验报告（verification）与原始单证（documents），
    供电脑端网页"输入批次编号查看完整报告"使用。
    """
    record = mobile_store.get_batch(batch_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到批次 {batch_id} 的核验记录")
    payload = mobile_store.lite_view(record)
    if include_full:
        payload["documents"] = record.get("documents", [])
        payload["verification"] = record.get("verification", {})
        audit.record(user["username"], audit.VIEW_REPORT, "batch", batch_id)
    return payload


@app.get("/mobile/lookup")
def mobile_lookup(q: str, limit: int = 5,
                  user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """
    现场速查（App专用）：按运单号/单证编号/箱号/批次编号检索历史核验结论。
    返回轻量视图列表（时间倒序，最多limit条），查无记录时 matches 为空列表。
    """
    if not q or not q.strip():
        raise HTTPException(status_code=422, detail="查询条件不能为空（运单号/单证编号/批次编号）")
    matches = mobile_store.lookup(q.strip(), limit=min(max(limit, 1), 20))
    audit.record(user["username"], audit.QUICK_CHECK, "lookup", q.strip(),
                 detail={"count": len(matches)})
    return {"query": q.strip(), "count": len(matches), "matches": matches}


@app.get("/mobile/recent")
def mobile_recent(limit: int = 20,
                  user: dict = Depends(require_roles(*_DOC_VERIFY_ROLES))):
    """最近上传批次一览（轻量视图，供运维/演示检查持久化结果）。"""
    return {"matches": mobile_store.recent(limit=min(max(limit, 1), 50))}
