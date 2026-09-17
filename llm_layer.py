# -*- coding: utf-8 -*-
"""
LLM 协同推理层（升级任务书·方向三）。

定位：规则引擎做筛选 → LLM 对灰色地带做复杂判断与自然语言解释。
触发条件：语义存疑（描述相似度落入 0.6-0.85）或路线合规 WARNING，
即"中低风险但需要第二意见"的边界案例。

诚实性说明（务必与README口径一致）：
  - demo 环境为保证现场演示稳定，采用 **预置推理结果**（llm_presets.json），
    文本由大模型离线生成；
  - 实时调用路径已实现（call_live_llm，火山方舟 OpenAI 兼容接口）：
    配置环境变量 ARK_API_KEY（可选 ARK_MODEL / ARK_BASE_URL）后，
    未命中预置的场景会实时调用大模型；
  - 每条 AI 意见都带 source 标记（"live"=实时调用 / "preset"=预置结果 /
    "unavailable"=无预置且无法实时调用），页面如实展示来源。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PRESET_PATH = Path(__file__).parent / "llm_presets.json"

ARK_BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seed-1-6-250615")


def _signature(check_id: str, payload: str) -> str:
    return hashlib.sha1(f"{check_id}|{payload}".encode("utf-8")).hexdigest()[:16]


def suspect_signature(baseline_text: str, other_text: str) -> str:
    """语义存疑场景的签名：与两段文本内容绑定（与顺序无关）。"""
    pair = "||".join(sorted([str(baseline_text).strip(), str(other_text).strip()]))
    return _signature("CONS-001", pair)


def route_signature(waybill_type: str, route_countries: list) -> str:
    return _signature("ROUTE-001", f"{waybill_type}|{','.join(route_countries or [])}")


def _load_presets() -> dict:
    try:
        return json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_prompt(check_id: str, context: dict) -> str:
    if check_id == "CONS-001":
        return (
            "你是中欧班列单证审核专家。一套出口单证中，多数单证货物描述为"
            f"「{context['baseline']}」，而{context['doc_name']}为「{context['other']}」，"
            f"字符级语义相似度 {context['score']:.2f}（灰色区0.6-0.85）。"
            "请判断两者是否属于同一批货物的合理表述差异。"
            "用JSON回答：{\"verdict\": \"same_goods|different_goods|uncertain\", "
            "\"confidence\": \"高|中|低\", \"explanation\": \"不超过120字的理由，"
            "结合品类与HS编码归类常识\", \"action\": \"一句话给制单员的操作建议\"}"
        )
    return (
        "你是中欧班列运输专家。一票货物线路为"
        f"{' → '.join(context['route'])}，但使用{context['waybill_type']}。"
        "请判断该运单类型与线路是否匹配、有何风险。"
        "用JSON回答：{\"verdict\": \"needs_document_change|acceptable\", "
        "\"confidence\": \"高|中|低\", \"explanation\": \"不超过120字的理由，"
        "说明运单公约覆盖范围与换装衔接\", \"action\": \"一句话操作建议\"}"
    )


def call_live_llm(check_id: str, context: dict) -> dict | None:
    """实时调用大模型（火山方舟 OpenAI 兼容接口）。未配置 Key 或失败时返回 None。"""
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        return None
    import requests

    try:
        resp = requests.post(
            f"{ARK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": ARK_MODEL,
                "messages": [{"role": "user", "content": _build_prompt(check_id, context)}],
                "temperature": 0.2,
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content[content.index("{"): content.rindex("}") + 1])
        return {**parsed, "source": "live"}
    except Exception:
        return None


def get_ai_opinion(check_id: str, context: dict) -> dict | None:
    """
    获取AI意见：优先实时调用（配置了Key时），未命中则回退预置结果。
    返回 {verdict, confidence, explanation, action, source}；完全不可用时返回 None。
    """
    live = call_live_llm(check_id, context)
    if live:
        return live

    if check_id == "CONS-001":
        sig = suspect_signature(context.get("baseline", ""), context.get("other", ""))
    else:
        sig = route_signature(context.get("waybill_type", ""), context.get("route") or [])
    preset = _load_presets().get(sig)
    if preset:
        return {**preset, "source": "preset"}
    return None
