# -*- coding: utf-8 -*-
"""LLM 端点解析（chat_assistant / email_generator 共用）。

优先级：
  1) ARK_API_KEY（火山方舟 OpenAI 兼容接口，可选 ARK_BASE_URL / ARK_MODEL）
  2) GLM_API_KEY / BIGMODEL_API_KEY（智谱开放平台 OpenAI 兼容接口，默认 glm-4-flash）
未配置任何 Key 时返回 None，调用方走降级路径（页面提示"需配置 API Key 启用"）。
"""

from __future__ import annotations

import os

GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_GLM_MODEL = os.environ.get("GLM_MODEL", "glm-4-flash")


def resolve_endpoint() -> dict | None:
    """返回 {base_url, api_key, model, provider}；无可用 Key 时返回 None。"""
    ark_key = os.environ.get("ARK_API_KEY")
    if ark_key:
        return {
            "base_url": os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            "api_key": ark_key,
            "model": os.environ.get("ARK_MODEL", "doubao-seed-1-6-250615"),
            "provider": "volc-ark",
        }
    glm_key = os.environ.get("GLM_API_KEY") or os.environ.get("BIGMODEL_API_KEY")
    if glm_key:
        return {
            "base_url": os.environ.get("GLM_BASE_URL", GLM_BASE_URL),
            "api_key": glm_key,
            "model": DEFAULT_GLM_MODEL,
            "provider": "zhipu-glm",
        }
    return None
