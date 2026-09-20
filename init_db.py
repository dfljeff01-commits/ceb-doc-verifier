# -*- coding: utf-8 -*-
"""
数据库初始化脚本：建表（幂等）+ 首次种子账号。

容器启动流程（start.sh）在服务拉起前执行本脚本，保证 schema 与初始账号就绪。
重复执行安全；users 表已有账号时不做任何种子操作（不影响现有账密）。

种子账号（仅 users 表为空时创建，密码取环境变量，未设置则生成随机密码
打印一次——之后无法找回，只能由管理员重置）：
  admin    管理员  ADMIN_USERNAME    / ADMIN_INITIAL_PASSWORD
  business 业务    BUSINESS_USERNAME / BUSINESS_INITIAL_PASSWORD
  finance  财务    FINANCE_USERNAME  / FINANCE_INITIAL_PASSWORD

运行：python init_db.py
"""

from __future__ import annotations

import os
import secrets

import audit
import auth_service
import db

SEED_ACCOUNTS = (
    # (用户名env, 密码env, 默认用户名, 角色)
    ("ADMIN_USERNAME", "ADMIN_INITIAL_PASSWORD", "admin", "admin"),
    ("BUSINESS_USERNAME", "BUSINESS_INITIAL_PASSWORD", "business", "business"),
    ("FINANCE_USERNAME", "FINANCE_INITIAL_PASSWORD", "finance", "finance"),
)


def seed_users_if_empty() -> list[tuple[str, str, str]]:
    """users 表为空时创建三个初始账号；返回 (用户名, 角色, 是否随机密码)。"""
    created = []
    if db.query("SELECT count(*) AS c FROM users")[0]["c"] > 0:
        return created
    for username_env, password_env, default_name, role in SEED_ACCOUNTS:
        username = os.environ.get(username_env, "").strip() or default_name
        password = os.environ.get(password_env, "").strip()
        random_password = not password
        if random_password:
            password = (secrets.token_urlsafe(6) + "7a")  # 保底含字母+数字
        auth_service.create_user(username, password, role)
        created.append((username, role, random_password))
        if random_password:
            print(f"[init_db] 初始账号 {username}（{role}）随机密码：{password}"
                  f" —— 仅此一次显示，请立即记录并尽快修改", flush=True)
    return created


def main() -> None:
    db.init_schema()
    counts = db.query(
        "SELECT (SELECT count(*) FROM users) AS users,"
        " (SELECT count(*) FROM batches) AS batches")[0]
    print(f"[init_db] schema 就绪：users={counts['users']} batches={counts['batches']}")
    created = seed_users_if_empty()
    if created:
        audit.record("system", audit.CREATE_USER, "system", "seed_accounts",
                     after={"users": [u for u, _, _ in created]})
    for username, role, random_pw in created:
        suffix = "（随机密码已打印一次）" if random_pw else "（密码来自环境变量）"
        print(f"[init_db] 种子账号就绪：{username} / {role}{suffix}")


if __name__ == "__main__":
    main()
