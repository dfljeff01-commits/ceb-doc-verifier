# -*- coding: utf-8 -*-
"""
向导式上传流程（任务书问题二）：把"上传→直接吐结果"改为分步骤引导。

  第1步 声明单据构成 —— 用户先声明"这批单证有哪几类、各几份"
                        （如 发票1、装箱单1、运单3、报关单1），
                        用于指导拆分识别并声明核验预期；
  第2步 逐类型上传 —— 每个类型独立上传位；一份PDF含多份单据时自动拆分
                      （复用 pdf_ingest.split_pdf 同源逻辑），拆分结果必须让
                      用户看到，边界不确定时逐页归属由用户确认；
  第3步 逐份确认识别结果 —— 每份单据独立展示、独立编辑（复用F05编辑契约）、
                            逐份勾选"已核对无误"；
  第4步 全部确认后核验 —— 组装批次（含 declared_composition）交给核验引擎，
                          产出可逐份追溯的分层结果。

本模块只做流程编排与展示，不做任何核验规则判断（规则全部在引擎）。
"""

from __future__ import annotations

import hashlib

import streamlit as st

import doc_contract
import pdf_ingest

# 声明构成的类型顺序（与引擎单证类型顺序一致）
COMPOSITION_ORDER = [
    "invoice",
    "packing_list",
    "railway_waybill",
    "smgs_rail_waybill",
    "export_customs_declaration",
    "certificate_of_origin",
]

COMPOSITION_LABELS = {
    "invoice": "商业发票",
    "packing_list": "装箱单",
    "railway_waybill": "国际铁路运单",
    "smgs_rail_waybill": "国际货协运单（СМГС）",
    "export_customs_declaration": "出口报关单",
    "certificate_of_origin": "原产地证书",
}

SHORT_LABELS = {"invoice": "发票", "packing_list": "装箱单", "railway_waybill": "运单",
                "smgs_rail_waybill": "运单",
                "export_customs_declaration": "报关单", "certificate_of_origin": "产地证"}

MAX_INSTANCES = 12     # 单个类型最多声明/拆分数（防呆上限）

PRESETS = {
    "常规整批：每类各1份": {t: 1 for t in COMPOSITION_ORDER},
    "多车厢分批：全套 + 运单3份": {**{t: 1 for t in COMPOSITION_ORDER},
                                   "railway_waybill": 3},
}

WIZ_STEPS = ["① 声明单据构成", "② 逐类型上传", "③ 逐份核对确认", "④ 核验结果"]


# ---------------------------------------------------------------- 会话状态


def _wkey(key: str, default=None):
    return st.session_state.get(key, default)


def reset_wizard() -> None:
    """清空向导状态（重置时用），含字段人工决策（wiz_fm）。"""
    for key in list(st.session_state.keys()):
        if key.startswith(("wiz_", "fld::pdf_wiz_", "wiz_fm::",
                           "edit_toggle::", "edit_val::", "edit_reason::")):
            del st.session_state[key]


def _file_sig(name: str, data: bytes) -> str:
    return f"{name}:{hashlib.sha256(data).hexdigest()[:16]}"


def _prune_removed_files(current_sigs: set) -> None:
    """上传控件里被移除的文件：联动清理其缓存与派生状态。"""
    stored = st.session_state.get("wiz_files") or {}
    for sig in list(stored.keys()):
        if sig not in current_sigs:
            stored.pop(sig, None)
            for key in list(st.session_state.keys()):
                if key.startswith((f"wiz_pg::{sig}::", f"wiz_gt::{sig}::",
                                   f"wiz_gn::{sig}", f"wiz_ok::{sig}::")):
                    del st.session_state[key]
    st.session_state["wiz_files"] = stored


# ---------------------------------------------------------------- 步骤指示器


def render_wizard_steps(current: int) -> None:
    spans = []
    for i, name in enumerate(WIZ_STEPS, start=1):
        if i < current:
            cls, mark = "done", "✓ "
        elif i == current:
            cls, mark = "cur", ""
        else:
            cls, mark = "", ""
        spans.append(f'<span class="{cls}">{mark}{name}</span>')
    st.markdown(f'<div class="ceb-step">{"".join(spans)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- 第1步：声明构成


def _step1_composition() -> None:
    st.subheader("第1步 · 声明单据构成")
    st.caption("先声明这批单证包含哪些类型、各几份（如多车厢分批发运可声明运单3份）。"
               "声明用于指导自动拆分与核验预期：同类型多份在申报份数内按独立单据核验，"
               "不再判为重复提交。")
    st.session_state.setdefault(
        "wiz_composition", {t: 1 for t in COMPOSITION_ORDER})

    preset_cols = st.columns([1.2] + [2] * len(PRESETS))
    with preset_cols[0]:
        st.markdown("快速预设：")
    for i, (name, preset) in enumerate(PRESETS.items()):
        with preset_cols[i + 1]:
            if st.button(name, key=f"wiz_preset::{i}", width="stretch"):
                for t in COMPOSITION_ORDER:
                    st.session_state[f"wiz_comp::{t}"] = int(preset.get(t, 0))
                st.session_state["wiz_composition"] = dict(preset)

    cols = st.columns(len(COMPOSITION_ORDER))
    for i, doc_type in enumerate(COMPOSITION_ORDER):
        with cols[i]:
            st.number_input(COMPOSITION_LABELS[doc_type], min_value=0,
                            max_value=MAX_INSTANCES, step=1,
                            key=f"wiz_comp::{doc_type}")
    composition = {t: int(st.session_state.get(f"wiz_comp::{t}", 0))
                   for t in COMPOSITION_ORDER}
    st.session_state["wiz_composition"] = composition

    total = sum(composition.values())
    if total == 0:
        st.warning("请至少声明一种单证。")
    else:
        pretty = "、".join(f"{COMPOSITION_LABELS[t]}×{composition[t]}"
                           for t in COMPOSITION_ORDER if composition[t] > 0)
        st.info(f"本批申报构成：{pretty}，共 {total} 份。")
        if st.button("下一步 · 按申报生成上传槽位 →", type="primary",
                     disabled=total == 0, key="wiz_to_step2"):
            st.session_state["wiz_step"] = 2
            st.rerun()


# ---------------------------------------------------------------- 第2步：逐类型上传


def _load_file(name: str, data: bytes, uploader_type: str) -> dict:
    """逐页提取+自动拆分（与 split_pdf 同源逻辑），结果按内容签名缓存。"""
    pages, warnings, ocr_failed, error = pdf_ingest.extract_pages(
        data, pdf_ingest.MAX_PDF_PAGES)
    entry = {"name": name, "pages": pages, "warnings": warnings,
             "ocr_failed": ocr_failed, "error": error, "uploader": uploader_type}
    if error:
        return entry
    groups, needs, reason = pdf_ingest.detect_doc_groups(pages)
    assignment = []
    for g_idx, g in enumerate(groups):
        assignment.extend([g_idx] * len(g.page_nos))
    entry.update({
        "groups": groups, "needs_confirmation": needs, "reason": reason,
        "auto_assignment": assignment,
    })
    return entry


def _group_results(entry: dict, sig: str) -> list:
    """按当前（可能被用户调整过的）边界与类型，重建各份单据的识别结果。"""
    pages = entry["pages"]
    auto = entry.get("auto_assignment") or ([0] * len(pages))
    auto_count = len(set(auto)) or 1
    # 首次渲染前把份数控件的默认值固化为自动拆分数（否则 number_input 默认取 min=1，
    # 会被误当成"用户改成1份"，触发不必要的逐页确认）
    st.session_state.setdefault(f"wiz_gn::{sig}", auto_count)
    override_count = int(st.session_state.get(f"wiz_gn::{sig}", auto_count))
    needs_manual = bool(entry.get("needs_confirmation")) or override_count != auto_count
    if needs_manual:
        count = max(1, override_count)
        assignment = []
        for page_no in range(1, len(pages) + 1):
            chosen = st.session_state.get(f"wiz_pg::{sig}::{page_no}")
            if chosen is not None:
                # 份数被调小后旧归属值可能越界：收敛到当前范围内
                assignment.append(min(int(chosen) - 1, count - 1))
            else:
                assignment.append(min(auto[page_no - 1], count - 1))
    else:
        count = auto_count
        assignment = auto
    group_types = []
    for g_idx in range(count):
        chosen = st.session_state.get(f"wiz_gt::{sig}::{g_idx}")
        group_types.append(chosen if chosen and chosen != "keep" else None)
    return pdf_ingest.rebuild_groups(
        entry["name"], pages, assignment, set(entry["ocr_failed"]),
        group_types if any(group_types) else None)


def _step2_upload() -> None:
    st.subheader("第2步 · 逐类型上传")
    st.caption("按第1步的申报，每个类型有独立上传位。**一份PDF里含多份同类型单据"
               "（如多车厢的3份运单扫在一个PDF）可直接上传**，系统按单据编号/类型变化"
               "自动拆分；拆分结果需在本页确认，边界不确定时会给出逐页归属调整。")
    composition = _wkey("wiz_composition") or {}
    files_state = st.session_state.setdefault("wiz_files", {})
    uploaded_sigs: set = set()
    rendered_sigs: set = set()
    totals: dict = {}

    for doc_type in COMPOSITION_ORDER:
        declared = int(composition.get(doc_type, 0))
        if declared <= 0:
            continue
        st.markdown(f"#### 📎 {COMPOSITION_LABELS[doc_type]}（申报 {declared} 份）")
        uploads = st.file_uploader(
            f"上传{COMPOSITION_LABELS[doc_type]}PDF（可多份文件）",
            type=["pdf"], accept_multiple_files=True, key=f"wiz_up::{doc_type}")
        type_uploads = uploads or []
        if not type_uploads:
            st.warning(f"该类型申报 {declared} 份，尚未上传任何文件——请补传，"
                       "或回第1步调整申报构成（核验时会按『实收与申报构成不符』显式提示）。")
            totals[doc_type] = 0
            continue

        found_instances = 0
        for up in type_uploads:
            data = up.getvalue()
            sig = _file_sig(up.name, data)
            uploaded_sigs.add(sig)
            if sig in rendered_sigs:
                st.caption(f"⚠️ {up.name} 已在其他类型的上传位处理；同一文件不要重复上传，"
                           "请从当前上传位移除。")
                continue
            if sig not in files_state:
                with st.spinner(f"解析 {up.name} …"):
                    files_state[sig] = _load_file(up.name, data, doc_type)
            entry = files_state[sig]
            rendered_sigs.add(sig)
            with st.expander(f"📄 {up.name}", expanded=True):
                if entry.get("error"):
                    st.error(f"解析失败：{entry['error']}")
                    continue
                results = _group_results(entry, sig)
                found_instances += len(results)
                if entry.get("needs_confirmation"):
                    st.warning(f"⚠️ {entry['reason']}——请在『调整拆分边界』中逐页确认归属后再继续。")
                for g_idx, g in enumerate(results):
                    if g.doc_type != doc_type and not st.session_state.get(f"wiz_gt::{sig}::{g_idx}"):
                        st.warning(
                            f"拆分第{g_idx + 1}份自动识别为"
                            f"「{doc_contract.DOC_TYPE_LABELS.get(g.doc_type, g.doc_type)}」"
                            f"，与当前上传位（{COMPOSITION_LABELS[doc_type]}）不一致，请人工确认类型。")
                    head = f"拆分第{g_idx + 1}份"
                    if g.identity_value:
                        head += f" · 编号 {g.identity_value}"
                    page_range = (f"第{g.pages[0].page_no}页" if len(g.pages) == 1
                                  else f"第{g.pages[0].page_no}-{g.pages[-1].page_no}页")
                    st.markdown(f"**{head}**（{page_range}，"
                                f"{doc_contract.DOC_TYPE_LABELS.get(g.doc_type, g.doc_type)}）")
                # 边界人工确认区（任务书问题一：不确定时让用户确认，系统不擅自猜）
                with st.expander("调整拆分边界 / 纠正类型",
                                 expanded=bool(entry.get("needs_confirmation"))):
                    st.number_input(
                        "这份文件实际包含几份独立单据？", min_value=1,
                        max_value=MAX_INSTANCES, step=1, key=f"wiz_gn::{sig}")
                    count_val = int(st.session_state.get(f"wiz_gn::{sig}", 1))
                    for page_no in range(1, len(entry["pages"]) + 1):
                        page_key = f"wiz_pg::{sig}::{page_no}"
                        stored = st.session_state.get(page_key)
                        if stored is not None and not (1 <= int(stored) <= count_val):
                            # 份数调小后旧归属值越界：清除旧值，回到自动归属
                            del st.session_state[page_key]
                        st.selectbox(
                            f"第{page_no}页归属", options=list(range(1, count_val + 1)),
                            index=min(entry["auto_assignment"][page_no - 1] + 1, count_val) - 1,
                            key=page_key)
                    st.caption("类型纠正：某一份识别类型不对时在此指定（按新类型重新提取字段）。")
                    for g_idx in range(count_val):
                        st.selectbox(
                            f"第{g_idx + 1}份的单证类型",
                            options=["keep"] + COMPOSITION_ORDER,
                            format_func=lambda t: "自动识别（不纠正）" if t == "keep"
                            else COMPOSITION_LABELS.get(t, t),
                            key=f"wiz_gt::{sig}::{g_idx}")
                for w in entry.get("warnings", [])[:4]:
                    st.caption(f"⚠️ {w}")

        totals[doc_type] = found_instances
        if found_instances != declared:
            st.warning(f"该类型申报 {declared} 份，当前识别出 {found_instances} 份——"
                       "可回第1步调整申报，或补传/删除文件。")
        else:
            st.success(f"已识别 {found_instances} 份，与申报一致。")

    _prune_removed_files(uploaded_sigs)
    st.session_state["wiz_totals"] = totals

    if st.button("← 上一步（修改申报构成）", key="wiz_back1"):
        st.session_state["wiz_step"] = 1
        st.rerun()
    if not uploaded_sigs:
        st.info("请先上传单证PDF，再进入下一步。")
    if st.button("下一步 · 逐份核对识别结果 →", type="primary", key="wiz_to_step3",
                 disabled=not uploaded_sigs):
        st.session_state["wiz_step"] = 3
        st.rerun()


# ---------------------------------------------------------------- 第3步：逐份确认


def _collect_wizard_documents() -> list:
    """按申报类型顺序收集全部单据实例，供第3步展示与第4步组装。
    文件归属以其上传位类型为准（entry.uploader）。"""
    composition = _wkey("wiz_composition") or {}
    files_state = st.session_state.get("wiz_files") or {}
    docs = []
    for doc_type in COMPOSITION_ORDER:
        declared = int(composition.get(doc_type, 0))
        if declared <= 0:
            continue
        idx = 0
        for sig, entry in files_state.items():
            if entry.get("uploader") != doc_type or entry.get("error"):
                continue
            for g_idx, g in enumerate(_group_results(entry, sig)):
                idx += 1
                label = f"{SHORT_LABELS.get(doc_type, doc_type)}#{idx}"
                suffix = f"P{g.pages[0].page_no}" if len(g.pages) > 1 or idx > 1 else ""
                doc = g.to_document(doc_id_suffix=suffix)
                docs.append({
                    "doc": doc, "doc_key": f"{sig}::{g_idx}", "label": label,
                    "declared_type": doc_type, "ingest": g,
                    "filename": entry["name"],
                    "page_nos": [p.page_no for p in g.pages],
                })
                if idx >= declared:
                    break
            if idx >= declared:
                break
    return docs


# -*- coding: utf-8 -*-
"""新版向导第3步（四状态UI）内容，供拼接脚本使用。"""

# ============================================================
# 第3步：逐份核对（字段四状态口径，P0任务书A1/P1任务书B2）
# ============================================================

# 用户字段决策状态键：wiz_fm::{doc_key}::{field} -> 决策dict
# 已确认单据标志：wiz_ok::{doc_key}（与第2步的旧键共用命名，含义不变）
_FM_PREFIX = "wiz_fm::"


# 状态 → (前景色, 背景色, 图标)；not_found 是灰色不是红色（P0任务书A1）
_FIELD_VISUAL = {
    doc_contract.FIELD_RECOGNIZED: ("#1B5E20", "#E8F5E9", "✅"),
    doc_contract.FIELD_NEEDS_REVIEW: ("#8D6E00", "#FFF8E1", "⚠️"),
    doc_contract.FIELD_NOT_FOUND: ("#5B6470", "#F1F3F5", "❔"),
    doc_contract.FIELD_BUSINESS_MISSING: ("#B71C1C", "#FFEBEE", "⛔"),
    doc_contract.FIELD_NOT_APPLICABLE: ("#6B7280", "#F1F3F5", "➖"),
}


def _decision_key(doc_key: str, field: str) -> str:
    return f"{_FM_PREFIX}{doc_key}::{field}"


def _get_decision(doc_key: str, field: str) -> dict | None:
    return st.session_state.get(_decision_key(doc_key, field))


def _save_decision(doc_key: str, field: str, status: str, value=None,
                   raw_text: str = "", reason: str = "", page=None,
                   region: str = "—") -> None:
    """落定一个人工字段决策：写会话状态 + 操作留痕（EDIT_FIELD）。
    含原值/新值/修改人/修改时间/原因/是否人工确认（任务书A1）。"""
    import datetime as _dt
    key = _decision_key(doc_key, field)
    prev = st.session_state.get(key)
    decision = {
        "status": status, "value": value, "raw_text": raw_text,
        "reason": reason, "page": page, "region": region,
        "user": st.session_state.get("ceb_user", {}).get("username", "—"),
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    st.session_state[key] = decision
    try:
        import audit
        from webapp import session as web_session
        audit.record(
            web_session.current_username(), audit.EDIT_FIELD, "field",
            f"{doc_key}/{field}",
            before=None if prev is None else {"status": prev.get("status"),
                                              "value": prev.get("value")},
            after={"status": status, "value": value, "reason": reason,
                   "human_confirmed": True})
    except Exception as exc:  # 审计存储不可用不阻断确认流程
        st.caption(f"⚠️ 本次确认留痕失败：{exc}")


def _apply_decisions(item: dict) -> dict:
    """把该单据的全部人工决策合并进 doc 的 fields + field_meta（提交核验前调用）。"""
    doc, doc_key = item["doc"], item["doc_key"]
    prefix = f"{_FM_PREFIX}{doc_key}::"
    decisions = {}
    for key in list(st.session_state.keys()):
        if key.startswith(prefix):
            decisions[key[len(prefix):]] = st.session_state[key]
    if not decisions:
        return doc
    fields = dict(doc.get("fields") or {})
    meta = dict(doc.get("field_meta") or {})
    for field, d in decisions.items():
        status, value = d["status"], d.get("value")
        if status in (doc_contract.FIELD_RECOGNIZED,
                      doc_contract.FIELD_NEEDS_REVIEW):
            if value not in (None, ""):
                fields[field] = value
        elif status in (doc_contract.FIELD_BUSINESS_MISSING,
                        doc_contract.FIELD_NOT_APPLICABLE):
            fields.pop(field, None)
        meta[field] = {
            "status": status, "value": value,
            "raw_text": d.get("raw_text", ""),
            "method": "人工确认", "confidence": 1.0,
            "note": d.get("reason", ""),
            "page": d.get("page"), "region": d.get("region", "—"),
            "confirmed_by": d.get("user"), "confirmed_at": d.get("ts"),
        }
    doc["fields"] = fields
    doc["field_meta"] = meta
    return doc


def _zh(field: str) -> str:
    return doc_contract.FIELD_LABELS_ZH.get(field, field)


def _status_badge(status: str) -> str:
    fg, bg, icon = _FIELD_VISUAL.get(status, ("#6B7280", "#F1F3F5", "•"))
    label = doc_contract.FIELD_STATUS_LABELS.get(status, status)
    return (f'<span style="color:{fg};background:{bg};border-radius:999px;'
            f'padding:2px 10px;font-size:12px;font-weight:700;">{icon} {label}</span>')


def _effective_status(item: dict, field: str) -> tuple:
    """字段当前生效状态：人工决策优先，其次识别证据。
    返回 (status, evidence/decision dict)。"""
    decision = _get_decision(item["doc_key"], field)
    if decision is not None:
        return decision["status"], decision
    doc = item["doc"]
    meta = doc.get("field_meta") or {}
    if field in meta:
        return meta[field].get("status", doc_contract.FIELD_NOT_FOUND), meta[field]
    fields = doc.get("fields") or {}
    return (doc_contract.FIELD_RECOGNIZED if fields.get(field) not in (None, "", [])
            else doc_contract.FIELD_NOT_FOUND), meta.get(field, {})


def _relevant_fields(item: dict) -> tuple:
    """该单据需要展示的字段：必填 ∪ 识别证据字段（排除"不适用"类型字段）。
    返回 (字段列表, 不适用字段列表)。"""
    doc = item["doc"]
    dtype = doc.get("doc_type") or "unknown"
    na = set(doc_contract.NOT_APPLICABLE_FIELDS.get(dtype, []))
    relevant = (set(doc_contract.REQUIRED_FIELDS.get(dtype, []))
                | set((doc.get("field_meta") or {}).keys())
                | set((doc.get("fields") or {}).keys())) - na
    return sorted(relevant), sorted(na)


def _render_field_row(item: dict, field: str, required: bool) -> None:
    """逐字段行：状态/值 + 证据（原文/页码/栏位/方法/置信度）+ 操作按钮。
    默认折叠识别证据，仅待处理字段展开操作区（P1：减少噪音）。"""
    doc_key = item["doc_key"]
    status, source = _effective_status(item, field)
    decision = _get_decision(doc_key, field)
    is_open = status in (doc_contract.FIELD_NEEDS_REVIEW,
                         doc_contract.FIELD_NOT_FOUND)
    value = (decision or source).get("value") if (decision or source) else None
    raw = (decision or source).get("raw_text", "") if (decision or source) else ""
    page = (decision or source).get("page") if (decision or source) else None
    region = (decision or source).get("region", "—") if (decision or source) else "—"
    method = (decision or source).get("method", "") if (decision or source) else ""
    conf = (decision or source).get("confidence") if (decision or source) else None
    note = (decision or source).get("note") or (decision or source).get("reason", "") \
        if (decision or source) else ""

    head_cols = st.columns([3, 2, 2])
    with head_cols[0]:
        st.markdown(f"**{_zh(field)}**")
    with head_cols[1]:
        st.markdown(_status_badge(status), unsafe_allow_html=True)
    with head_cols[2]:
        st.caption(f"值：{value if value not in (None, '') else '—'}")

    # 证据（展开器：默认收起，待处理字段展开）
    with st.expander("识别证据与操作", expanded=is_open):
        st.caption(
            f"原始候选：{raw or '—'}　|　来源：{region}"
            f"{' 第' + str(page) + '页' if page else ''}　|　"
            f"方式：{method or '—'}　|　置信度：{conf if conf is not None else '—'}")
        if note:
            st.caption(f"说明：{note}")
        if decision is not None:
            st.caption(f"已由 {decision.get('user')} 于 {decision.get('ts')} 确认；"
                       "重新提取不会覆盖本字段（如需覆盖请在下方显式操作）。")

        # ---- 操作区 ----
        edit_toggle = st.toggle("✏️ 修改/补录", key=f"edit_t::{doc_key}::{field}")
        if edit_toggle:
            new_text = st.text_input(
                "新值", value="" if value is None else str(value),
                key=f"edit_val::{doc_key}::{field}")
            reason = st.text_input("修改原因", key=f"edit_reason::{doc_key}::{field}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✔ 确认新值（标记已识别）",
                             key=f"save_v::{doc_key}::{field}"):
                    parsed = new_text
                    if field in doc_contract.NUMERIC_FIELDS:
                        parsed = __import__("field_extraction").parse_num(new_text)
                        if parsed is None:
                            st.error("无法解析为数值，请核对输入")
                            return
                    _save_decision(
                        doc_key, field, doc_contract.FIELD_RECOGNIZED,
                        value=parsed, raw_text=raw or new_text,
                        reason=reason, page=page, region=region)
                    st.rerun()
            with c2:
                if st.button("✔ 保存但仍需他人复核",
                             key=f"save_r::{doc_key}::{field}"):
                    _save_decision(
                        doc_key, field, doc_contract.FIELD_NEEDS_REVIEW,
                        value=new_text, raw_text=raw or new_text,
                        reason=reason, page=page, region=region)
                    st.rerun()

        btn_cols = st.columns(3 if required else 2)
        with btn_cols[0]:
            if status != doc_contract.FIELD_RECOGNIZED and st.button(
                    "✅ 识别无误，确认", key=f"ok::{doc_key}::{field}"):
                _save_decision(
                    doc_key, field, doc_contract.FIELD_RECOGNIZED,
                    value=value, raw_text=raw, reason="人工确认识别值",
                    page=page, region=region)
                st.rerun()
        with btn_cols[1]:
            if status != doc_contract.FIELD_NOT_APPLICABLE and st.button(
                    "➖ 标记不适用/未要求", key=f"na::{doc_key}::{field}"):
                _save_decision(
                    doc_key, field, doc_contract.FIELD_NOT_APPLICABLE,
                    value=None, raw_text=raw, reason="该字段对此单据不适用",
                    page=page, region=region)
                st.rerun()
        if required:
            with btn_cols[2]:
                if status != doc_contract.FIELD_BUSINESS_MISSING and st.button(
                        "⛔ 确认业务缺失", key=f"bm::{doc_key}::{field}"):
                    _save_decision(
                        doc_key, field, doc_contract.FIELD_BUSINESS_MISSING,
                        value=None, raw_text=raw,
                        reason="人工确认：单据确无此字段", page=page, region=region)
                    st.rerun()


def _render_document(item: dict) -> tuple:
    """渲染一份单据的摘要卡 + 字段区，返回 (待处理数, 是否确认)。"""
    doc = item["doc"]
    dtype = doc.get("doc_type") or "unknown"
    fields_li, na_fields = _relevant_fields(item)
    required_set = set(doc_contract.REQUIRED_FIELDS.get(dtype, []))

    # 合并人工决策（摘要计数按生效状态）
    open_fields = []
    counts = {doc_contract.FIELD_RECOGNIZED: 0,
              doc_contract.FIELD_NEEDS_REVIEW: 0,
              doc_contract.FIELD_NOT_FOUND: 0,
              doc_contract.FIELD_BUSINESS_MISSING: 0}
    for f in fields_li:
        status, _src = _effective_status(item, f)
        counts[status] = counts.get(status, 0) + 1
        if status in (doc_contract.FIELD_NEEDS_REVIEW,
                      doc_contract.FIELD_NOT_FOUND):
            open_fields.append((f, status))

    title = f"📄 {item['label']} · {doc_contract.DOC_TYPE_LABELS.get(dtype, dtype)}"
    st.markdown(f"##### {title}")
    st.caption(f"来源文件：{item['filename']}（第{'、'.join(map(str, item['page_nos']))}页）")

    # 摘要卡：只看数字就知道要做什么（P1任务书B2-2）
    st.markdown(
        f'<div style="background:#F9FAFB;border:1px solid #E5E7EB;border-radius:12px;'
        f'padding:10px 16px;font-size:13.5px;">'
        f'已识别 <b style="color:#1B5E20;">{counts[doc_contract.FIELD_RECOGNIZED]}</b> 项　'
        f'待您确认 <b style="color:#8D6E00;">{counts[doc_contract.FIELD_NEEDS_REVIEW] + counts[doc_contract.FIELD_NOT_FOUND]}</b> 项　'
        f'业务缺失 <b style="color:#B71C1C;">{counts[doc_contract.FIELD_BUSINESS_MISSING]}</b> 项'
        f'　不适用 {len(na_fields)} 项</div>', unsafe_allow_html=True)

    if counts[doc_contract.FIELD_BUSINESS_MISSING]:
        st.caption("⛔ 已确认业务缺失字段会在核验中判 FAIL，请确认是否需要补办单据。")

    # 字段区：待处理字段默认展开，已识别字段收进展开器
    open_now = [f for f, s in open_fields]
    closed_now = [f for f in fields_li if f not in open_now]
    for f in open_now:
        _render_field_row(item, f, f in required_set)
    if closed_now:
        with st.expander(f"✅ 已识别/已处理字段（{len(closed_now)} 项，点击展开）"):
            for f in closed_now:
                _render_field_row(item, f, f in required_set)

    confirmed = st.checkbox(
        "✅ 本份已核对无误", key=f"wiz_ok::{item['doc_key']}")
    if not confirmed and open_fields:
        names = "、".join(_zh(f) for f, _s in open_fields)
        st.caption(f"还有字段待处理（{names}）——这是识别状态，不代表单据业务不合格。")
    return len(open_fields), confirmed


def _step3_confirm(field_renderer) -> None:
    st.subheader("第3步 · 逐份核对识别结果")
    st.caption("先看每份单据的摘要卡，只处理“待您确认”的少量字段。"
               "❔未找到 = 系统没有识别到（可能需要补录），⛔业务缺失 = 经您确认"
               "单据确实没有该字段——两者不是一回事（识别状态 ≠ 业务缺失）。")
    entries = _collect_wizard_documents()
    if not entries:
        st.warning("没有可核验的单据，请回上一步上传。")
        if st.button("← 上一步", key="wiz_back2_empty"):
            st.session_state["wiz_step"] = 2
            st.rerun()
        st.stop()

    all_confirmed = True
    total_open = 0
    for item in entries:
        open_n, confirmed = _render_document(item)
        total_open += open_n
        all_confirmed = all_confirmed and confirmed
        st.divider()

    if st.button("← 上一步（调整上传）", key="wiz_back2"):
        st.session_state["wiz_step"] = 2
        st.rerun()
    if not all_confirmed:
        if total_open:
            st.caption(f"提示：共 {total_open} 个字段待处理，但仍可逐份勾选“已核对无误”"
                       "继续——未识别字段不会自动判单据不合格。")
        st.caption("请逐份勾选「本份已核对无误」后才能开始核验。")
    if st.button("✅ 确认全部单据，开始核验 →", type="primary",
                 disabled=not all_confirmed, key="wiz_run"):
        # 合并人工决策 → 快照单据（含编辑后字段与四状态证据）
        snapshot = []
        for item in entries:
            doc = _apply_decisions(item)
            snapshot.append(doc)
        st.session_state["wiz_confirmed_docs"] = snapshot
        st.session_state["wiz_step"] = 4
        st.rerun()

# ---------------------------------------------------------------- 组装批次


def build_wizard_batch(documents: list) -> dict:
    """组装含申报构成的批次（数据版本哈希作 batch_id，换文件/编辑即失效缓存）。"""
    dv = doc_contract.data_version(documents)
    composition = {t: int(n) for t, n in (_wkey("wiz_composition") or {}).items() if n}
    return {
        "batch_id": f"pdf_wiz_{dv}",
        "batch_name": "上传PDF核验批次（向导模式）",
        "description": ("由向导式上传流程组装：先声明构成，逐类型上传（多单据PDF自动拆分），"
                        "逐份人工确认后提交核验。"),
        "destination_summary": "—",
        "declared_composition": composition,
        "documents": documents,
    }


# ---------------------------------------------------------------- 主入口


def render_upload_wizard(field_renderer):
    """向导主入口。返回 None（流程未完成）或 (batch, documents, edited_count)。
    field_renderer: app.py 提供的单份单据字段编辑渲染函数（F05契约复用）。"""
    st.session_state.setdefault("wiz_step", 1)
    step = st.session_state["wiz_step"]

    if st.session_state.get("wiz_files"):
        if st.button("↺ 重置向导（清空上传与申报）", key="wiz_reset"):
            reset_wizard()
            st.rerun()

    if step == 1:
        render_wizard_steps(1)
        _step1_composition()
        st.stop()
    elif step == 2:
        render_wizard_steps(2)
        _step2_upload()
        st.stop()
    elif step == 3:
        render_wizard_steps(3)
        _step3_confirm(field_renderer)
        st.stop()

    # ---- 第4步：核验（结果展示由 app.py 主流程渲染） ----
    render_wizard_steps(4)
    docs = st.session_state.get("wiz_confirmed_docs") or []
    if not docs:
        st.session_state["wiz_step"] = 3
        st.rerun()
    if st.button("← 返回上一步（调整上传或修改识别结果）", key="wiz_back3"):
        st.session_state["wiz_step"] = 3
        st.rerun()
    batch = build_wizard_batch(docs)
    return batch, docs, 0    # 编辑已在第3步完成并快照
