# -*- coding: utf-8 -*-
"""
登录权限服务（架构升级：多人内部系统 → 用户名密码登录 + 角色权限）。

设计口径：
  - 密码必须以 bcrypt 哈希存储（cost 由库默认值决定），绝不存明文或可逆加密；
  - 角色（初版三角色，后续可扩展）：business 业务 / finance 财务 / admin 管理员；
  - 令牌：JWT HS256 签名，过期时间 AUTH_TOKEN_EXPIRE_HOURS（默认12小时），
    网页端会话内持有，App 端落地 SharedPreferences，到/临期需重新登录；
  - 密码强度基线：长度≥8，且同时包含字母和数字（内部系统不做复杂多因素）；
  - 账号状态 active/disabled，禁用账号立即无法登录（已有令牌在校验角色数据时
    也会复查状态）。

种子账号：init_db.py 在 users 表为空时按环境变量创建
（ADMIN_INITIAL_PASSWORD / BUSINESS_INITIAL_PASSWORD / FINANCE_INITIAL_PASSWORD，
未设置则生成随机密码打印到日志一次——只打印一次，之后无法找回只能重置）。
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

import audit
import db

ROLES = ("business", "finance", "admin")
ROLE_LABELS = {"business": "业务", "finance": "财务", "admin": "管理员"}

PASSWORD_MIN_LENGTH = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")

_JWT_ALGORITHM = "HS256"


class AuthError(Exception):
    """登录/账号管理类业务错误（对用户可读）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------- 密码策略与哈希

def validate_password_strength(password: str) -> None:
    """密码强度基线：≥8位且同时含字母与数字。不满足抛 AuthError。"""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError(f"密码长度至少{PASSWORD_MIN_LENGTH}位")
    if not re.search(r"[A-Za-z]", password):
        raise AuthError("密码必须同时包含字母和数字")
    if not re.search(r"[0-9]", password):
        raise AuthError("密码必须同时包含字母和数字")


def hash_password(password: str) -> str:
    """bcrypt 哈希（自动含盐，成本因子取库默认）。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 用户管理

def list_users() -> list[dict]:
    rows = db.query(
        "SELECT username, role, status, created_at, last_login_at "
        "FROM users ORDER BY id")
    for r in rows:
        r["role_label"] = ROLE_LABELS.get(r["role"], r["role"])
    return rows


def get_user(username: str) -> dict | None:
    rows = db.query("SELECT * FROM users WHERE username = %s", (username,))
    return rows[0] if rows else None


def create_user(username: str, password: str, role: str,
                created_by: str = "") -> dict:
    """新增账号：校验用户名格式/角色/密码强度，bcrypt 落库，并留痕（调用方负责审计）。"""
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("用户名只能包含字母/数字/_.-，长度3~64")
    if role not in ROLES:
        raise AuthError(f"角色必须是 {'/'.join(ROLES)} 之一")
    validate_password_strength(password)
    if get_user(username) is not None:
        raise AuthError(f"用户名已存在：{username}")
    db.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
        (username, hash_password(password), role))
    if created_by:
        audit.record(created_by, audit.CREATE_USER, "user", username,
                     after={"role": role})
    return get_user(username)


def set_password(username: str, new_password: str, changed_by: str = "") -> None:
    validate_password_strength(new_password)
    if get_user(username) is None:
        raise AuthError(f"用户不存在：{username}")
    db.execute("UPDATE users SET password_hash = %s WHERE username = %s",
               (hash_password(new_password), username))
    if changed_by:
        audit.record(changed_by, audit.RESET_PASSWORD, "user", username)


def set_status(username: str, status: str, changed_by: str = "") -> None:
    """启用/禁用账号。禁用是软删除：历史与审计保留，仅拒绝登录。"""
    if status not in ("active", "disabled"):
        raise AuthError("账号状态只能是 active/disabled")
    if get_user(username) is None:
        raise AuthError(f"用户不存在：{username}")
    db.execute("UPDATE users SET status = %s WHERE username = %s",
               (status, username))
    if changed_by:
        audit.record(changed_by,
                     audit.ENABLE_USER if status == "active" else audit.DISABLE_USER,
                     "user", username)


# ---------------------------------------------------------------- 登录与令牌

def authenticate(username: str, password: str) -> dict:
    """校验用户名密码。成功返回用户信息并更新最后登录时间；失败抛 AuthError。"""
    user = get_user(username or "")
    generic = AuthError("用户名或密码不正确")
    if user is None:
        raise generic
    if not verify_password(password or "", user["password_hash"]):
        raise generic
    if user["status"] != "active":
        raise AuthError("账号已被禁用，请联系管理员")
    db.execute("UPDATE users SET last_login_at = now() WHERE username = %s",
               (username,))
    return public_view(user)


def public_view(user: dict) -> dict:
    """对外安全的用户视图：绝不含密码哈希。"""
    return {
        "username": user["username"],
        "role": user["role"],
        "role_label": ROLE_LABELS.get(user["role"], user["role"]),
        "status": user["status"],
        "created_at": _iso(user.get("created_at")),
        "last_login_at": _iso(user.get("last_login_at")),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _secret_key() -> str:
    secret = os.environ.get("AUTH_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError(
            "AUTH_SECRET_KEY 未配置：登录令牌签名密钥必须由环境变量提供"
            "（参考 .env.example），不允许硬编码。")
    return secret


def token_expire_hours() -> int:
    raw = os.environ.get("AUTH_TOKEN_EXPIRE_HOURS", "12").strip()
    try:
        hours = int(raw)
    except ValueError:
        hours = 12
    return max(1, min(hours, 24 * 30))


def create_token(user: dict) -> dict:
    """签发 JWT：{sub=用户名, role=角色, exp}。返回 {token, expires_at, token_type}。"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=token_expire_hours())
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": expires,
    }
    token = jwt.encode(payload, _secret_key(), algorithm=_JWT_ALGORITHM)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires.isoformat(timespec="seconds"),
        "expires_in_hours": token_expire_hours(),
    }


def verify_token(token: str) -> dict:
    """校验 JWT 并复查账号当前状态（禁用账号的存量令牌立即失效）。"""
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise AuthError("登录凭证无效，请重新登录")
    user = get_user(payload.get("sub", ""))
    if user is None or user["status"] != "active":
        raise AuthError("账号不存在或已被禁用")
    view = public_view(user)
    view["token_exp"] = payload.get("exp")
    return view
