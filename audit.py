# -*- coding: utf-8 -*-
"""
操作日志（审计留痕）服务。

口径（架构升级任务书 §3）：
  - 记录：操作人、操作时间、操作类型、操作对象、编辑类操作的前后值；
  - 只增不改：本模块只提供 INSERT（record）与 SELECT（query/count），
    不存在任何 UPDATE/DELETE 入口；数据库层另有触发器兜底
    （db.AUDIT_GUARD_SQL：任何角色的 UPDATE/DELETE/TRUNCATE 直接报错），
    双重保障，审计记录不可被应用或 DBA 日常操作篡改。

操作类型常量（action）：登录/登出/登录失败、上传单据、核验、编辑字段、
查看报告、生成邮件、用户管理类操作（创建/启用/禁用/改密）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import db

# 操作类型常量（新增类型只在这里追加，避免魔法字符串散落）
LOGIN = "LOGIN"                    # 登录成功
LOGOUT = "LOGOUT"                  # 登出
LOGIN_FAILED = "LOGIN_FAILED"      # 登录失败（用户名或密码错误/账号禁用）
UPLOAD_DOCS = "UPLOAD_DOCS"        # 上传单据（网页向导 / App拍照即传）
VERIFY = "VERIFY"                  # 执行核验
EDIT_FIELD = "EDIT_FIELD"          # 人工编辑字段（记录前后值）
VIEW_REPORT = "VIEW_REPORT"        # 查看完整核验报告
GENERATE_EMAIL = "GENERATE_EMAIL"  # 生成沟通邮件
GENERATE_REPORT_PDF = "GENERATE_REPORT_PDF"  # 导出报告PDF
QUICK_CHECK = "QUICK_CHECK"        # App现场速查（检索历史结论）
CREATE_USER = "CREATE_USER"        # 新增账号
ENABLE_USER = "ENABLE_USER"        # 启用账号
DISABLE_USER = "DISABLE_USER"      # 禁用账号
RESET_PASSWORD = "RESET_PASSWORD"  # 重置密码

ACTION_LABELS = {
    LOGIN: "登录", LOGOUT: "登出", LOGIN_FAILED: "登录失败",
    UPLOAD_DOCS: "上传单据", VERIFY: "核验", EDIT_FIELD: "编辑字段",
    VIEW_REPORT: "查看报告", GENERATE_EMAIL: "生成邮件",
    GENERATE_REPORT_PDF: "导出报告PDF", QUICK_CHECK: "现场速查",
    CREATE_USER: "新增账号", ENABLE_USER: "启用账号",
    DISABLE_USER: "禁用账号", RESET_PASSWORD: "重置密码",
}


def record(username: str, action: str, object_type: str | None = None,
           object_id: str | None = None, detail: dict | None = None,
           before=None, after=None, ip: str | None = None) -> int:
    """追加一条审计记录，返回记录 id。审计失败会向上抛错（宁可失败不留黑洞）。"""
    rows = db.query(
        "INSERT INTO audit_logs (username, action, object_type, object_id,"
        " detail, before_value, after_value, ip)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (username or "anonymous", action, object_type, object_id,
         db.jsonb(detail), db.jsonb(before), db.jsonb(after), ip))
    return rows[0]["id"]


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def query(username: str | None = None, action: str | None = None,
          since=None, until=None, limit: int = 100, offset: int = 0) -> list[dict]:
    """按 操作人/操作类型/时间范围 筛选，时间倒序（管理员日志页用）。"""
    conditions, params = [], []
    if username:
        conditions.append("username = %s")
        params.append(username)
    if action:
        conditions.append("action = %s")
        params.append(action)
    if since is not None:
        conditions.append("created_at >= %s")
        params.append(_as_utc(since))
    if until is not None:
        conditions.append("created_at < %s")
        params.append(_as_utc(until) + timedelta(days=1)
                      if isinstance(until, date) and not isinstance(until, datetime)
                      else _as_utc(until))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    rows = db.query(
        "SELECT id, username, action, object_type, object_id, detail,"
        " before_value, after_value, ip, created_at"
        f" FROM audit_logs {where} ORDER BY created_at DESC, id DESC"
        " LIMIT %s OFFSET %s", tuple(params))
    for r in rows:
        r["action_label"] = ACTION_LABELS.get(r["action"], r["action"])
    return rows


def count(username: str | None = None, action: str | None = None,
          since=None, until=None) -> int:
    conditions, params = [], []
    if username:
        conditions.append("username = %s")
        params.append(username)
    if action:
        conditions.append("action = %s")
        params.append(action)
    if since is not None:
        conditions.append("created_at >= %s")
        params.append(_as_utc(since))
    if until is not None:
        conditions.append("created_at < %s")
        params.append(_as_utc(until) + timedelta(days=1)
                      if isinstance(until, date) and not isinstance(until, datetime)
                      else _as_utc(until))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return db.query(
        f"SELECT count(*) AS c FROM audit_logs {where}", tuple(params))[0]["c"]
