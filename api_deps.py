# -*- coding: utf-8 -*-
"""FastAPI 鉴权依赖（api.py 与 datacheck_api.py 共用，避免循环导入）。

从 api.py 原位抽出的 require_user / require_roles / get_client_ip：
  - require_user：Bearer JWT → 当前用户（缺失/过期/账号禁用一律 401）；
  - require_roles(*roles)：角色门禁工厂，不满足返回 403。
鉴权本体仍在 auth_service（bcrypt + JWT + 状态复查），此处只做HTTP层装配。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import auth_service

_security = HTTPBearer(auto_error=False, description="POST /auth/login 获取的JWT")


def get_client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def require_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(_security)) -> dict:
    """Bearer JWT → 当前用户。缺失/过期/禁用一律 401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="未登录或缺少访问令牌",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return auth_service.verify_token(credentials.credentials)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message,
                            headers={"WWW-Authenticate": "Bearer"})


def require_roles(*roles: str):
    """角色门禁：不满足返回 403（已登录但无权限）。"""
    def _dep(user: dict = Depends(require_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403,
                                detail="当前角色无权访问该功能")
        return user
    return _dep
