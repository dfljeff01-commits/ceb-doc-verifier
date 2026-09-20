# -*- coding: utf-8 -*-
"""网页端 → 核验API 客户端（自动携带登录令牌；API不可用时核验降级为进程内直连）。"""

from __future__ import annotations

import os

import requests
import streamlit as st

from verification_engine import run_verification
from webapp import session

API_URL = os.environ.get("VERIFY_API_URL", "http://localhost:8000")


@st.cache_data(ttl=10, show_spinner=False)
def api_healthy() -> bool:
    try:
        return requests.get(f"{API_URL}/health", timeout=1.5).ok
    except Exception:
        return False


def run_verification_effective(batch: dict) -> tuple[dict, str]:
    """优先调用核验API（携带登录令牌，服务端留痕"核验"操作）；
    API不可用时降级为进程内直连，保证使用不中断（此时由本地补记审计）。"""
    import audit
    if api_healthy():
        try:
            resp = requests.post(f"{API_URL}/verify", json=batch,
                                 headers=session.auth_headers(), timeout=15)
            if resp.status_code == 401:
                # 令牌过期/无效：清除会话要求重新登录
                session.logout()
                st.warning("登录状态已过期，请重新登录。")
                st.rerun()
            resp.raise_for_status()
            return resp.json(), "api"
        except Exception:
            pass
    verification = run_verification(batch)
    # API降级时API侧留痕不可用，由网页进程补记（口径一致）
    try:
        audit.record(session.current_username(), audit.VERIFY, "batch",
                     batch.get("batch_id") or "(未编号)",
                     detail={"summary": verification.get("summary"),
                             "via": "local-fallback"})
    except Exception:
        pass
    return verification, "local"


def fetch_batch_full(batch_id: str) -> requests.Response:
    """按批次编号取完整记录（含完整核验报告），携带登录令牌（API侧留痕"查看报告"）。"""
    return requests.get(f"{API_URL}/mobile/batch/{batch_id}",
                        params={"include_full": "true"},
                        headers=session.auth_headers(), timeout=8)
