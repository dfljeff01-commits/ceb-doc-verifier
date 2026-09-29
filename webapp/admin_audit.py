# -*- coding: utf-8 -*-
"""操作日志页（仅管理员）：按 操作人/操作类型/时间范围 筛选查看审计记录。

审计记录本身只增不改（应用层无修改/删除入口 + 数据库触发器拒绝 UPDATE/
DELETE/TRUNCATE），本页仅提供只读查询。
"""

from datetime import date

import streamlit as st

import audit
from webapp.format import SECOND_FMT, fmt_dt
from webapp.styles import APP_CSS

#: detail 字典键 → 中文标签（未收录的键原样展示，保证留痕不丢信息）
_DETAIL_KEY_LABELS = {
    "via": "来源", "reason": "原因", "user": "用户", "username": "用户",
    "count": "命中数", "photos": "张数", "source": "上传来源",
    "risk_grade": "风险等级", "risk_score": "风险分", "batch": "批次",
    "lookup": "检索词", "comp": "申报构成", "fields": "字段数",
    "object": "对象", "message": "说明",
}

_ACTION_ICONS = {"LOGIN": "🔑", "LOGOUT": "🚪", "LOGIN_FAILED": "⚠️",
                 "UPLOAD_DOCS": "📎", "VERIFY": "🔍", "EDIT_FIELD": "✍️",
                 "VIEW_REPORT": "📄", "GENERATE_EMAIL": "✉️",
                 "GENERATE_REPORT_PDF": "⬇️", "QUICK_CHECK": "📱",
                 "CREATE_USER": "➕", "ENABLE_USER": "✅",
                 "DISABLE_USER": "🚫", "RESET_PASSWORD": "🔑"}


def _fmt_value(value) -> str:
    """嵌套 dict/list → 可读短语（递归展开，不引入原始 JSON 语法）。"""
    if value is None:
        return "—"
    if isinstance(value, dict):
        return "、".join(f"{_DETAIL_KEY_LABELS.get(k, k)} {_fmt_value(v)}"
                        for k, v in value.items()) or "—"
    if isinstance(value, (list, tuple)):
        return "、".join(_fmt_value(v) for v in value) or "—"
    return str(value)


def _fmt_detail(detail) -> str:
    """detail 摘要：常见键给中文，核验汇总给结论式描述。"""
    if not detail:
        return "—"
    if not isinstance(detail, dict):
        return _fmt_value(detail)
    parts = []
    for key, value in detail.items():
        if key == "summary" and isinstance(value, dict):
            parts.append(f"核验 {value.get('total', '—')} 项："
                         f"通过 {value.get('pass', 0)} · "
                         f"警告 {value.get('warning', 0)} · "
                         f"不合格 {value.get('fail', 0)}")
        elif key == "one_line" and isinstance(value, str) and len(value) > 40:
            parts.append(f"摘要：{value[:40]}…")
        else:
            parts.append(f"{_DETAIL_KEY_LABELS.get(key, key)}：{_fmt_value(value)}")
    return "；".join(parts) or "—"


def render() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)
    st.title("📜 操作日志")
    st.caption("仅管理员可见 · 审计记录只增不改（数据库层拒绝修改/删除/清空）")

    with st.form("audit_filter_form"):
        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
        with c1:
            username = st.text_input("操作人（精确匹配，留空=全部）")
        with c2:
            action_label = st.selectbox(
                "操作类型", ["全部"]
                + [f"{code}（{label}）" for code, label in audit.ACTION_LABELS.items()])
        with c3:
            since = st.date_input("起始日期（含）", value=None, min_value=date(2026, 1, 1))
        with c4:
            until = st.date_input("结束日期（含）", value=None)
        go = st.form_submit_button("查询", type="primary")

    # 默认展示最近100条；提交筛选后按条件查询
    action = None
    if go and action_label != "全部":
        action = action_label.split("（", 1)[0]
    # 直接传 date：audit.query 对 date 类型按"含当日"处理（until=次日零点前）
    since_dt = since
    until_dt = until

    try:
        rows = audit.query(username=username.strip() or None, action=action,
                           since=since_dt, until=until_dt, limit=200)
        total = audit.count(username=username.strip() or None, action=action,
                            since=since_dt, until=until_dt)
    except Exception as exc:
        st.error(f"查询失败：{exc}")
        return

    st.markdown(f"共 **{total}** 条记录（显示最近 {len(rows)} 条，时间倒序）")
    if not rows:
        st.info("没有符合条件的记录。")
        return

    table, raw = [], []
    for r in rows[:100]:
        detail_text = _fmt_detail(r.get("detail"))
        if r["action"] == audit.EDIT_FIELD and (
                r.get("before_value") is not None or r.get("after_value") is not None):
            detail_text = (f"修改前 {_fmt_value(r.get('before_value'))} → "
                           f"修改后 {_fmt_value(r.get('after_value'))}"
                           + (f"；{detail_text}" if detail_text != "—" else ""))
        obj = (f"{r['object_type']}:{r['object_id']}" if r.get("object_id")
               else (r.get("object_type") or "—"))
        table.append({
            "时间": fmt_dt(r["created_at"], SECOND_FMT),
            "操作人": r["username"],
            "操作": f"{_ACTION_ICONS.get(r['action'], '•')} {r['action_label']}",
            "动作码": r["action"],
            "对象": obj,
            "详情": detail_text,
            "IP": r.get("ip") or "—",
        })
        raw.append({"id": r.get("id"),
                    "created_at": fmt_dt(r.get("created_at"), SECOND_FMT),
                    "username": r.get("username"), "action": r.get("action"),
                    "object_type": r.get("object_type"),
                    "object_id": r.get("object_id"), "detail": r.get("detail"),
                    "before_value": r.get("before_value"),
                    "after_value": r.get("after_value"), "ip": r.get("ip")})

    st.dataframe(table, use_container_width=True, hide_index=True)
    with st.expander(f"原始记录（{len(raw)} 条 · 技术留痕，出问题时给运维看）"):
        st.json(raw)
