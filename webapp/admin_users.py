# -*- coding: utf-8 -*-
"""用户管理页（仅管理员）：新增账号、启用/禁用、重置密码。"""

import streamlit as st

import auth_service
from webapp import session

ROLE_LABELS = auth_service.ROLE_LABELS


def render() -> None:
    st.header("👤 用户管理")
    st.caption("仅管理员可见：账号的新增 / 启用禁用 / 密码重置（全部操作留痕审计）")

    tab_list, tab_create, tab_pwd = st.tabs(["账号列表", "新增账号", "重置密码"])

    with tab_list:
        _render_user_table()
    with tab_create:
        _render_create_form()
    with tab_pwd:
        _render_password_form()


def _render_user_table() -> None:
    try:
        users = auth_service.list_users()
    except Exception as exc:
        st.error(f"读取用户失败：{exc}")
        return
    st.dataframe(
        [{"用户名": u["username"], "角色": u["role_label"], "状态": "启用" if u["status"] == "active" else "禁用",
          "创建时间": (u.get("created_at") or "—").strftime("%Y-%m-%d %H:%M") if u.get("created_at") else "—",
          "最后登录": (u.get("last_login_at") or "—").strftime("%Y-%m-%d %H:%M") if u.get("last_login_at") else "—"}
         for u in users],
        use_container_width=True, hide_index=True)

    st.markdown("##### 启用 / 禁用")
    me = session.current_username()
    target = st.selectbox("选择账号", [u["username"] for u in users],
                          key="admin_toggle_user",
                          format_func=lambda name: next(
                              (f"{u['username']}（{ROLE_LABELS.get(u['role'], u['role'])}，"
                               f"{'启用' if u['status'] == 'active' else '禁用'}）"
                               for u in users if u['username'] == name), name))
    user_row = next((u for u in users if u["username"] == target), None)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚫 禁用该账号",
                     disabled=(user_row is None or user_row["status"] == "disabled"
                               or target == me),
                     use_container_width=True):
            try:
                auth_service.set_status(target, "disabled", changed_by=me)
                st.success(f"已禁用 {target}")
                st.rerun()
            except auth_service.AuthError as exc:
                st.error(exc.message)
    with c2:
        if st.button("✅ 启用该账号", disabled=(user_row is None or user_row["status"] == "active"),
                     use_container_width=True):
            try:
                auth_service.set_status(target, "active", changed_by=me)
                st.success(f"已启用 {target}")
                st.rerun()
            except auth_service.AuthError as exc:
                st.error(exc.message)
    st.caption("禁用为软删除：历史记录与审计保留，仅拒绝登录。"
               "不能禁用当前登录的账号（防止自我锁定）。")


def _render_create_form() -> None:
    with st.form("create_user_form", clear_on_submit=True):
        st.markdown("新账号密码要求：至少8位，且同时包含字母和数字。")
        username = st.text_input("用户名（字母/数字/_.-，3~64位）")
        password = st.text_input("初始密码", type="password")
        role_label = st.select_slider("角色", options=["业务", "财务", "管理员"])
        submit = st.form_submit_button("创建账号", type="primary")
    if submit:
        role = {"业务": "business", "财务": "finance", "管理员": "admin"}.get(role_label, "business")
        try:
            auth_service.create_user(username.strip(), password, role,
                                     created_by=session.current_username())
            st.success(f"账号已创建：{username.strip()}（{role_label}）")
        except auth_service.AuthError as exc:
            st.error(exc.message)


def _render_password_form() -> None:
    with st.form("reset_pwd_form", clear_on_submit=True):
        st.markdown("新密码要求：至少8位，且同时包含字母和数字。")
        username = st.text_input("目标用户名")
        new_password = st.text_input("新密码", type="password")
        submit = st.form_submit_button("重置密码", type="primary")
    if submit:
        try:
            auth_service.set_password(username.strip(), new_password,
                                      changed_by=session.current_username())
            st.success(f"已重置 {username.strip()} 的密码")
        except auth_service.AuthError as exc:
            st.error(exc.message)
