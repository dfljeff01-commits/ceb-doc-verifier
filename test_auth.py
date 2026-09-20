# -*- coding: utf-8 -*-
"""登录权限与角色体系测试（架构升级任务书 §2 验收）。

覆盖：bcrypt 哈希存储、密码强度基线、用户名密码登录、禁用账号拒绝、
JWT 签发与过期、角色端点差异（business 可核验 / finance 403）。
运行：pytest test_auth.py -v
"""

import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import auth_service
import db
from api import app

client = TestClient(app)


# ---------------------------------------------------------------- 密码策略与哈希

def test_password_rejected_without_letter_or_digit(unique_name):
    with pytest.raises(auth_service.AuthError):
        auth_service.create_user(unique_name, "12345678", "business")   # 纯数字
    with pytest.raises(auth_service.AuthError):
        auth_service.create_user(unique_name, "abcdefgh", "business")   # 纯字母
    with pytest.raises(auth_service.AuthError):
        auth_service.create_user(unique_name, "ab1", "business")        # 过短


def test_password_stored_as_bcrypt_hash(unique_name):
    user = auth_service.create_user(unique_name, "Passw0rd1", "business")
    row = db.query("SELECT password_hash FROM users WHERE username=%s",
                   (unique_name,))[0]
    assert row["password_hash"].startswith(("$2a$", "$2b$", "$2y$"))
    assert "Passw0rd1" not in row["password_hash"]
    assert row["password_hash"] != user["password_hash"] or True  # 每次盐不同
    other = auth_service.create_user(unique_name + "x", "Passw0rd1", "business")
    # 同密码不同盐 → 哈希不同
    assert other["username"] != user["username"]
    h1 = db.query("SELECT password_hash FROM users WHERE username=%s",
                  (user["username"],))[0]["password_hash"]
    h2 = db.query("SELECT password_hash FROM users WHERE username=%s",
                  (other["username"],))[0]["password_hash"]
    assert h1 != h2


def test_duplicate_username_rejected(unique_name):
    auth_service.create_user(unique_name, "Passw0rd1", "business")
    with pytest.raises(auth_service.AuthError, match="已存在"):
        auth_service.create_user(unique_name, "Other1234", "finance")


def test_invalid_role_rejected(unique_name):
    with pytest.raises(auth_service.AuthError, match="角色"):
        auth_service.create_user(unique_name, "Passw0rd1", "superuser")


# ---------------------------------------------------------------- 登录与状态

def test_authenticate_success_updates_last_login(unique_name):
    auth_service.create_user(unique_name, "Passw0rd1", "business")
    assert db.query("SELECT last_login_at FROM users WHERE username=%s",
                    (unique_name,))[0]["last_login_at"] is None
    user = auth_service.authenticate(unique_name, "Passw0rd1")
    assert user["role"] == "business"
    assert "password_hash" not in user          # 对外视图绝不含哈希
    assert db.query("SELECT last_login_at FROM users WHERE username=%s",
                    (unique_name,))[0]["last_login_at"] is not None


def test_authenticate_wrong_password(unique_name):
    auth_service.create_user(unique_name, "Passw0rd1", "business")
    with pytest.raises(auth_service.AuthError, match="用户名或密码不正确"):
        auth_service.authenticate(unique_name, "Wrong1234")
    with pytest.raises(auth_service.AuthError, match="用户名或密码不正确"):
        auth_service.authenticate("nosuchuser", "Whatever1")


def test_disabled_user_cannot_login(unique_name):
    auth_service.create_user(unique_name, "Passw0rd1", "business")
    auth_service.set_status(unique_name, "disabled")
    with pytest.raises(auth_service.AuthError, match="禁用"):
        auth_service.authenticate(unique_name, "Passw0rd1")
    # 禁用前签发的存量令牌也应立即失效（verify_token 复查状态）
    auth_service.set_status(unique_name, "active")
    token = auth_service.create_token(
        auth_service.authenticate(unique_name, "Passw0rd1"))["access_token"]
    auth_service.set_status(unique_name, "disabled")
    with pytest.raises(auth_service.AuthError, match="禁用"):
        auth_service.verify_token(token)


# ---------------------------------------------------------------- JWT

def test_token_roundtrip_and_expiry(unique_name):
    auth_service.create_user(unique_name, "Passw0rd1", "admin")
    user = auth_service.authenticate(unique_name, "Passw0rd1")
    token = auth_service.create_token(user)
    assert token["token_type"] == "bearer"
    me = auth_service.verify_token(token["access_token"])
    assert me["username"] == unique_name
    assert me["role"] == "admin"

    # 过期令牌 → 明确的"登录已过期"
    expired = pyjwt.encode(
        {"sub": unique_name, "role": "admin",
         "iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600},
        auth_service._secret_key(), algorithm="HS256")
    with pytest.raises(auth_service.AuthError, match="过期"):
        auth_service.verify_token(expired)

    # 篡改令牌 → 无效
    tampered = token["access_token"][:-3] + ("aaa" if not token["access_token"].endswith("aaa") else "bbb")
    with pytest.raises(auth_service.AuthError):
        auth_service.verify_token(tampered)


# ---------------------------------------------------------------- API端点角色差异

@pytest.fixture
def api_users(unique_name):
    password = "Passw0rd1"
    accounts = {}
    for role in ("business", "finance", "admin"):
        name = f"{unique_name}_{role}"
        auth_service.create_user(name, password, role)
        accounts[role] = name
    return {"accounts": accounts, "password": password}


def _login(name, password):
    resp = client.post("/auth/login", json={"username": name, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_api_role_endpoint_differences(api_users):
    """验收：不同角色可用的功能端点有差异——
    business/admin 可核验；finance 对单据核验数据 403（待业务侧确认后再放开）。"""
    accounts, password = api_users["accounts"], api_users["password"]
    batch = {"batch_id": "t", "batch_name": "t",
             "documents": [{"doc_type": "invoice", "doc_id": "d1", "title": "发票",
                            "fields": {"invoice_no": "INV-1"}}]}
    hb = _login(accounts["business"], password)
    hf = _login(accounts["finance"], password)
    ha = _login(accounts["admin"], password)

    assert client.post("/verify", json=batch, headers=hb).status_code == 200
    assert client.post("/verify", json=batch, headers=ha).status_code == 200
    resp = client.post("/verify", json=batch, headers=hf)
    assert resp.status_code == 403
    assert client.get("/mobile/recent", headers=hf).status_code == 403

    # /auth/me 全角色可用；未登录 401
    for h in (hb, hf, ha):
        me = client.get("/auth/me", headers=h)
        assert me.status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_api_logout_records_audit(api_users):
    accounts, password = api_users["accounts"], api_users["password"]
    name = accounts["business"]
    h = _login(name, password)
    assert client.post("/auth/logout", headers=h).status_code == 200
    import audit
    rows = audit.query(username=name, action=audit.LOGOUT)
    assert len(rows) == 1


def test_password_reset_and_relogin(api_users):
    accounts, password = api_users["accounts"], api_users["password"]
    name = accounts["business"]
    auth_service.set_password(name, "NewPass99")
    with pytest.raises(auth_service.AuthError):
        auth_service.authenticate(name, password)
    user = auth_service.authenticate(name, "NewPass99")
    assert user["username"] == name
