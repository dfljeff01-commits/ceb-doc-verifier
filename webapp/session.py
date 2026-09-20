# -*- coding: utf-8 -*-
"""网页端登录会话（Streamlit session_state 封装）。

网页登录口径：登录页校验用户名密码成功后，把用户信息与JWT令牌写入
session_state——令牌用于网页服务端调用核验API（Bearer），用户信息用于
角色导航与操作留痕。浏览器关闭/会话过期后需重新登录（内部系统可接受）。
"""

from __future__ import annotations

USER_KEY = "ceb_user"
TOKEN_KEY = "ceb_token"
TOKEN_EXPIRES_KEY = "ceb_token_expires"


def login(user: dict, token: str, expires_at: str = "") -> None:
    import streamlit as st
    st.session_state[USER_KEY] = user
    st.session_state[TOKEN_KEY] = token
    st.session_state[TOKEN_EXPIRES_KEY] = expires_at


def logout() -> None:
    import streamlit as st
    for key in (USER_KEY, TOKEN_KEY, TOKEN_EXPIRES_KEY):
        st.session_state.pop(key, None)


def is_logged_in() -> bool:
    import streamlit as st
    return bool(st.session_state.get(USER_KEY)) and bool(st.session_state.get(TOKEN_KEY))


def current_user() -> dict | None:
    import streamlit as st
    return st.session_state.get(USER_KEY)


def current_username() -> str:
    user = current_user()
    return (user or {}).get("username", "anonymous")


def current_role() -> str:
    user = current_user()
    return (user or {}).get("role", "")


def auth_headers() -> dict:
    """带令牌的请求头（网页服务端 → 核验API）。未登录返回空（降级直连场景）。"""
    import streamlit as st
    token = st.session_state.get(TOKEN_KEY)
    return {"Authorization": f"Bearer {token}"} if token else {}
