# -*- coding: utf-8 -*-
"""
核验服务 API（升级任务书·工程化加固 + App任务书·图片摄取）。

端点：
  GET  /health         健康检查
  POST /verify         批次JSON核验（返回明细+风险评分+建议）
  POST /ingest/image   图片摄取（App拍照/相册）：OCR → 类型识别 → 字段解析
  POST /ingest/pdf     PDF摄取：按页判型 → 文本直取/OCR → 字段解析

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

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

import doc_contract
import pdf_ingest
from verification_engine import run_verification

API_VERSION = "1.3.0"

app = FastAPI(
    title="中欧班列单证智能核验 API",
    description="上传单证批次JSON或PDF/图片文件，返回核验明细、风险评分与AI修正建议。",
    version=API_VERSION,
)

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
    """单证对象契约：doc_type 必须是受支持口径内的字符串，fields 必须是对象。"""
    doc_type: str = "unknown"
    doc_id: str = ""
    title: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)

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
    """批次请求契约（与 sample_data/*.json 同构）。"""
    batch_id: str = ""
    batch_name: str = ""
    description: str = ""
    destination_summary: str = ""
    documents: list[DocumentIn] = Field(min_length=1, description="至少1份单证")

    @field_validator("documents")
    @classmethod
    def _documents_non_empty(cls, v):
        if not v:
            raise ValueError("documents 不能为空")
        return v


@app.get("/health")
def health():
    return {"status": "ok", "service": "ceb-doc-verifier", "version": API_VERSION,
            "rule_version": doc_contract.ROUTE_RULE_VERSION,
            "ocr_available": pdf_ingest.ocr_available()}


@app.post("/verify")
def verify(batch: BatchIn):
    """
    请求体即批次JSON（与 sample_data/*.json 同构）：
    {"batch_id": "...", "documents": [{"doc_type", "doc_id", "title", "fields": {...}}, ...]}

    结构错误（documents 缺失/为空/含null、fields 类型错误等）由 Pydantic 校验
    返回 422，detail 指明具体字段；不做静默修正。
    返回：results（明细）、summary（计数）、risk（评分+构成）、suggestions（修正建议）。
    """
    return run_verification(batch.model_dump())


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
async def ingest_image(file: UploadFile = File(...)):
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
async def ingest_pdf(file: UploadFile = File(...)):
    """
    PDF摄取：按页判型（文本直取/扫描OCR）→ 类型识别 → 字段解析。
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
