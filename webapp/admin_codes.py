# -*- coding: utf-8 -*-
"""代码字典维护页（仅管理员）——发站/口岸/目的地缩写的增改停用。

背景（补充任务书）：编号引擎对未登记缩写直接报错（保守设计，不瞎猜），
但 train_code_dict 此前只有 API 没有网页界面，业务负责人无法自行登记
新缩写，导入真实数据遇到"未登记缩写"只能找工程师。本页给
GET/PUT /datacheck/codes 配一个维护界面，业务可自助处理。

口径：
  - 缩写格式校验直接调用 train_number.is_valid_code（编号引擎同一函数，
    保证"网页允许录入的，编号引擎必然接受"，绝不出现两边口径不一致）；
  - 停用是软删除（active=false）：历史班列编号的可读性不受影响，
    只是新建班列时不再可选该缩写；
  - 同类别同缩写不能重复登记（已存在时明确提示，不静默覆盖）；
  - 新增/修改/停用全部审计留痕（DC_CODE_CREATE/UPDATE/DISABLE）。
"""

import os

import requests
import streamlit as st

import audit
import train_number
import train_store
from webapp import session

API_URL = os.environ.get("VERIFY_API_URL", "http://localhost:8000")

CATEGORY_LABELS = dict(train_number.CATEGORY_LABELS)   # station/port/dest → 中文


# ---------------------------------------------------------------- API 封装（含本地降级）

def _headers() -> dict:
    try:
        return session.auth_headers()
    except Exception:
        return {}


def _safe_audit(action, object_id, **kw) -> None:
    try:
        audit.record(session.current_username(), action, "train_code_dict",
                     object_id, **kw)
    except Exception as exc:
        print(f"[admin_codes] 审计写入失败（{action}）: {exc}")


def dc_load_codes() -> list[dict] | None:
    """全量字典（含停用，供维护页展示与判重）。API不可用走本地直连。"""
    try:
        resp = requests.get(f"{API_URL}/datacheck/codes", timeout=10,
                            headers=_headers())
        if resp.status_code == 401:
            session.logout()
            st.warning("登录状态已过期，请重新登录。")
            st.rerun()
        resp.raise_for_status()
        return resp.json().get("codes", [])
    except requests.RequestException:
        try:
            return train_store.list_codes()
        except Exception as exc:
            st.error(f"读取代码字典失败：{exc}")
            return None


def dc_put_code(payload: dict, mode: str = "upsert") -> tuple[bool, str]:
    """登记/修改/停用。返回 (成功?, 消息)。本地降级直连 train_store。"""
    try:
        resp = requests.put(
            f"{API_URL}/datacheck/codes", params={"mode": mode},
            json=payload, timeout=10, headers=_headers())
        if resp.status_code == 401:
            session.logout()
            st.warning("登录状态已过期，请重新登录。")
            st.rerun()
        if resp.status_code == 422:
            return False, resp.json().get("detail", resp.text)
        resp.raise_for_status()
        return True, "已保存（操作已留痕）。"
    except requests.RequestException:
        # 本地降级：复刻 API 的判重与审计口径
        code = str(payload.get("code") or "").strip().upper()
        existing = next((c for c in (train_store.list_codes()
                         if payload else [])
                         if c["category"] == payload.get("category")
                         and c["code"] == code), None)
        if mode == "create" and existing is not None:
            return False, (f"{payload.get('category')}类别下缩写 {code} 已登记，"
                           "不能重复登记；如需修改请用编辑功能。")
        try:
            row = train_store.upsert_code(payload["category"], code,
                                          payload["name"], payload.get("sort", 0),
                                          payload.get("active", True),
                                          session.current_username())
        except Exception as exc:
            return False, str(exc)
        if existing is None:
            action = audit.DC_CODE_CREATE
        elif existing.get("active") and not payload.get("active", True):
            action = audit.DC_CODE_DISABLE
        else:
            action = audit.DC_CODE_UPDATE
        _safe_audit(action, f"{payload['category']}/{code}",
                    before=existing, after=row)
        return True, "已保存（本地直连模式，操作已留痕）。"


# ---------------------------------------------------------------- 页面

def render() -> None:
    st.header("🔤 代码字典维护")
    st.caption("仅管理员可见 · 班列编号使用的 发站/口岸/目的地 缩写登记。"
               "缩写规则与编号引擎完全一致：1-8位大写字母/数字，不含连字符。"
               "停用为软删除——历史班列编号不受影响，仅新建班列不再可选。")

    codes = dc_load_codes()
    if codes is None:
        return

    tab_add, tab_edit, tab_disable = st.tabs(
        ["➕ 新增缩写", "✏️ 修改名称/排序", "⏸️ 停用缩写"])

    with tab_add:
        _render_add(codes)
    with tab_edit:
        _render_edit(codes)
    with tab_disable:
        _render_disable(codes)

    st.divider()
    _render_list(codes)


def _render_add(codes: list[dict]) -> None:
    with st.form("code_add_form", border=True):
        c1, c2 = st.columns(2)
        with c1:
            category = st.selectbox(
                "类别 *", list(CATEGORY_LABELS),
                format_func=lambda c: CATEGORY_LABELS[c], key="ca_cat")
            code_raw = st.text_input(
                "缩写 *（1-8位大写字母/数字，不含连字符）", key="ca_code",
                placeholder="如 PW / MZL / KZ").strip().upper()
        with c2:
            name = st.text_input("中文名称 *", key="ca_name",
                                 placeholder="如 平旺 / 满洲里 / 哈萨克斯坦")
            sort = st.number_input("排序序号（同类别内展示顺序）", min_value=0,
                                   step=1, value=0, key="ca_sort")
        submitted = st.form_submit_button("➕ 登记缩写", type="primary")

    if not submitted:
        return
    # 格式校验：与编号引擎同一函数（train_number.is_valid_code），
    # 报错口径与引擎一致（连字符是编号分隔符，绝不能出现在缩写里）
    if not code_raw:
        st.error("请填写缩写。")
        return
    if not train_number.is_valid_code(code_raw):
        st.error(f"缩写「{code_raw}」不合法：只能是1-8位大写字母/数字，"
                 "不能包含连字符（连字符是班列编号的字段分隔符）——"
                 "与编号引擎校验口径一致。")
        return
    if not name.strip():
        st.error("请填写中文名称。")
        return
    # 判重：同类别同缩写（不论启用/停用）明确提示
    dup = [c for c in codes if c["category"] == category
           and c["code"] == code_raw]
    if dup:
        state = "启用" if dup[0].get("active") else "已停用"
        st.error(f"{CATEGORY_LABELS[category]}类别下缩写 {code_raw} 已登记"
                 f"（{state}，名称：{dup[0].get('name')}），不能重复登记；"
                 f"如需修改请用「修改名称/排序」页签。")
        return
    ok, msg = dc_put_code({"category": category, "code": code_raw,
                           "name": name.strip(), "sort": int(sort),
                           "active": True}, mode="create")
    if ok:
        st.success(f"缩写已登记：{category}/{code_raw} = {name.strip()}（已留痕）")
        st.rerun()
    else:
        st.error(msg)


def _render_edit(codes: list[dict]) -> None:
    if not codes:
        st.info("字典为空，请先在「新增缩写」页签登记。")
        return
    options = [f"[{CATEGORY_LABELS.get(c['category'], c['category'])}] "
               f"{c['code']} = {c['name']}"
               + ("" if c.get("active") else "（已停用）") for c in codes]
    with st.form("code_edit_form", border=True):
        pick = st.selectbox("选择缩写", options, key="ce_pick")
        idx = options.index(pick)
        current = codes[idx]
        c1, c2 = st.columns(2)
        with c1:
            new_name = st.text_input("中文名称", value=current["name"],
                                     key="ce_name")
        with c2:
            new_sort = st.number_input("排序序号", min_value=0, step=1,
                                       value=int(current.get("sort") or 0),
                                       key="ce_sort")
        submitted = st.form_submit_button("💾 保存修改", type="primary")
    if not submitted:
        return
    if not new_name.strip():
        st.error("中文名称不能为空。")
        return
    ok, msg = dc_put_code({"category": current["category"],
                           "code": current["code"],
                           "name": new_name.strip(),
                           "sort": int(new_sort),
                           "active": bool(current.get("active"))})
    if ok:
        st.success(f"已修改 {current['code']}（已留痕）。")
        st.rerun()
    else:
        st.error(msg)


def _render_disable(codes: list[dict]) -> None:
    active_codes = [c for c in codes if c.get("active")]
    if not active_codes:
        st.info("当前没有启用中的缩写。")
        return
    st.caption("停用只影响「新建班列时是否还能选到该缩写」；历史班列编号的"
               "可读性与既有记录不受影响。已停用的缩写可在下方列表中重新启用。")
    options = [f"[{CATEGORY_LABELS.get(c['category'], c['category'])}] "
               f"{c['code']} = {c['name']}" for c in active_codes]
    pick = st.selectbox("选择要停用的缩写", options, key="cd_pick")
    current = active_codes[options.index(pick)]
    c1, c2 = st.columns(2)
    with c1:
        if st.button("⏸️ 停用该缩写", type="primary",
                     disabled=not current.get("active")):
            ok, msg = dc_put_code({"category": current["category"],
                                   "code": current["code"],
                                   "name": current["name"],
                                   "sort": int(current.get("sort") or 0),
                                   "active": False})
            if ok:
                st.success(f"已停用 {current['code']}（已留痕，历史编号不受影响）。")
                st.rerun()
            else:
                st.error(msg)
    with c2:
        st.write("")

    # 重新启用区（对已停用的缩写）
    disabled_codes = [c for c in codes if not c.get("active")]
    if disabled_codes:
        with st.expander(f"♻️ 已停用的缩写（{len(disabled_codes)} 个，点击重新启用）"):
            for c in disabled_codes:
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    st.markdown(f"[{CATEGORY_LABELS.get(c['category'], c['category'])}]"
                                f" **{c['code']}** = {c['name']}")
                with col_b:
                    if st.button("启用", key=f"cd_enable::{c['category']}::{c['code']}"):
                        ok, msg = dc_put_code({"category": c["category"],
                                               "code": c["code"],
                                               "name": c["name"],
                                               "sort": int(c.get("sort") or 0),
                                               "active": True})
                        if ok:
                            st.success(f"已重新启用 {c['code']}（已留痕）。")
                            st.rerun()
                        else:
                            st.error(msg)


def _render_list(codes: list[dict]) -> None:
    st.markdown("##### 📋 全部缩写（按类别分组）")
    if not codes:
        st.info("字典为空。")
        return
    for category, label in CATEGORY_LABELS.items():
        group = sorted([c for c in codes if c["category"] == category],
                       key=lambda c: (c.get("sort") or 0, c["code"]))
        if not group:
            continue
        rows = [{"缩写": c["code"], "中文名称": c["name"],
                 "排序": c.get("sort") or 0,
                 "状态": "✅ 启用" if c.get("active") else "⏸️ 已停用",
                 "最后修改人": c.get("updated_by") or "—",
                 "最后修改时间": str(c.get("updated_at") or "—")[:19]}
                for c in group]
        st.markdown(f"**{label}（{category}）**")
        st.dataframe(rows and __import__("pandas").DataFrame(rows),
                     use_container_width=True, hide_index=True)
