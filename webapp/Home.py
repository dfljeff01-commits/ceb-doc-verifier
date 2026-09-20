# -*- coding: utf-8 -*-
"""
网页端主入口（webapp 双入口架构）。

结构：
  1. 登录门禁：未登录显示登录页（用户名+密码，内部系统口径）；
  2. 角色化导航：登录后按角色装配页面（st.navigation）——
       business 业务   : 单据核对
       finance 财务    : 数据核对（建设中占位）
       admin   管理员  : 单据核对 + 数据核对 + 用户管理 + 操作日志
     财务角色对"单据核对"的可见性待业务侧最终确认，本轮默认不可见
     （任务书 §2/§4；调整只需改下方 ROLE_PAGES）；
  3. 首页：两大功能入口卡片（单据核对 / 数据核对）+ 管理员快捷入口。

运行：streamlit run webapp/Home.py --server.port 8501
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

import audit
import auth_service
from webapp import admin_audit, admin_users, data_check, doc_verify
from webapp import session
from webapp.styles import APP_CSS

st.set_page_config(
    page_title="中欧班列单证智能核验 · 内部系统",
    page_icon="🚂",
    layout="wide",
)
st.markdown(APP_CSS, unsafe_allow_html=True)

# 角色 → 可见页面（初版权限矩阵；后续角色/边界调整只改这里）
ROLE_PAGES = {
    "business": [
        ("单据核对", doc_verify.render, "🔍"),
    ],
    "finance": [
        ("数据核对", data_check.render, "📊"),
    ],
    "admin": [
        ("单据核对", doc_verify.render, "🔍"),
        ("数据核对", data_check.render, "📊"),
        ("用户管理", admin_users.render, "👤"),
        ("操作日志", admin_audit.render, "📜"),
    ],
}


# ---------------------------------------------------------------- 登录页

def _client_ip() -> str:
    try:
        return (st.context.headers.get("x-forwarded-for")
                or st.context.headers.get("x-real-ip") or "").split(",")[0].strip()
    except Exception:
        return ""


def render_login() -> None:
    st.markdown('<div class="ceb-login-box">', unsafe_allow_html=True)
    st.markdown("### 🚂 中欧班列单证智能核验系统")
    st.caption("内部业务系统 · 仅限公司内部网络访问 · 请使用公司分配的账号登录")

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("用户名", autocomplete="username")
        password = st.text_input("密码", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("登 录", type="primary", use_container_width=True)

    if submitted:
        try:
            user = auth_service.authenticate(username.strip(), password)
        except auth_service.AuthError as exc:
            try:
                audit.record(username.strip() or "anonymous", audit.LOGIN_FAILED,
                             "user", username.strip() or None,
                             detail={"reason": exc.message, "via": "web"},
                             ip=_client_ip())
            except Exception:
                pass
            st.error(exc.message)
        else:
            token = auth_service.create_token(user)
            try:
                audit.record(user["username"], audit.LOGIN, "user", user["username"],
                             detail={"via": "web"}, ip=_client_ip())
            except Exception:
                pass
            session.login(user, token["access_token"], token["expires_at"])
            st.rerun()

    st.caption("忘记密码请联系管理员重置。连续登录失败会记录在操作日志中。")
    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------- 首页（双入口卡片）

def render_home() -> None:
    user = session.current_user() or {}
    st.title("🏠 功能入口")
    st.caption(f"中欧班列单证智能核验 · 内部系统（初级版）　|　"
               f"当前登录：**{user.get('username', '—')}**（{user.get('role_label', '—')}）")

    role = user.get("role", "")
    col1, col2 = st.columns(2)
    with col1:
        allowed_doc = role in ("business", "admin")
        st.markdown(
            '<div class="ceb-entry-card">'
            '<span style="font-size:34px;">🔍</span>'
            '<div class="t">单据核对</div>'
            '<div class="d">上传/选择单证批次 → AI核验一致性、齐全性与路线合规 → '
            '风险评分与整改建议 → 生成整改邮件与PDF报告。<br>'
            '<span style="color:#94A3B8;">支持示例批次、PDF向导上传、App现场批次复查。</span></div>'
            '</div>', unsafe_allow_html=True)
        if allowed_doc:
            st.page_link(PAGE_DOC, label="进入单据核对 →", icon="🔍",
                         use_container_width=True)
        else:
            st.caption("（当前角色不可用：财务角色对单据核验数据的可见性待业务侧确认）")
    with col2:
        st.markdown(
            '<div class="ceb-entry-card">'
            '<span style="font-size:34px;">📊</span>'
            '<div class="t">数据核对</div>'
            '<div class="d">财务数据比对模块（建设中）。<br>'
            '<span style="color:#94A3B8;">比对逻辑与对账双方数据源待架构方明确后实施，'
            '当前为占位页面。</span></div>'
            '</div>', unsafe_allow_html=True)
        if role in ("finance", "admin"):
            st.page_link(PAGE_DATA, label="进入数据核对 →", icon="📊", use_container_width=True)
        else:
            st.caption("（当前角色不可用：数据核对面向财务/管理员角色）")

    if role == "admin":
        st.divider()
        st.markdown("##### 管理员快捷入口")
        c1, c2, _ = st.columns([1, 1, 2])
        with c1:
            st.page_link(PAGE_USERS, label="用户管理", icon="👤", use_container_width=True)
        with c2:
            st.page_link(PAGE_AUDIT, label="操作日志", icon="📜", use_container_width=True)

    st.divider()
    st.caption("本系统仅限公司内部部署使用，不面向公网提供服务（持续维护原则，见 README）。")


# ---------------------------------------------------------------- 组装导航

PAGE_HOME = st.Page(render_home, title="首页", icon="🏠", default=True)

if not session.is_logged_in():
    render_login()
    st.stop()

role = session.current_role()
PAGE_DOC = st.Page(doc_verify.render, title="单据核对", icon="🔍", url_path="doc-verify")
PAGE_DATA = st.Page(data_check.render, title="数据核对", icon="📊", url_path="data-check")
PAGE_USERS = st.Page(admin_users.render, title="用户管理", icon="👤", url_path="users")
PAGE_AUDIT = st.Page(admin_audit.render, title="操作日志", icon="📜", url_path="audit")
ALL_PAGES = {"单据核对": PAGE_DOC, "数据核对": PAGE_DATA,
             "用户管理": PAGE_USERS, "操作日志": PAGE_AUDIT}

pages = [PAGE_HOME] + [ALL_PAGES[name] for name, _, _ in ROLE_PAGES.get(role, [])]

# 侧栏：登录信息与登出（导航链接由 st.navigation 自动生成）
with st.sidebar:
    user = session.current_user() or {}
    st.markdown(f"👤 **{user.get('username', '—')}**（{user.get('role_label', '—')}）")
    expires = st.session_state.get(session.TOKEN_EXPIRES_KEY, "")
    if expires:
        st.caption(f"登录有效期至 {expires.replace('T', ' ')}")
    if st.button("🚪 退出登录", use_container_width=True):
        try:
            audit.record(session.current_username(), audit.LOGOUT, "user",
                         session.current_username(), detail={"via": "web"})
        except Exception:
            pass
        session.logout()
        st.rerun()

nav = st.navigation(pages)
nav.run()
