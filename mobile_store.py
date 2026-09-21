# -*- coding: utf-8 -*-
"""
核验结果存储（架构升级：JSON文件 → PostgreSQL 正式表结构）。

手机App拍照上传走 /mobile/quick-check、电脑端网页向导上传时：
  1. 组装批次 → 核验引擎完整核验（同一引擎、同一口径）；
  2. 完整记录写入 PostgreSQL（batches / documents / verification_issues 三张
     正式表；batches.verification 另存完整JSON快照，保证电脑端渲染零失真）；
  3. 只向App返回轻量摘要：风险等级（绿/黄/红）+ 一句话关键问题 + 批次编号。

持久化的完整记录服务于两个"回电脑端"场景：
  - 电脑端网页：输入批次编号查看完整报告（GET /mobile/batch/{id}?include_full=true）
  - 手机现场速查：按运单号/单证编号/批次编号检索历史结论（GET /mobile/lookup?q=...）

历史沿革：v1 用"一批次一JSON文件"（mobile_results/ 目录）；多人并发与数据
永久保留要求下迁移到 PostgreSQL。旧JSON目录仅作为迁移前备份保留，运行时
不再读写；历史数据用 migrate_json_to_pg.py 一次性迁入（幂等可重跑）。
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

import db

# 展示用业务时区：批次/日志时间统一按此时区呈现（与服务器系统时区解耦，
# 容器普遍跑UTC）。历史JSON里的无时区时间也按此时区解释，保证迁移零偏移。
def business_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("BUSINESS_TIMEZONE", "Asia/Shanghai"))


def _aware(dt: datetime) -> datetime:
    """无时区时间按业务时区解释；已带时区的保持绝对时刻不变。"""
    return dt.replace(tzinfo=business_tz()) if dt.tzinfo is None else dt


def _iso_local(value) -> str:
    """timestamptz → 业务时区的无时区后缀ISO串（与旧JSON展示口径一致）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.astimezone(business_tz()).replace(tzinfo=None).isoformat(
        timespec="seconds")

# 迁移前JSON备份目录（只读，供 migrate_json_to_pg.py 扫描；运行时不再写入）
LEGACY_JSON_DIR_NAME = "mobile_results"

# 批次编号白名单（只允许自产编号格式字符，防注入）
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# 现场速查索引字段：各单证类型身份编号 + 箱号（现场随手可抄/可扫的编号）
_INDEX_FIELDS = [
    "invoice_no", "packing_list_no", "waybill_no", "declaration_no",
    "co_no", "container_no",
]

# 风险等级映射（引擎 grade → App红黄绿）
GRADE_TO_LEVEL = {"low": "green", "medium": "yellow", "high": "red"}


def new_batch_id() -> str:
    """批次编号：MB-日期时间-4位随机（现场抄写/电脑端输入都方便）。"""
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


# ---------------------------------------------------------------- 写入

def save_batch(documents: list, verification: dict, source: str = "mobile_app",
               created_by: str | None = None, created_at: datetime | None = None) -> dict:
    """持久化一个批次的完整记录到 PostgreSQL，返回记录（调用方自行裁剪轻量视图）。

    - batches：批次元信息 + 轻量摘要列 + verification 完整JSON快照；
    - documents：逐单据一行（实例序号、类型、识别字段JSON、该单据核验状态）；
    - verification_issues：逐问题一行（FAIL/WARNING、规则版本、修改建议）。
    同一批次编号重复保存时整体替换（幂等，迁移重跑安全）。
    """
    record = {
        "batch_id": verification.get("batch_id", ""),
        "source": source,
        "created_by": created_by,
        "lite": summarize(documents, verification),
        "identity_numbers": _identity_numbers(documents),
        "documents": documents,
        "verification": verification,
    }
    if not _BATCH_ID_RE.match(record["batch_id"]):
        raise ValueError(f"批次编号不合法：{record['batch_id']!r}")
    created_at_dt = _aware(created_at) if created_at is not None \
        else datetime.now().astimezone()
    record["created_at"] = _iso_local(created_at_dt)

    lite = record["lite"]
    groups = {g.get("doc_id"): g for g in verification.get("document_groups", [])}

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO batches (batch_id, batch_name, created_at, created_by,
                                     source, risk_grade, risk_level, risk_score,
                                     risk_label, one_line, doc_count, doc_types,
                                     identity_numbers, declared_composition,
                                     rule_version, data_version, verification)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (batch_id) DO UPDATE SET
                    batch_name=EXCLUDED.batch_name, created_at=EXCLUDED.created_at,
                    created_by=EXCLUDED.created_by, source=EXCLUDED.source,
                    risk_grade=EXCLUDED.risk_grade, risk_level=EXCLUDED.risk_level,
                    risk_score=EXCLUDED.risk_score, risk_label=EXCLUDED.risk_label,
                    one_line=EXCLUDED.one_line, doc_count=EXCLUDED.doc_count,
                    doc_types=EXCLUDED.doc_types,
                    identity_numbers=EXCLUDED.identity_numbers,
                    declared_composition=EXCLUDED.declared_composition,
                    rule_version=EXCLUDED.rule_version,
                    data_version=EXCLUDED.data_version,
                    verification=EXCLUDED.verification
                """,
                (record["batch_id"],
                 str(verification.get("batch_name") or ""),
                 created_at_dt, created_by, source,
                 lite["risk_grade"], lite["risk_level"], lite["risk_score"],
                 lite["risk_label"], lite["one_line"], lite["doc_count"],
                 db.jsonb(lite["doc_types"]), db.jsonb(record["identity_numbers"]),
                 db.jsonb(verification.get("declared_composition")),
                 str(verification.get("rule_version") or ""),
                 str(verification.get("data_version") or ""),
                 db.jsonb(verification)))
            cur.execute("DELETE FROM documents WHERE batch_id = %s",
                        (record["batch_id"],))
            for idx, doc in enumerate(documents, start=1):
                group = groups.get(doc.get("doc_id") or "", {})
                fail_n = int(group.get("fail_count") or 0)
                warn_n = int(group.get("warning_count") or 0)
                status = "FAIL" if fail_n else ("WARNING" if warn_n else "PASS")
                cur.execute(
                    """INSERT INTO documents (batch_id, doc_index, doc_type, doc_id,
                                              title, fields, status, field_meta)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (record["batch_id"], idx,
                     str(doc.get("doc_type") or "unknown"),
                     str(doc.get("doc_id") or f"{record['batch_id']}-{idx}"),
                     str(doc.get("title") or ""), db.jsonb(doc.get("fields") or {}),
                     status, db.jsonb(doc.get("field_meta"))))
            cur.execute("DELETE FROM verification_issues WHERE batch_id = %s",
                        (record["batch_id"],))
            rule_version = str(verification.get("rule_version") or "")
            issue_rows = []
            for group in verification.get("document_groups", []):
                for issue in group.get("issues", []):
                    if issue.get("status") not in ("FAIL", "WARNING"):
                        continue
                    issue_rows.append((
                        record["batch_id"], group.get("doc_id"),
                        str(issue.get("check_id") or ""),
                        str(issue.get("check_name") or ""),
                        str(issue.get("category") or ""),
                        issue["status"],
                        issue.get("field"),
                        str(issue.get("detail") or ""),
                        str(issue.get("suggestion") or ""),
                        str(issue.get("rule_version") or rule_version)))
            for issue in verification.get("batch_level_issues", []):
                if issue.get("status") not in ("FAIL", "WARNING"):
                    continue
                issue_rows.append((
                    record["batch_id"], None,
                    str(issue.get("check_id") or ""),
                    str(issue.get("check_name") or ""),
                    str(issue.get("category") or ""),
                    issue["status"],
                    issue.get("field"),
                    str(issue.get("detail") or ""),
                    str(issue.get("suggestion") or ""),
                    str(issue.get("rule_version") or rule_version)))
            if issue_rows:
                cur.executemany(
                    """INSERT INTO verification_issues (batch_id, doc_id, check_id,
                           check_name, category, level, field, detail, suggestion,
                           rule_version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", issue_rows)
    return record


# ---------------------------------------------------------------- 读取

_LITE_COLUMNS = ("batch_id, created_at, source, created_by, risk_grade, risk_level,"
                 " risk_score, risk_label, one_line, doc_count, doc_types,"
                 " identity_numbers")


def _lite_view_from_row(row: dict) -> dict:
    """轻量视图（与旧JSON口径同构，另加创建人）：摘要 + 编号索引 + 元信息。"""
    return {
        "batch_id": row["batch_id"],
        "created_at": _iso_local(row["created_at"]),
        "source": row["source"],
        "created_by": row.get("created_by") or "",
        "risk_grade": row["risk_grade"],
        "risk_level": row["risk_level"],
        "risk_label": row["risk_label"],
        "risk_score": row["risk_score"],
        "one_line": row["one_line"],
        "doc_count": row["doc_count"],
        "doc_types": row["doc_types"],
        "identity_numbers": row["identity_numbers"],
    }


def lite_view(record: dict) -> dict:
    """完整记录 → 轻量视图：绝不含核验明细/单证字段。"""
    return {
        "batch_id": record.get("batch_id", ""),
        "created_at": record.get("created_at", ""),
        "source": record.get("source", ""),
        "created_by": record.get("created_by") or "",
        **record.get("lite", {}),
        "identity_numbers": record.get("identity_numbers", {}),
    }


def get_batch(batch_id: str) -> dict | None:
    """按批次编号取完整记录（含verification/documents，供电脑端复查）。"""
    if not _BATCH_ID_RE.match(batch_id or ""):
        return None
    rows = db.query(
        f"SELECT {_LITE_COLUMNS}, verification, batch_name, declared_composition"
        " FROM batches WHERE batch_id = %s", (batch_id,))
    if not rows:
        return None
    row = rows[0]
    doc_rows = db.query(
        "SELECT doc_index, doc_type, doc_id, title, fields, field_meta FROM documents"
        " WHERE batch_id = %s ORDER BY doc_index", (batch_id,))
    documents = []
    for r in doc_rows:
        d = {"doc_type": r["doc_type"], "doc_id": r["doc_id"],
             "title": r["title"], "fields": r["fields"]}
        if r.get("field_meta"):
            d["field_meta"] = r["field_meta"]
        documents.append(d)
    record = {
        "batch_id": row["batch_id"],
        "batch_name": row.get("batch_name") or "",
        "created_at": _iso_local(row["created_at"]),
        "source": row["source"],
        "created_by": row.get("created_by") or "",
        "lite": {
            "risk_grade": row["risk_grade"], "risk_level": row["risk_level"],
            "risk_label": row["risk_label"], "risk_score": row["risk_score"],
            "one_line": row["one_line"], "doc_count": row["doc_count"],
            "doc_types": row["doc_types"],
        },
        "identity_numbers": row["identity_numbers"],
        "declared_composition": row.get("declared_composition"),
        "documents": documents,
        "verification": row["verification"],
    }
    return record


_LOOKUP_SQL = f"""
SELECT {_LITE_COLUMNS}
FROM batches b
WHERE lower(b.batch_id) = %(q)s
   OR EXISTS (
       SELECT 1
       FROM jsonb_each(b.identity_numbers) AS kv(k, v),
            jsonb_array_elements_text(kv.v) AS num
       WHERE lower(num) = %(q)s
          OR (length(%(q)s) >= 4 AND position(%(q)s in lower(num)) > 0)
          OR (length(num) >= 4 AND position(lower(num) in %(q)s) > 0))
ORDER BY b.created_at DESC
LIMIT %(limit)s
"""


def lookup(query_text: str, limit: int = 5) -> list[dict]:
    """现场速查：按 运单号/单证编号/箱号/批次编号 检索历史核验结论。

    命中口径（大小写/空格不敏感，与JSON目录扫描版一致）：批次编号精确匹配，
    或与任一索引编号互为包含（编号长度≥4才参与包含匹配，避免过短查询误报）。
    返回按时间倒序的轻量视图。
    """
    q = _norm(query_text).lower()
    if not q:
        return []
    rows = db.query(_LOOKUP_SQL, {"q": q, "limit": max(1, limit)})
    return [_lite_view_from_row(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    """最近核验的批次（轻量视图，电脑端历史一览/运维检查用）。"""
    rows = db.query(
        f"SELECT {_LITE_COLUMNS} FROM batches ORDER BY created_at DESC"
        " LIMIT %s", (max(1, limit),))
    return [_lite_view_from_row(r) for r in rows]


def counts() -> dict:
    """数据量统计（迁移验证/运维巡检用）。"""
    def _c(table):
        return db.query(f"SELECT count(*) AS c FROM {table}")[0]["c"]
    return {"batches": _c("batches"), "documents": _c("documents"),
            "issues": _c("verification_issues"), "users": _c("users"),
            "audit_logs": _c("audit_logs")}
