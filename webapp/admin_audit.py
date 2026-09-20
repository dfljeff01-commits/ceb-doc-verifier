# -*- coding: utf-8 -*-
"""操作日志页（仅管理员）：按 操作人/操作类型/时间范围 筛选查看审计记录。

审计记录本身只增不改（应用层无修改/删除入口 + 数据库触发器拒绝 UPDATE/
DELETE/TRUNCATE），本页仅提供只读查询。
"""

from datetime import date

import streamlit as st

import audit
from webapp.styles import APP_CSS


def render() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)
    st.header("📜 操作日志")
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
    for r in rows[:100]:
        created = r["created_at"]
        when = created.astimezone().strftime("%Y-%m-%d %H:%M:%S") if created else "—"
        obj = f"{r['object_type']}:{r['object_id']}" if r.get("object_id") else (r.get("object_type") or "—")
        change = ""
        if r.get("before_value") is not None or r.get("after_value") is not None:
            change = (f"　修改前 `{r.get('before_value')}` → 修改后 `{r.get('after_value')}`"
                      if r["action"] == audit.EDIT_FIELD
                      else f"　数据：`{r.get('after_value') or r.get('detail') or ''}`")
        detail = f"　{r['detail']}" if r.get("detail") else ""
        ip = f"　IP {r['ip']}" if r.get("ip") else ""
        icon = {"LOGIN": "🔑", "LOGOUT": "🚪", "LOGIN_FAILED": "⚠️",
                "UPLOAD_DOCS": "📎", "VERIFY": "🔍", "EDIT_FIELD": "✍️",
                "VIEW_REPORT": "📄", "GENERATE_EMAIL": "✉️",
                "GENERATE_REPORT_PDF": "⬇️", "QUICK_CHECK": "📱",
                "CREATE_USER": "➕", "ENABLE_USER": "✅",
                "DISABLE_USER": "🚫", "RESET_PASSWORD": "🔑"}.get(r["action"], "•")
        st.markdown(
            f"{icon} `{when}`　**{r['username']}**　{r['action_label']}"
            f"（{r['action']}）　对象：{obj}{change}{detail}{ip}")
