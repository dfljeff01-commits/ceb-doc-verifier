# -*- coding: utf-8 -*-
"""
App轻量结果存储（产品定位"电脑端是大脑、手机端是触手"的配套模块）。

手机App拍照上传走 /mobile/quick-check 时：
  1. 组装批次 → 核验引擎完整核验（与电脑端同一引擎、同一口径）；
  2. 完整核验报告 + 原始单证持久化到 mobile_results/ 目录（每批次一个JSON，原子写）；
  3. 只向App返回轻量摘要：风险等级（绿/黄/红）+ 一句话关键问题 + 批次编号，
     不含核验明细/分数构成/AI建议——复杂展示交给电脑端。

持久化的完整记录服务于两个"回电脑端"场景：
  - 电脑端网页：输入批次编号查看完整报告（GET /mobile/batch/{id}?include_full=true）
  - 手机现场速查：按运单号/单证编号/批次编号检索历史结论（GET /mobile/lookup?q=...）

实现口径：演示规模用"一批次一JSON文件 + 读取时目录扫描"，不引入数据库依赖；
文件名只使用受控字符集（批次编号白名单校验），查询大小写不敏感。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

import doc_contract

# 存储目录：默认项目根 mobile_results/，可用环境变量覆盖（测试隔离用）
DEFAULT_STORE_DIR = Path(__file__).parent / "mobile_results"
_ENV_DIR = os.environ.get("MOBILE_RESULTS_DIR", "")

# 批次编号白名单（只允许自产编号格式字符，防路径注入）
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# 现场速查索引字段：各单证类型身份编号 + 箱号（现场随手可抄/可扫的编号）
_INDEX_FIELDS = [
    "invoice_no", "packing_list_no", "waybill_no", "declaration_no",
    "co_no", "container_no",
]

_LOCK = threading.Lock()

# 风险等级映射（引擎 grade → App红黄绿）
GRADE_TO_LEVEL = {"low": "green", "medium": "yellow", "high": "red"}


def store_dir() -> Path:
    return Path(_ENV_DIR) if _ENV_DIR else DEFAULT_STORE_DIR


def new_batch_id() -> str:
    """App批次编号：MB-日期时间-4位随机（现场抄写/电脑端输入都方便）。"""
    return f"MB-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def _norm(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _short(text: str, limit: int = 60) -> str:
    """一句话摘要截断：去掉换行，超长截断加省略号（完整内容在电脑端查看）。"""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def summarize(documents: list, verification: dict) -> dict:
    """由完整核验结果生成轻量摘要——只含等级+一句话关键问题，不含任何明细。

    一句话选取口径：最严重的FAIL项 > 最先出现的WARNING项 > 全部通过。
    """
    risk = verification.get("risk", {})
    grade = risk.get("grade", "low")
    top = None
    for status in ("FAIL", "WARNING"):
        top = next((r for r in verification.get("results", [])
                    if r.get("status") == status), None)
        if top is not None:
            break
    if top is not None:
        one_line = f"{top.get('check_name', '核验提示')}：{_short(top.get('detail'))}"
    else:
        one_line = "全部检查项通过，未发现问题"
    return {
        "risk_grade": grade,
        "risk_level": GRADE_TO_LEVEL.get(grade, "green"),
        "risk_label": risk.get("grade_label", ""),
        "risk_score": int(risk.get("score", 0)),
        "one_line": one_line,
        "doc_count": len(documents),
        "doc_types": sorted({d.get("doc_type") or "unknown" for d in documents}),
    }


def _identity_numbers(documents: list) -> dict:
    """抽取现场可检索的编号（运单号/单证编号/箱号），值归一化为大写无空格。"""
    index: dict = {}
    for doc in documents:
        fields = doc.get("fields") or {}
        for key in _INDEX_FIELDS:
            value = fields.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            index.setdefault(key, [])
            if _norm(value) not in index[key]:
                index[key].append(_norm(value))
    return index


def _path(batch_id: str) -> Path:
    return store_dir() / f"{batch_id}.json"


def save_batch(documents: list, verification: dict, source: str = "mobile_app") -> dict:
    """持久化一个批次的完整记录，返回记录（调用方自行裁剪轻量视图）。"""
    record = {
        "batch_id": verification.get("batch_id", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "lite": summarize(documents, verification),
        "identity_numbers": _identity_numbers(documents),
        "documents": documents,
        "verification": verification,
    }
    if not _BATCH_ID_RE.match(record["batch_id"]):
        raise ValueError(f"批次编号不合法：{record['batch_id']!r}")
    directory = store_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = _path(record["batch_id"]).with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, _path(record["batch_id"]))
    return record


def lite_view(record: dict) -> dict:
    """对外轻量视图：摘要 + 编号索引 + 元信息，绝不含核验明细/单证字段。"""
    return {
        "batch_id": record.get("batch_id", ""),
        "created_at": record.get("created_at", ""),
        "source": record.get("source", ""),
        **record.get("lite", {}),
        "identity_numbers": record.get("identity_numbers", {}),
    }


def get_batch(batch_id: str) -> dict | None:
    """按批次编号取完整记录（含verification/documents，供电脑端复查）。"""
    if not _BATCH_ID_RE.match(batch_id or ""):
        return None
    path = _path(batch_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def lookup(query: str, limit: int = 5) -> list[dict]:
    """现场速查：按 运单号/单证编号/箱号/批次编号 检索历史核验结论。

    命中口径（大小写/空格不敏感）：批次编号精确匹配，或与任一索引编号互为包含
    （编号长度≥4才参与包含匹配，避免过短查询误报）。返回按时间倒序的轻量视图。
    """
    q = _norm(query)
    if not q:
        return []
    hits = []
    for path in store_dir().glob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        batch_id = record.get("batch_id", "")
        numbers = [n for values in (record.get("identity_numbers") or {}).values()
                   for n in values]
        matched = (batch_id.upper() == q
                   or any(q == n or (len(q) >= 4 and q in n) or (len(n) >= 4 and n in q)
                          for n in numbers))
        if matched:
            hits.append((record.get("created_at", ""), record))
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return [lite_view(record) for _, record in hits[:max(1, limit)]]


def recent(limit: int = 20) -> list[dict]:
    """最近上传的批次（轻量视图，调试/电脑端一览用）。"""
    hits = []
    for path in store_dir().glob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        hits.append((record.get("created_at", ""), record))
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return [lite_view(record) for _, record in hits[:max(1, limit)]]
