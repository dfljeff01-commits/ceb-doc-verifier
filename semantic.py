# -*- coding: utf-8 -*-
"""
语义相似度模块（升级任务书·方向一）。

目标：货物描述等文本字段的比对，从"字面全等"升级为"语义相似度分级"：
  相似度 ≥ 0.85        一致（PASS）
  0.60 ~ 0.85          存疑（WARNING，转人工复核 / AI二次判断）
  < 0.60               不一致（FAIL）

后端说明（如实披露）：
  1) 默认后端 char-ngram-tf：字符 1+2-gram 词频向量 + 余弦相似度。
     选型原因：a) 纯离线、零模型下载，现场演示稳定；b) 短文本（单证品名）
     场景下判别边界清晰；c) 中英混排无需分词，避免分词错误引入噪声。
     这是任务书允许的"向量化退化方案"（TF词频 + 余弦，替代 sentence-transformers
     句向量），架构上通过 SemanticBackend 接口预留了句向量后端。
  2) 可选后端 sentence-transformers（paraphrase-multilingual-MiniLM-L12-v2）：
     设置环境变量 SEMANTIC_BACKEND=st 即可启用（需自行安装依赖与模型）。
     未在 demo 默认启用的原因：多语言句向量对"陶瓷卫浴洁具 vs 卫浴陶瓷制品"
     这类字面高度重合的错报对评分偏高（实测同类模型普遍 ≥0.8），会把业务上
     必须拦下的不一致升为"一致"，与核验目标的召回要求冲突。
"""

from __future__ import annotations

import math
import os
import re

# 相似度分级阈值（升级任务书给定）
THRESHOLD_MATCH = 0.85
THRESHOLD_SUSPECT = 0.60

BACKEND_TFIDF = "char-ngram-tf"
BACKEND_ST = "sentence-transformers"


def _normalize(text: str) -> str:
    """归一化：仅保留文字与数字（去除中英文标点、空白、符号），并转小写。
    例外：数字间的小数点用"点"占位保留——"1.5mm"与"15mm"不得归一化成同一串（F03）。"""
    text = str(text or "").lower()
    text = re.sub(r"(?<=\d)\.(?=\d)", "点", text)
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _char_ngrams(text: str) -> dict:
    """字符 1+2-gram 词频向量。"""
    counts: dict = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    for i in range(len(text) - 1):
        bg = text[i:i + 2]
        counts[bg] = counts.get(bg, 0) + 1
    return counts


def _cosine(v1: dict, v2: dict) -> float:
    dot = sum(c * v2.get(k, 0) for k, c in v1.items())
    if dot == 0:
        return 0.0
    n1 = math.sqrt(sum(c * c for c in v1.values()))
    n2 = math.sqrt(sum(c * c for c in v2.values()))
    return dot / (n1 * n2)


class CharNgramBackend:
    """默认后端：字符 1+2-gram 词频向量余弦相似度。"""
    name = BACKEND_TFIDF

    def similarity(self, a: str, b: str) -> float:
        na, nb = _normalize(a), _normalize(b)
        if na == nb:
            return 1.0
        if not na or not nb:
            return 0.0
        return round(_cosine(_char_ngrams(na), _char_ngrams(nb)), 4)


class SentenceTransformerBackend:
    """可选后端：sentence-transformers 多语言句向量（需环境已安装）。"""
    name = BACKEND_ST

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"):
        from sentence_transformers import SentenceTransformer  # 延迟导入
        self.model = SentenceTransformer(model_name)

    def similarity(self, a: str, b: str) -> float:
        from sentence_transformers.util import cos_sim
        score = float(cos_sim(self.model.encode(_normalize(a)),
                              self.model.encode(_normalize(b))))
        return round(max(0.0, min(1.0, score)), 4)


_backend = None


def get_backend():
    """按环境变量选择后端；默认 char-ngram-tf，ST 不可用时自动回退。"""
    global _backend
    if _backend is not None:
        return _backend
    if os.environ.get("SEMANTIC_BACKEND", "").lower() in ("st", "sentence-transformers"):
        try:
            _backend = SentenceTransformerBackend()
            return _backend
        except Exception as exc:  # 模型缺失/依赖未装 → 回退
            print(f"[semantic] sentence-transformers 不可用（{exc}），回退 char-ngram-tf")
    _backend = CharNgramBackend()
    return _backend


def backend_name() -> str:
    return get_backend().name


def similarity(a: str, b: str) -> float:
    return get_backend().similarity(a, b)


def grade_similarity(score: float) -> str:
    """相似度 → 分级：match(一致) / suspect(存疑) / mismatch(不一致)。"""
    if score >= THRESHOLD_MATCH:
        return "match"
    if score >= THRESHOLD_SUSPECT:
        return "suspect"
    return "mismatch"


# ---------------------------------------------------------------- 关键实体守卫（F03）
#
# 字符相似度只衡量"字面接近"，会被规格数字抹平（"钢板厚度1.5mm" 与
# "钢板厚度15mm" 去标点后完全相同 → 相似度1.0）。因此数字+单位规格、
# 否定词必须独立核对：出现矛盾时无论字符相似度多高都判 mismatch。

_SPEC_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm|cm|m|km|kg|g|mg|lb|oz|t|ton|s|h|min|v|mv|kv|w|kw|"
    r"mah|ah|wh|kwh|hz|pcs|mm2|mm3|m2|m3|l|ml|%)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_NEGATIVE_TOKENS = ("不含", "无", "不带", "非", "禁止",
                    "without", "non-", "not ", "no ", "free from")


def spec_tokens(text: str) -> set:
    """提取"数字+单位"规格集合；小数点保留（1.5 与 15 是不同规格）。"""
    tokens = set()
    for num, unit in _SPEC_RE.findall(str(text or "")):
        try:
            tokens.add((float(num), unit.lower()))
        except ValueError:
            continue
    return tokens


def bare_numbers(text: str) -> set:
    """提取全部数字（含无单位数字，如型号/数量）；小数点保留。"""
    nums = set()
    for num in _NUM_RE.findall(str(text or "")):
        try:
            nums.add(float(num))
        except ValueError:
            continue
    return nums


def contradiction(a: str, b: str) -> str | None:
    """
    关键实体矛盾检测（F03）。返回矛盾类型描述；无矛盾返回 None。
      - numeric_spec : 带单位的规格数字不一致（厚度/容量/重量等）
      - bare_number  : 无单位数字不一致（仅当两段文本都有数字时）
      - negation     : 否定词有无不一致（"含X" vs "不含X"）
    """
    na, nb = str(a or ""), str(b or "")
    specs_a, specs_b = spec_tokens(na), spec_tokens(nb)
    if specs_a != specs_b and (specs_a or specs_b):
        return "numeric_spec"
    nums_a, nums_b = bare_numbers(na), bare_numbers(nb)
    if nums_a != nums_b and (nums_a or nums_b):
        return "bare_number"
    neg_a = any(tok in na.lower() for tok in _NEGATIVE_TOKENS)
    neg_b = any(tok in nb.lower() for tok in _NEGATIVE_TOKENS)
    if neg_a != neg_b:
        return "negation"
    return None


def compare(a: str, b: str) -> dict:
    """
    描述比对统一入口（引擎使用）：字符相似度 + 关键实体守卫。
    返回 {score, grade, guard}：guard 非 None 时 grade 强制为 mismatch，
    保证"规格矛盾"不会被高字符相似度放行（修复 F03）。
    """
    score = similarity(a, b)
    guard = contradiction(a, b)
    if guard:
        grade = "mismatch"
    else:
        grade = grade_similarity(score)
    return {"score": score, "grade": grade, "guard": guard}
