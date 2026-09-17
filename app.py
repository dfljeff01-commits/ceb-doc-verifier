# -*- coding: utf-8 -*-
"""
中欧班列单证智能核验 Demo —— Streamlit 前端。

数据流：sample_data/*.json（模拟OCR提取结果）
        -> verification_engine.run_verification()（核验规则全部在引擎中，本文件不做规则判断）
        -> 页面展示（汇总卡片 / 明细表格 / AI修正建议 / PDF导出 / 手动编辑实时复核）
"""

import hashlib
import io
import llm_layer
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

import chat_assistant
import doc_contract
import email_generator
import knowledge_base
import pdf_ingest
from llm_endpoint import resolve_endpoint

from verification_engine import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARNING,
    run_verification,
    sort_results_by_severity,
)
from risk_model import GRADE_META

SAMPLE_DIR = Path(__file__).parent / "sample_data"
API_URL = os.environ.get("VERIFY_API_URL", "http://localhost:8000")


@st.cache_data(ttl=10, show_spinner=False)
def api_healthy() -> bool:
    try:
        return requests.get(f"{API_URL}/health", timeout=1.5).ok
    except Exception:
        return False


def run_verification_effective(batch: dict) -> tuple[dict, str]:
    """优先调用核验API（前后端分离形态）；API不可用时降级为进程内直连，保证演示不中断。"""
    if api_healthy():
        try:
            resp = requests.post(f"{API_URL}/verify", json=batch, timeout=10)
            resp.raise_for_status()
            return resp.json(), "api"
        except Exception:
            pass
    return run_verification(batch), "local"

BATCH_FILES = [
    ("batch_clean.json", "示例批次A：全部通过（干净数据）"),
    ("batch_with_issues.json", "示例批次B：含4类典型问题"),
    ("batch_route_warning.json", "示例批次C：路线合规告警（SMGS运单+土耳其线路）"),
]

SCENE_SENTENCE = (
    "中欧班列欧洲枢纽换装平均耗时38.7小时，单证不一致是主因之一——"
    "本工具在发运前自动核验单证一致性、齐全性与路线合规性，"
    "几秒内发现人工容易漏掉的单证问题，降低边境滞留与退运风险。"
)

STATUS_META = {
    STATUS_PASS: {"label": "✅ PASS", "bg": "#E8F5E9", "fg": "#1B5E20"},
    STATUS_WARNING: {"label": "⚠️ WARNING", "bg": "#FFF8E1", "fg": "#8D6E00"},
    STATUS_FAIL: {"label": "⛔ FAIL", "bg": "#FFEBEE", "fg": "#B71C1C"},
}

# 风险等级配色（唯一事实来源：评分卡/明细/建议/仪表盘统一引用，P1 配色一致性）
GRADE_COLORS = {
    "low": {"fg": "#1B5E20", "bg": "#E8F5E9", "label": "低风险"},
    "medium": {"fg": "#8D6E00", "bg": "#FFF8E1", "label": "中风险"},
    "high": {"fg": "#B71C1C", "bg": "#FFEBEE", "label": "高风险"},
}

# 四步流程（P1 步骤指引）
FLOW_STEPS = ["选择/上传单证", "核对识别结果", "查看核验报告", "生成整改材料"]

APP_CSS = """
<style>
/* 来源选择：radio 卡片化（P0 首屏操作入口） */
div[data-testid="stRadio"] label {
    border: 1.5px solid #E5E7EB; border-radius: 12px; padding: 12px 16px;
    background: #FFFFFF; margin: 2px 0; transition: all .12s ease; cursor: pointer;
}
div[data-testid="stRadio"] label:hover { border-color: #94A3B8; }
div[data-testid="stRadio"] label:has(input:checked) {
    border: 2px solid #0B5394; background: #EFF6FF;
}
/* 来源选择按钮组（与radio等价的备选形态） */
div[data-testid="stButton"] > button { border-radius: 10px; }
/* 步骤指示器 */
.ceb-step { display:flex; gap:6px; margin:2px 0 14px; flex-wrap:wrap; }
.ceb-step span {
    display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    padding:6px 13px; border-radius:999px; border:1px solid #E5E7EB;
    color:#6B7280; background:#F9FAFB; font-weight:600;
}
.ceb-step span.done { color:#1B5E20; background:#E8F5E9; border-color:#A5D6A7; }
.ceb-step span.cur { color:#0B5394; background:#EFF6FF; border-color:#0B5394;
    box-shadow:0 1px 6px #0B539433; }
/* 问题卡片（FAIL/WARNING） */
.ceb-problem { border-radius:12px; padding:12px 16px; margin:10px 0;
    border-left:6px solid; }
.ceb-problem h4 { margin:0 0 6px 0; font-size:15px; }
.ceb-problem .d { font-size:13.5px; color:#374151; margin:2px 0; }
.ceb-problem .s { font-size:13px; color:#374151; background:#FFFFFFCC;
    border-radius:8px; padding:8px 12px; margin-top:8px; }
/* 通用小卡片 */
.ceb-mini { border-radius:10px; padding:10px 14px; text-align:center; }
.ceb-mini .v { font-size:26px; font-weight:800; line-height:1.2; }
.ceb-mini .k { font-size:12px; color:#6B7280; font-weight:600; }
</style>
"""


def render_step_indicator(done_through: int, current: int) -> None:
    """四步流程指示器（P1）：done_through 及之前的步骤显示为已完成，
    current 步高亮为当前所在，其余待办。"""
    spans = []
    for i, name in enumerate(FLOW_STEPS, start=1):
        if i <= done_through and i != current:
            cls, mark = "done", "✓ "
        elif i == current:
            cls, mark = "cur", f"{i}. "
        else:
            cls, mark = "", f"{i}. "
        spans.append(f'<span class="{cls}">{mark}{name}</span>')
    st.markdown(f'<div class="ceb-step">{"".join(spans)}</div>', unsafe_allow_html=True)

# 手动编辑（P1/F05）暴露的字段：key -> (中文名, 控件类型, 附加参数)
# - 全部字段对每份单证渲染（缺失字段可补齐，不再跳过）；
# - route_countries 用列表序列化（顿号/逗号分隔），修复手机端"列表变字符串"；
# - waybill_type 限定受支持枚举（与 F07 契约一致）。
EDITABLE_FIELDS = [
    ("goods_description", "货物描述", "text", None),
    ("total_packages", "件数（箱数）", "int", None),
    ("gross_weight_kg", "毛重（kg）", "float", 10.0),
    ("consignor_name", "发货人名称", "text", None),
    ("consignee_name", "收货人名称", "text", None),
    ("total_amount", "发票总金额", "float", 100.0),
    ("declared_value", "报关申报金额", "float", 100.0),
    ("currency", "币种（如USD）", "text", None),
    ("waybill_no", "运单号", "text", None),
    ("waybill_type", "运单类型", "select",
     ["SMGS国际货协运单", "CIM国际铁路运单", "CIM/SMGS统一运单"]),
    ("container_no", "集装箱号", "text", None),
    ("departure_country", "起运国", "text", None),
    ("destination_country", "运抵国", "text", None),
    ("route_countries", "经停国家（顿号/逗号分隔，保存为列表）", "list", None),
]

_WAYBILL_UNSET = "（不设置/留空）"


@st.cache_data(show_spinner=False)
def load_batch(filename: str) -> dict:
    return json.loads((SAMPLE_DIR / filename).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- UI 组件


def render_risk_dashboard(risk: dict, summary: dict | None = None) -> None:
    """风险评分仪表盘（P0 视觉焦点）：大号环形仪表 + 汇总迷你卡 + 可解释分数构成。
    配色统一引用 GRADE_COLORS。"""
    meta = GRADE_COLORS.get(risk["grade"], GRADE_COLORS["low"])
    color, bg = meta["fg"], meta["bg"]
    score = int(risk["score"])
    deg = max(0, min(100, score)) * 3.6

    mini_cards = ""
    if summary:
        mini_cards = "".join(
            f'<div class="ceb-mini" style="flex:1; min-width:110px; background:{m_bg};'
            f' border:1px solid {m_bd};"><div class="v" style="color:{m_fg};">{m_val}</div>'
            f'<div class="k">{m_label}</div></div>'
            for m_label, m_val, m_fg, m_bg, m_bd in [
                ("总检查项", summary["total"], "#111827", "#F3F4F6", "#E5E7EB"),
                ("通过 PASS", summary["pass"], "#1B5E20", "#E8F5E9", "#A5D6A7"),
                ("警告 WARNING", summary["warning"], "#8D6E00", "#FFF8E1", "#FFE082"),
                ("不合格 FAIL", summary["fail"], "#B71C1C", "#FFEBEE", "#F5B4BD"),
            ])

    chips = "".join(
        f'<div style="display:flex; justify-content:space-between; gap:8px; padding:5px 12px;'
        f' margin:4px 0; border-radius:8px; font-size:13px; background:#F9FAFB; border:1px solid #E5E7EB;">'
        f'<span>{"🔴" if item["status"]=="FAIL" else "🟡"} {item["reason"]}</span>'
        f'<span style="font-weight:800; color:{"#B71C1C" if item["status"]=="FAIL" else "#8D6E00"};">+{item["points"]}</span></div>'
        for item in risk["breakdown"]
    ) or '<div style="font-size:13px; color:#6B7280; padding:4px 0;">无扣分项，各检查全部通过。</div>'

    st.markdown(
        f'<div style="display:flex; gap:22px; margin:6px 0 18px; align-items:stretch; flex-wrap:wrap;">'
        # —— 左：环形仪表（视觉焦点） ——
        f'<div style="flex:0 0 260px; display:flex; justify-content:center; align-items:center;">'
        f'<div style="width:248px; height:248px; border-radius:50%;'
        f' background:conic-gradient({color} {deg}deg, #E9EDF3 {deg}deg);'
        f' display:flex; align-items:center; justify-content:center;'
        f' box-shadow:0 4px 18px {color}2E;">'
        f'<div style="width:192px; height:192px; border-radius:50%; background:#FFFFFF;'
        f' display:flex; flex-direction:column; align-items:center; justify-content:center;">'
        f'<div style="font-size:13px; color:#6B7280; font-weight:600;">单证组风险分</div>'
        f'<div style="font-size:66px; font-weight:800; color:{color}; line-height:1.05;">{score}</div>'
        f'<div style="font-size:14px; font-weight:700; color:{color}; background:{bg};'
        f' border:1px solid {color}44; border-radius:999px; padding:2px 14px; margin-top:6px;">{meta["label"]}</div>'
        f'</div></div></div>'
        # —— 右：汇总迷你卡 + 分数构成 ——
        f'<div style="flex:1; min-width:300px; display:flex; flex-direction:column; gap:10px;">'
        + (f'<div style="display:flex; gap:10px; flex-wrap:wrap;">{mini_cards}</div>' if mini_cards else "")
        + f'<div style="flex:1; border-radius:12px; padding:12px 16px; background:#FFFFFF;'
        f' border:1px solid #E5E7EB;">'
        f'<div style="font-size:13px; color:#6B7280; font-weight:700; margin-bottom:6px;">分数构成（可解释分解）</div>'
        f'{chips}'
        f'<div style="font-size:11px; color:#9CA3AF; margin-top:6px;">风险分级：0-20 低 · 21-50 中 · 51-100 高</div>'
        f'</div></div></div>',
        unsafe_allow_html=True,
    )


def render_summary_cards(summary: dict) -> None:
    """汇总迷你卡（独立渲染形态；主流程已并入仪表盘，保留供其他入口使用）。"""
    cells = "".join(
        f'<div class="ceb-mini" style="flex:1; min-width:120px; background:{bg};'
        f' border:1px solid {bd};"><div class="v" style="color:{fg};">{value}</div>'
        f'<div class="k">{label}</div></div>'
        for label, value, fg, bg, bd in [
            ("总检查项", summary["total"], "#111827", "#F3F4F6", "#E5E7EB"),
            ("通过 PASS", summary["pass"], "#1B5E20", "#E8F5E9", "#A5D6A7"),
            ("警告 WARNING", summary["warning"], "#8D6E00", "#FFF8E1", "#FFE082"),
            ("不合格 FAIL", summary["fail"], "#B71C1C", "#FFEBEE", "#F5B4BD"),
        ]
    )
    st.markdown(
        f'<div style="display:flex; gap:10px; margin:0 0 12px; flex-wrap:wrap;">{cells}</div>',
        unsafe_allow_html=True,
    )


def render_detail_table(results: list) -> None:
    rows = []
    for r in sort_results_by_severity(results):
        rows.append({
            "状态": r["status"],
            "类别": r["category"],
            "检查项": r["check_name"],
            "核验说明": r["detail"] or "—",
            "涉及单证": "、".join(r["involved_docs"]) if r["involved_docs"] else "—",
        })
    df = pd.DataFrame(rows)

    def _highlight(series: pd.Series):
        return [
            f"background-color: {STATUS_META[s]['bg']}; color: {STATUS_META[s]['fg']};"
            "font-weight: 700; text-align: center;"
            for s in series
        ]

    styler = (
        df.style
        .apply(_highlight, subset=["状态"])
        .format({"状态": lambda s: STATUS_META[s]["label"]})
    )
    st.dataframe(
        styler,
        hide_index=True,
        width="stretch",
        column_config={
            "状态": st.column_config.Column(width="small"),
            "类别": st.column_config.Column(width="small"),
            "核验说明": st.column_config.Column(width="large"),
            "涉及单证": st.column_config.Column(width="medium"),
        },
    )


def render_detail_section(results: list) -> None:
    """核验明细区（P0）：FAIL/WARNING 以问题卡片默认展开，PASS 项折叠收起，
    完整明细表保留在二级展开器中——避免用户在一堆绿色PASS里找不到问题。"""
    problems = [r for r in sort_results_by_severity(results) if r["status"] != STATUS_PASS]
    passes = [r for r in results if r["status"] == STATUS_PASS]

    st.markdown("###### 核验明细")
    if problems:
        st.markdown(
            f'<span style="font-size:13px;color:#6B7280;">发现 <b style="color:#B71C1C;">'
            f'{sum(1 for r in problems if r["status"] == STATUS_FAIL)}</b> 项不合格、'
            f'<b style="color:#8D6E00;">{sum(1 for r in problems if r["status"] == STATUS_WARNING)}'
            f'</b> 项警告（已展开）；另有 {len(passes)} 项通过已折叠。</span>',
            unsafe_allow_html=True)
        for r in problems:
            meta = STATUS_META[r["status"]]
            badge = STATUS_META[r["status"]]["label"]
            rule_note = ""
            if r.get("rule_version"):
                rule_note = (f'<div style="font-size:11.5px;color:#9CA3AF;margin-top:6px;">'
                             f'规则版本：{r["rule_version"]}　|　适用范围见说明</div>')
            st.markdown(
                f'<div class="ceb-problem" style="background:{meta["bg"]}; border-color:{meta["fg"]};">'
                f'<h4 style="color:{meta["fg"]};">{badge}　{r["check_name"]}'
                f'<span style="font-weight:400;color:#6B7280;font-size:12px;">　·　{r["category"]}</span></h4>'
                f'<div class="d">{r["detail"]}</div>'
                + (f'<div class="s">💡 <b>建议</b>：{r["suggestion"]}</div>' if r.get("suggestion") else "")
                + rule_note +
                f'</div>',
                unsafe_allow_html=True)
    else:
        st.success(f"本批次 {len(passes)} 项检查全部通过，未发现 FAIL/WARNING 问题。")

    if passes:
        with st.expander(f"✅ 全部通过项（{len(passes)} 项）——点击展开查看"):
            rows = [{"检查项": r["check_name"], "类别": r["category"],
                     "核验说明": r["detail"] or "—"} for r in passes]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={
                             "检查项": st.column_config.Column(width="small"),
                             "核验说明": st.column_config.Column(width="large"),
                         })
    with st.expander("📋 完整核验明细表（含全部状态与涉及单证）", expanded=False):
        render_detail_table(results)


def collect_gray_cases(results: list, documents: list) -> list:
    """收集进入LLM二次判断的灰色地带场景（语义存疑/路线合规WARNING）。"""
    import re as _re
    gray_cases = []
    for r in results:
        if r["status"] != STATUS_WARNING:
            continue
        if r["check_id"] == "CONS-001":
            for sim in r.get("similarities") or []:
                if sim.get("grade") == "suspect":
                    m = _re.search(r"基准「(.+?)」", r["detail"])
                    gray_cases.append(("CONS-001", {
                        "baseline": m.group(1) if m else "",
                        "other": sim.get("text") or "",
                        "score": sim.get("score", 0),
                        "doc_name": "、".join(sim.get("docs") or []),
                    }))
        elif r["check_id"] == "ROUTE-001":
            wb = next((d for d in documents if d.get("doc_type") == "railway_waybill"), None)
            if wb:
                gray_cases.append(("ROUTE-001", {
                    "waybill_type": (wb.get("fields") or {}).get("waybill_type", ""),
                    "route": (wb.get("fields") or {}).get("route_countries") or [],
                }))
    return gray_cases


def render_ai_reasoning(results: list, documents: list) -> None:
    """AI协同推理区块（升级任务书·方向三）：对灰色地带WARNING给出LLM二次判断与解释。"""
    gray_cases = collect_gray_cases(results, documents)
    if not gray_cases:
        return

    st.subheader("🤖 AI 推理说明（规则筛选 → LLM二次判断）")
    for check_id, context in gray_cases:
        opinion = llm_layer.get_ai_opinion(check_id, context)
        if opinion:
            source_badge = {"live": "🟢 实时调用", "preset": "📦 预置推理结果（离线生成）"}.get(
                opinion.get("source"), opinion.get("source", ""))
            st.info(
                f"**场景**：{opinion.get('scenario') or context}\n\n"
                f"**AI判断**：{opinion.get('verdict')}（置信度：{opinion.get('confidence')}）\n\n"
                f"**推理过程**：{opinion.get('explanation')}\n\n"
                f"**建议动作**：{opinion.get('action')}\n\n"
                f"<span style='font-size:11px; color:#6B7280;'>来源：{source_badge} · {opinion.get('llm', '')}"
                f" —— demo为保证稳定性采用预置结果，配置 ARK_API_KEY 后未命中的场景将实时调用</span>",
                unsafe_allow_html=True,
            )
        else:
            st.caption(f"⚠️ 场景 {context} 暂无预置推理结果；配置 ARK_API_KEY 环境变量后可实时调用大模型分析。")


def render_kb_basis(results: list) -> None:
    """合规依据展示（RAG-lite）：对 FAIL/WARNING 项做知识库向量检索并引用条文。"""
    problems = [r for r in results if r["status"] != STATUS_PASS]
    if not problems:
        return
    st.subheader("📖 合规依据（知识库向量检索）")
    for r in sort_results_by_severity(problems):
        basis = knowledge_base.basis_for_result(r)
        if not basis:
            continue
        with st.expander(f"依据 · {r['check_name']}（{r['status']}）"):
            for hit in basis:
                e = hit["entry"]
                st.markdown(
                    f"**{e['id']}｜{e['title']}**（相关度 {hit['score']:.2f}）\n\n"
                    f"> {e['text']}")
            st.caption("🔍 " + knowledge_base.KB_DISCLAIMER
                       + " 检索为字符n-gram向量余弦相似度（与语义比对同一套技术）。")


def render_email_generator(verification: dict, documents: list) -> None:
    """AI整改邮件（原生AI功能2）：LLM实时生成，无Key时降级为数据填充的结构化草稿。
    缓存按数据版本（内容哈希）关联——字段编辑/换文件后旧草稿失效，需重新生成（修复 F09）。"""
    st.subheader("📧 整改邮件（AI代拟给供应商/货代）")
    problems = [r for r in verification["results"] if r["status"] != STATUS_PASS]
    if not problems:
        st.success("本批次全部检查通过，无需生成整改邮件。")
        return

    batch_id = str(verification.get("batch_id"))
    dv = doc_contract.data_version(documents)
    cache_key = f"email::{batch_id}::{dv}"
    generated_dv = st.session_state.get(f"email_generated_dv::{batch_id}")
    if generated_dv is not None and generated_dv != dv:
        st.warning("⚠️ 单证数据已变化（字段被编辑或文件已更换），此前生成的邮件基于旧数据，"
                   "请点击下方按钮重新生成。")
    if st.button("✉️ 生成中英双语整改邮件草稿", type="primary"):
        with st.spinner("AI正在起草邮件…"):
            st.session_state[cache_key] = email_generator.generate_email(verification, documents)
            st.session_state[f"email_generated_dv::{batch_id}"] = dv
    result = st.session_state.get(cache_key)
    if not result:
        st.caption("草稿基于本批次真实的 FAIL/WARNING 明细生成；生成后可直接编辑文本，"
                   "下载内容为编辑后的最新文本。")
        return

    if result["mode"] == "not_needed":
        st.success(result["message"])
        return
    mode_badge = {"live": "🟢 LLM实时生成",
                  "offline_template": "📦 离线模板模式：草稿由本批次核验明细数据填充生成，"
                                      "配置 API Key 后由LLM生成完整商务邮件"}.get(result["mode"], result["mode"])
    st.caption(f"生成方式：{mode_badge}　|　数据版本 {dv}　|　发送前请人工审阅编辑")
    tab_zh, tab_en = st.tabs(["中文版", "English"])
    with tab_zh:
        # 下载读取当前编辑后的实际内容（修复 F09：下载不再使用缓存的原始文本）
        zh_text = st.text_area("邮件草稿（可编辑）", value=result["zh"], height=380,
                               key=f"{cache_key}::zh")
        st.download_button("下载中文版 (.txt)", data=zh_text.encode("utf-8"),
                           file_name=f"整改邮件_中文_{dv}.txt", width="stretch")
    with tab_en:
        en_source = result.get("en") or "（English version unavailable）"
        en_text = st.text_area("Email draft (editable)", value=en_source, height=380,
                               key=f"{cache_key}::en")
        st.download_button("Download English (.txt)", data=en_text.encode("utf-8"),
                           file_name=f"remediation_email_en_{dv}.txt", width="stretch")


def render_chat_assistant(verification: dict, documents: list) -> None:
    """对话式核验助手（原生AI功能1）：LLM+工具调用，数值假设会真实重跑风险模型。
    会话按数据版本关联——字段编辑/换文件后旧对话不带入新数据，并明确提示（修复 F09）。"""
    st.subheader("💬 向AI追问（对话式核验助手）")
    st.caption("ℹ️ 回答由AI实时生成，可能存在误差，请以核验报告明细为准。"
               "数值假设类问题会调用核验引擎真实重算并经计算验证，不是模型猜测。")

    if resolve_endpoint() is None:
        st.info("⚠️ 对话式核验助手需配置 LLM API Key 启用（环境变量 ARK_API_KEY，"
                "或 GLM_API_KEY / BIGMODEL_API_KEY）。为保证诚实性，本功能不提供预置问答。",
                icon="🔑")
        return

    batch_id = str(verification.get("batch_id"))
    dv = doc_contract.data_version(documents)
    hist_key = f"chat::{batch_id}::{dv}"
    prev_dv = st.session_state.get(f"chat_dv::{batch_id}")
    if prev_dv is not None and prev_dv != dv and st.session_state.get(f"chat::{batch_id}::{prev_dv}"):
        st.warning("⚠️ 单证数据已变化（字段被编辑或文件已更换），此前对话基于旧数据；"
                   "以下为新数据的全新会话，重要结论请重新提问核对。")
    st.session_state[f"chat_dv::{batch_id}"] = dv
    history = st.session_state.setdefault(hist_key, [])
    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    question = st.chat_input("例如：为什么这批风险打100分？／如果箱数改成480，风险会变成多少？／一句话总结核心问题")
    if not question:
        return
    history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("AI思考中…"):
            reply = chat_assistant.answer_question(
                question, verification, documents, history=history[:-1])
        st.markdown(reply["answer"])
        if reply.get("tool_trace"):
            recalcs = [t for t in reply["tool_trace"] if t["tool"] == "simulate_field_change"]
            if recalcs:
                last = recalcs[-1]["result"]
                st.caption(f"🔧 已调用核验引擎真实重算：新风险分 {last['new_risk_score']}"
                           f"（{last['new_risk_grade']}），FAIL {last['new_summary']['fail']} 项")
        history.append({"role": "assistant", "content": reply["answer"]})


def build_pdf(batch: dict, verification: dict, edited_count: int,
              documents: list | None = None) -> bytes:
    """用 reportlab 生成与页面内容一致的 PDF 报告（中文用内置 STSong-Light 字体）。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    FONT = "STSong-Light"

    # 数据版本+规则版本入报告（F09/F07：导出材料可追溯到当前数据与规则口径）
    dv = doc_contract.data_version(documents) if documents else "-"
    rule_version = verification.get("rule_version", "")

    title_style = ParagraphStyle("t", fontName=FONT, fontSize=16, leading=22, spaceAfter=4)
    normal_style = ParagraphStyle("n", fontName=FONT, fontSize=9.5, leading=14)
    small_style = ParagraphStyle("s", fontName=FONT, fontSize=8, leading=12,
                                 textColor=colors.HexColor("#6B7280"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            title="中欧班列单证核验报告")
    story = [
        Paragraph("中欧班列单证智能核验报告（Demo）",
                  ParagraphStyle("h", parent=title_style, fontSize=17)),
        Paragraph(f"批次：{verification['batch_name']}　|　"
                  f"路径：{batch.get('destination_summary', '—')}　|　"
                  f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
                  + (f"　|　⚠️ 含手动修改字段 {edited_count} 处" if edited_count else "")
                  + f"　|　数据版本 {dv}"
                  + (f"　|　规则版本 {rule_version}" if rule_version else ""),
                  normal_style),
        Spacer(1, 6),
        Paragraph(f"背景：{SCENE_SENTENCE}", small_style),
        Spacer(1, 10),
    ]

    s = verification["summary"]
    risk = verification.get("risk", {})
    risk_line = f"单证组风险分：{risk.get('score', 0)}/100（{risk.get('grade_label', '低风险')}）"
    breakdown = risk.get("breakdown", [])
    if breakdown:
        risk_line += " —— 构成：" + "，".join(
            f"{item['reason']} +{item['points']}" for item in breakdown)
    story.append(Paragraph(
        f"<b>汇总：</b>共 {s['total']} 项检查 —— 通过 {s['pass']}，警告 {s['warning']}，不合格 {s['fail']}",
        ParagraphStyle("sum", parent=normal_style, fontSize=11, leading=16),
    ))
    story.append(Paragraph(
        f"<b>{risk_line}</b>",
        ParagraphStyle("risk", parent=normal_style, fontSize=11, leading=16,
                       textColor=colors.HexColor(GRADE_META.get(risk.get("grade", "low"), {}).get("color", "#111827"))),
    ))
    story.append(Spacer(1, 8))

    header = [Paragraph("状态", ParagraphStyle("h1", parent=normal_style, textColor=colors.white)),
              Paragraph("检查项", ParagraphStyle("h2", parent=normal_style, textColor=colors.white)),
              Paragraph("核验说明", ParagraphStyle("h3", parent=normal_style, textColor=colors.white)),
              Paragraph("AI修正建议", ParagraphStyle("h4", parent=normal_style, textColor=colors.white))]
    data = [header]
    status_fills = []
    # PDF 内置中文字体无 emoji 字形，状态列使用纯文字，颜色由底色表达
    pdf_status_label = {STATUS_PASS: "PASS", STATUS_WARNING: "WARNING", STATUS_FAIL: "FAIL"}
    for i, r in enumerate(sort_results_by_severity(verification["results"]), start=1):
        meta = STATUS_META[r["status"]]
        status_fills.append((i, meta["bg"], meta["fg"]))
        data.append([
            Paragraph(pdf_status_label[r["status"]], normal_style),
            Paragraph(r["check_name"], normal_style),
            Paragraph(r["detail"] or "—", normal_style),
            Paragraph(r.get("suggestion") or "—", normal_style),
        ])
    table = Table(data, colWidths=[2.2 * cm, 3.8 * cm, 6.0 * cm, 6.0 * cm],
                  repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_idx, bg, _fg in status_fills:
        style_cmds.append(("BACKGROUND", (0, row_idx), (0, row_idx), colors.HexColor(bg)))
    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    story.append(Spacer(1, 10))

    if verification["suggestions"]:
        story.append(Paragraph("AI 修正建议汇总", ParagraphStyle("s2", parent=normal_style,
                                                              fontSize=12, spaceAfter=4)))
        for sug in verification["suggestions"]:
            story.append(Paragraph("• " + sug,
                                   ParagraphStyle("li", parent=normal_style, leftIndent=10,
                                                  spaceAfter=3)))
        story.append(Spacer(1, 8))

    story.append(Paragraph(
        "免责声明：本工具为竞赛演示 Demo，单证数据为模拟OCR结果，核验规则为简化规则集，"
        "输出不构成任何商业或法律依据。未来可接入真实OCR与AI大模型实现单证自动识别与解释，"
        "并进一步扩展在途提单融资额度测算等供应链金融能力。",
        small_style))

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------- 手动编辑（P1）


def _parse_numeric_text(text: str, kind: str):
    """编辑框文本按字段类型解析（F05；契约实现见 doc_contract.parse_edited_number）。"""
    return doc_contract.parse_edited_number(text, as_int=(kind == "int")), True


def _serialize_edited_value(kind: str, text: str):
    """按字段类型序列化编辑值；返回 None 表示该字段应删除/不设置。"""
    if kind == "list":
        return doc_contract.split_route_text(text)
    return text.strip() or None


def _value_changed(new_value, original) -> bool:
    """编辑改动判定：数值按数值比较（int 12300 与 float 123.0 视为相同，
    避免 number_input 把整数字段误报"已修改"）；其余按内容比较。"""
    def _is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if _is_num(new_value) and _is_num(original):
        return float(new_value) != float(original)
    return doc_contract.canonical_json(new_value) != doc_contract.canonical_json(original)


def collect_edited_documents(batch: dict) -> tuple[list, int]:
    """渲染编辑控件并返回合并后的单证列表 + 修改字段数。
    F05：编辑表单来自契约字段定义——缺失字段也渲染控件（可补齐）；
    按字段类型序列化（数值保持数值、路线保持字符串列表）。
    未暴露为可编辑字段的原始字段（发票号/日期等）原样保留，不丢失。"""
    batch_id = batch["batch_id"]
    edited = 0
    documents = []
    for doc in batch["documents"]:
        original_fields = dict(doc.get("fields") or {})
        fields = dict(original_fields)          # 保留全部原始字段（含非编辑字段）
        with st.expander(f"📄 {_doc_title(doc)}（{doc.get('doc_id', '')}）"):
            for key, label, kind, extra in EDITABLE_FIELDS:
                widget_key = f"fld::{batch_id}::{doc['doc_id']}::{key}"
                has_original = key in original_fields
                original = original_fields.get(key)
                new_value = None
                included = False

                if kind in ("int", "float"):
                    try:
                        seed = float(original) if has_original else 0.0
                        numeric_seed = seed
                    except (TypeError, ValueError):
                        numeric_seed = None
                    if numeric_seed is None:
                        # 原值不是纯数字（如"12300kg"）：退化为文本框，解析交给引擎口径
                        text = st.text_input(label, value="" if original is None else str(original),
                                             key=widget_key)
                        new_value = doc_contract.parse_edited_number(text, as_int=(kind == "int"))
                        included = new_value is not None
                    else:
                        step = extra or 1.0
                        value = st.number_input(label, value=numeric_seed,
                                                min_value=0.0, step=step, key=widget_key)
                        new_value = value if kind == "float" else round(float(value), 2)
                        if kind == "int" and float(new_value).is_integer():
                            new_value = int(new_value)   # 整数字段保持int类型（不产生12300.0）
                        included = has_original or new_value not in (0, 0.0)
                elif kind == "select":
                    options = list(extra or [])
                    if has_original and str(original) not in options:
                        options = [str(original)] + options
                    if has_original:
                        value = st.selectbox(label, options,
                                             index=options.index(str(original)), key=widget_key)
                        new_value, included = value, True
                    else:
                        value = st.selectbox(label, [_WAYBILL_UNSET] + options,
                                             index=0, key=widget_key)
                        new_value = None if value == _WAYBILL_UNSET else value
                        included = new_value is not None
                elif kind == "list":
                    display = "、".join(str(x) for x in original) if isinstance(original, list) \
                        else ("" if original is None else str(original))
                    text = st.text_input(label, value=display, key=widget_key)
                    new_value = _serialize_edited_value("list", text)
                    included = new_value is not None
                else:   # text
                    text = st.text_input(label, value="" if original is None else str(original),
                                         key=widget_key)
                    new_value = _serialize_edited_value("text", text)
                    included = new_value is not None

                if included:
                    fields[key] = new_value
                elif has_original:
                    fields.pop(key, None)     # 用户清空该字段 → 显式删除
                if _value_changed(fields.get(key), original if has_original else None):
                    edited += 1
        documents.append({**doc, "fields": fields})
    return documents, edited


def _doc_title(doc: dict) -> str:
    return doc.get("title") or doc.get("doc_type", "单证")


def reset_edits(batch: dict) -> None:
    prefix = f"fld::{batch['batch_id']}::"
    for key in list(st.session_state.keys()):
        if key.startswith(prefix):
            del st.session_state[key]
    st.rerun()


# ---------------------------------------------------------------- 页面主体

st.set_page_config(
    page_title="中欧班列单证智能核验",
    page_icon="🚂",
    layout="wide",
)
st.markdown(APP_CSS, unsafe_allow_html=True)

st.title("🚂 中欧班列单证智能核验")
st.caption("AI + 多式联运 · 单证交叉核验工具（竞赛Demo）")
st.info(SCENE_SENTENCE, icon="🎯")

# ---------------- 第一步：选择单证来源（P0 卡片式入口） ----------------
SOURCE_SAMPLE = "📁 示例批次（3组预置模拟数据，一键加载，推荐先看）"
SOURCE_UPLOAD = "📎 上传PDF单证（自动判型/OCR，可人工纠正）"
source_mode = st.radio(
    "第一步 · 选择单证来源",
    [SOURCE_SAMPLE, SOURCE_UPLOAD],
    index=0,
    key="source_mode",
    label_visibility="collapsed",
    horizontal=True,
)

# 流程进度（P1 步骤指示：来自会话状态，首屏默认第1步）
_mode_upload = source_mode.startswith("📎")
_uploaded = bool(st.session_state.get("pdf_files"))
_verified = bool(st.session_state.get("pdf_verified"))
_materials_used = any(
    k.startswith(("email_generated_dv::", "chat::")) and v
    for k, v in st.session_state.items())
if not _mode_upload:
    _done, _cur = 2, 3
elif not _uploaded:
    _done, _cur = 0, 1
elif not _verified:
    _done, _cur = 1, 2
else:
    _done, _cur = 2, 3
if _materials_used and _done < 3:
    _done, _cur = 3, 4
render_step_indicator(_done, _cur)

batch = None
if not _mode_upload:
    labels = [label for _, label in BATCH_FILES]
    batch_ids = [fn.removesuffix(".json") for fn, _ in BATCH_FILES]
    # 支持 URL 参数直达批次（如 ?batch=batch_with_issues），便于分享与演示
    default_index = (
        batch_ids.index(st.query_params["batch"])
        if "batch" in st.query_params and st.query_params["batch"] in batch_ids
        else 0
    )
    chosen_col, desc_col = st.columns([1, 2])
    with chosen_col:
        chosen_index = labels.index(
            st.selectbox("选择示例批次（模拟上传+OCR提取完成）", labels, index=default_index)
        )
    with desc_col:
        batch = load_batch(BATCH_FILES[chosen_index][0])
        st.markdown(
            f'<div style="border:1px solid #E5E7EB; border-radius:12px; padding:10px 16px;'
            f' background:#F9FAFB; font-size:13px; color:#374151;">'
            f'<b>{batch.get("batch_name", "")}</b>　{batch.get("description", "")}'
            f'<br><span style="color:#6B7280;">🚉 运输路径：{batch.get("destination_summary", "—")}'
            f'　|　📎 已提取单证：'
            f'{"、".join(d.get("title", d.get("doc_type", "")) for d in batch["documents"])}</span></div>',
            unsafe_allow_html=True)
else:
    st.caption("上传发票 / 装箱单 / 铁路运单 / 出口报关单 PDF（最多4份）。"
               "系统自动判型（文本型直取文字层，扫描页OCR），识别结果可人工纠正后核验。")
    if not pdf_ingest.ocr_available():
        st.warning("未检测到本机 tesseract OCR，扫描型PDF将无法识别文字层以外的内容"
                   "（文本型PDF不受影响）。安装方法见 README。")


# ---------------- PDF 上传模式辅助 ----------------


def _file_sig(up_file) -> str:
    """上传文件签名（修复 F09）：文件名 + 内容SHA256。
    同名同大小但内容不同的文件签名不同，不会误命中旧解析缓存。"""
    data = None
    getter = getattr(up_file, "getvalue", None)
    if callable(getter):
        try:
            data = getter()
        except Exception:
            data = None
    if data is not None:
        return f"{up_file.name}:{hashlib.sha256(data).hexdigest()[:16]}"
    return f"{up_file.name}:{getattr(up_file, 'size', '?')}"


def ingest_uploaded_files(uploads: list) -> list:
    """处理上传的PDF（带session_state缓存，避免重复OCR）；返回与上传列表对齐的 IngestResult。"""
    sig_now = [_file_sig(u) for u in uploads]
    cached = st.session_state.get("pdf_ingest")
    cached_sigs = st.session_state.get("pdf_ingest_sigs")
    if cached is not None and cached_sigs == sig_now:
        return cached

    import pdf_ingest
    results = []
    progress = st.progress(0.0, "正在解析上传的PDF单证…")
    for i, up in enumerate(uploads):
        data = up.getvalue()
        r = pdf_ingest.process_pdf(data, up.name)
        results.append(r)
        progress.progress((i + 1) / len(uploads))
    progress.empty()
    st.session_state["pdf_ingest"] = results
    st.session_state["pdf_ingest_sigs"] = sig_now
    st.session_state.pop("pdf_verified", None)   # 新文件需重新点击开始核验
    return results


def ingest_with_type(results: list) -> list:
    """应用人工纠正的单证类型（类型变了则按新类型重新提取字段）。"""
    import pdf_ingest
    corrected = []
    for r in results:
        chosen = st.session_state.get(f"pdf_type::{r.filename}", r.doc_type)
        if chosen != r.doc_type:
            texts = [p.text for p in r.pages]
            fields, conf, warnings = pdf_ingest.extract_fields(chosen, texts)
            r2 = pdf_ingest.IngestResult(
                filename=r.filename, doc_type=chosen, type_score=r.type_score,
                pages=r.pages, fields=fields, field_confidence=conf,
                elapsed_seconds=r.elapsed_seconds, warnings=warnings,
                pages_with_ocr_failure=r.pages_with_ocr_failure)
            corrected.append(r2)
        else:
            corrected.append(r)
    return corrected


# 侧边栏：功能入口与使用指引（P1：入口整理，不再承担来源选择主交互）
with st.sidebar:
    st.header("🧭 使用指引")
    st.markdown(
        "1️⃣ 选择/上传单证　→　2️⃣ 核对识别结果　→　"
        "3️⃣ 查看核验报告　→　4️⃣ 生成整改材料\n\n"
        "页面顶部会同步显示当前所在步骤。")
    st.divider()
    st.header("🔗 功能入口")
    st.markdown(
        f"- [核验API文档（Swagger）]({API_URL}/docs)\n"
        f"- [API健康检查]({API_URL}/health)")
    st.caption("📱 Android App 见项目 mobile_app/INSTALL.md（扫码分发：python serve_apk.py）；"
               "网页端建议 PC 浏览。")
    st.divider()
    st.caption("Demo 版 v1.4 · 规则引擎 + 风险评分 + 语义比对+关键实体守卫 + LLM协同（审查整改后）")

# ---------------- 非示例模式的PDF提取区 ----------------

pdf_verified = None
if source_mode.startswith("📎"):
    uploads = st.file_uploader("上传PDF单证（可多选）", type=["pdf"],
                               accept_multiple_files=True, key="pdf_files")
    if not uploads:
        st.session_state.pop("pdf_verified", None)
    if not uploads:
        st.info("👆 请先上传PDF单证文件。可使用 `sample_pdfs/` 目录下的示例文件（含文本型/扫描型/混合型）。")
        st.stop()

    with st.spinner("解析PDF中…"):
        results = ingest_uploaded_files(uploads)
    results = ingest_with_type(results)

    st.subheader("📥 提取结果预览（模拟OCR → 结构化字段，可纠正）")
    total_ocr = sum(p.ocr_seconds for r in results for p in r.pages)
    st.caption(f"耗时：文本页直取（毫秒级）；扫描页OCR合计 {total_ocr:.1f}s"
               f"（判型阈值：每页可提取字符数 <{pdf_ingest.TEXT_PAGE_MIN_CHARS} 判为扫描页）")

    for r in results:
        label = f"📄 {r.filename}"
        with st.expander(label, expanded=True):
            if r.error:
                st.error(f"解析失败：{r.error}")
                continue
            if r.warnings:
                for w in r.warnings:
                    st.warning(w)
            if r.pages_with_ocr_failure:
                st.error(f"第 {'、'.join(map(str, r.pages_with_ocr_failure))} 页OCR失败，"
                         f"该页内容缺失，核验结论不可信，请处理后重新上传或人工补齐。")
            tcols = st.columns([2, 1])
            with tcols[0]:
                st.caption(f"单证类型：自动判断（关键词得分 {r.type_score}）"
                           + ("　⚠️ 低置信度，请人工确认" if r.type_score < 2 else ""))
                type_options = ["invoice", "packing_list", "railway_waybill",
                                "export_customs_declaration", "certificate_of_origin", "unknown"]
                type_names = {"invoice": "商业发票", "packing_list": "装箱单",
                              "railway_waybill": "铁路运单", "export_customs_declaration": "出口报关单",
                              "certificate_of_origin": "原产地证书",
                              "unknown": "无法识别（需人工指定）"}
                st.selectbox("单证类型（可纠正）", type_options,
                             format_func=lambda t: type_names[t],
                             key=f"pdf_type::{r.filename}",
                             index=type_options.index(
                                 st.session_state.get(f"pdf_type::{r.filename}", r.doc_type)))
            with tcols[1]:
                page_info = "，".join(
                    f"P{p.page_no}:{'文本' if p.mode == 'text' else f'OCR {p.ocr_seconds:.1f}s'}"
                    for p in r.pages)
                st.caption(f"页面判型：{page_info}")

            rows = ["| 字段 | 提取值 | 置信度 |", "|---|---|---|"]
            for k, v in sorted(r.field_confidence.items()):
                mark = {"high": "✅ 高（关键词命中）",
                        "review": "⚠️ 存疑（规则命中但口径待确认）",
                        "missing": "⛔ 提取失败/需人工核对"}.get(v, v)
                rows.append(f"| `{k}` | {r.fields.get(k, '—')} | {mark} |")
            st.markdown("\n".join(rows))
            missing = [k for k, v in r.field_confidence.items() if v == "missing"]
            review = [k for k, v in r.field_confidence.items() if v == "review"]
            if missing:
                st.warning("以下必需字段未能提取，请稍后在『单证字段』区人工补齐后再核验："
                           + "、".join(missing))
            if review:
                st.warning("以下字段提取值口径存疑（单位/格式待确认），请人工复核："
                           + "、".join(review))

    if any(r.error for r in results):
        st.caption("⚠️ 存在解析失败的文件，已自动跳过。")

    if st.button("▶️ 开始核验（使用上方确认后的提取结果）", type="primary"):
        st.session_state["pdf_verified"] = True
    if not st.session_state.get("pdf_verified"):
        st.info("确认提取结果无误后，点击『开始核验』。")
        st.stop()

    from pdf_ingest import build_batch
    batch = build_batch(results)
    if not batch["documents"]:
        st.error("没有可核验的单证（全部解析失败）。")
        st.stop()

st.subheader("2️⃣ 单证字段（提取结果，可手动修改实时复核）")
edited_documents, edited_count = collect_edited_documents(batch)
c1, c2, _ = st.columns([1, 2, 3])
with c1:
    if st.button("↺ 重置本批次修改", disabled=edited_count == 0):
        reset_edits(batch)
with c2:
    if edited_count:
        st.markdown(f"<span style='color:#B26A00;font-weight:600;'>"
                    f"✍️ 已手动修改 {edited_count} 个字段，以下核验结果已实时更新</span>",
                    unsafe_allow_html=True)

# 核验（规则全部来自 verification_engine，本文件只做展示；优先走API，不可用时直连）
effective_batch = {**batch, "documents": edited_documents}
with st.spinner("核验计算中…"):
    verification, verify_mode = run_verification_effective(effective_batch)
summary = verification["summary"]
st.caption("🔌 核验通道：" + (
    f"FastAPI 服务（{API_URL}）—— 前后端分离形态" if verify_mode == "api"
    else "进程内直连（未检测到核验API服务，启动 `uvicorn api:app --port 8000` 可切换为API形态）"))

# 核验结果汇总（P0：环形风险仪表为全页视觉焦点；导出按钮放标题行右侧）
head_left, head_right = st.columns([4, 1])
with head_left:
    st.subheader("3️⃣ 核验结果汇总")
with head_right:
    pdf_bytes = build_pdf(batch, verification, edited_count, edited_documents)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    st.download_button(
        "⬇️ 导出PDF报告",
        data=pdf_bytes,
        file_name=f"核验报告_{batch['batch_id']}_{ts}.pdf",
        mime="application/pdf",
        width="stretch",
        type="primary" if summary["fail"] or summary["warning"] else "secondary",
    )
render_risk_dashboard(verification["risk"], summary)

# 核验明细（P0：FAIL/WARNING 默认展开，PASS 折叠）
render_detail_section(verification["results"])
render_kb_basis(verification["results"])
render_ai_reasoning(verification["results"], edited_documents)

# 第四步：生成整改材料（P1 分组导航）
st.markdown("###### 4️⃣ 生成整改材料")
render_email_generator(verification, edited_documents)
# 对话助手收进默认折叠的展开器：chat_input 挂载时会自动聚焦并把页面滚到底部，
# 折叠后首屏保持在顶部；用户点开时再聚焦正合适。
with st.expander("💬 向AI追问（对话式核验助手）——点击展开", expanded=False):
    render_chat_assistant(verification, edited_documents)

# 原始单证数据
with st.expander("🔍 查看原始单证数据（模拟OCR提取结果JSON）"):
    st.json(edited_documents)

st.divider()
st.caption(
    "本工具为竞赛演示 Demo：OCR 为模拟数据（预置JSON），核验规则为简化规则集，非生产系统；"
    "后续可替换真实OCR接口与大模型字段抽取，并扩展在途提单融资额度测算等供应链金融能力。"
)
