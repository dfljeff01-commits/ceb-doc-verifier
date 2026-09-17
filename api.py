# -*- coding: utf-8 -*-
"""
核验服务 API（升级任务书·工程化加固 + App任务书·图片摄取）。

端点：
  GET  /health         健康检查
  POST /verify         批次JSON核验（返回明细+风险评分+建议）
  POST /ingest/image   图片摄取（App拍照/相册）：OCR → 类型识别 → 字段解析
  POST /ingest/pdf     PDF摄取：按页判型 → 文本直取/OCR → 字段解析

运行：uvicorn api:app --host 0.0.0.0 --port 8000
文档：http://localhost:8000/docs （FastAPI 自动生成的 Swagger UI）
"""

from fastapi import FastAPI, File, HTTPException, UploadFile

import pdf_ingest
from verification_engine import run_verification

app = FastAPI(
    title="中欧班列单证智能核验 API",
    description="上传单证批次JSON或PDF/图片文件，返回核验明细、风险评分与AI修正建议。",
    version="1.2.0",
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ceb-doc-verifier", "version": "1.2.0",
            "ocr_available": pdf_ingest.ocr_available()}


@app.post("/verify")
def verify(batch: dict):
    """
    请求体即批次JSON（与 sample_data/*.json 同构）：
    {"batch_id": "...", "documents": [{"doc_type", "doc_id", "title", "fields": {...}}, ...]}

    返回：results（明细）、summary（计数）、risk（评分+构成）、suggestions（修正建议）。
    """
    if not isinstance(batch, dict) or not isinstance(batch.get("documents"), list):
        raise HTTPException(status_code=422, detail="请求体需为批次JSON，且包含 documents 列表")
    if not batch["documents"]:
        raise HTTPException(status_code=422, detail="documents 不能为空")
    return run_verification(batch)


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
    本机未安装OCR时返回 503 与安装提示。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    r = pdf_ingest.process_image(data, file.filename or "upload.jpg")
    if r.error and not pdf_ingest.ocr_available():
        raise HTTPException(status_code=503, detail=r.error)
    return _ingest_result_payload(r)


@app.post("/ingest/pdf")
async def ingest_pdf(file: UploadFile = File(...)):
    """PDF摄取：按页判型（文本直取/扫描OCR）→ 类型识别 → 字段解析。"""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    r = pdf_ingest.process_pdf(data, file.filename or "upload.pdf")
    return _ingest_result_payload(r)
