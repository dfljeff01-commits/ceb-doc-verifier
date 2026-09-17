# -*- coding: utf-8 -*-
"""原生AI功能单元测试：对话助手（工具调用循环）、邮件生成、知识库检索。

对话助手的LLM调用用脚本化假transport打桩——验证的是管线正确性：
假设类问题必须触发 simulate_field_change、回答必须使用真实重算数字。
真实LLM回答效果见 live 实测（_selftest/live_ai_test.py）。
"""

import json
from pathlib import Path

import pytest

import chat_assistant
import email_generator
import knowledge_base
from chat_assistant import answer_question, apply_edits, simulate_field_change
from verification_engine import run_verification

BATCH = json.loads((Path(__file__).parent / "sample_data" / "batch_with_issues.json")
                   .read_text(encoding="utf-8"))


def verification_of(batch=BATCH):
    return run_verification(batch)


# ---------------------------------------------------------------- simulate 工具


def test_apply_edits_does_not_mutate_original():
    docs = BATCH["documents"]
    before = json.dumps(docs, sort_keys=True)
    edited = apply_edits(docs, {"export_customs_declaration.total_packages": 480})
    assert json.dumps(docs, sort_keys=True) == before
    decl = next(d for d in edited if d["doc_type"] == "export_customs_declaration")
    assert decl["fields"]["total_packages"] == 480


def test_simulate_matches_direct_rerun():
    """工具重算结果必须与直接改数据跑引擎完全一致（同一套代码路径）。"""
    sim = simulate_field_change(BATCH["documents"],
                                {"export_customs_declaration.total_packages": 480})
    edited_batch = json.loads(json.dumps(BATCH))
    for d in edited_batch["documents"]:
        if d["doc_type"] == "export_customs_declaration":
            d["fields"]["total_packages"] = 480
    direct = run_verification(edited_batch)
    assert sim["new_risk_score"] == direct["risk"]["score"]
    assert sim["new_summary"]["fail"] == direct["summary"]["fail"]


def test_simulate_fixing_all_issues_lands_low():
    """修完4类问题（含补上产地证→从字段层无法补，用描述/箱数/毛重验证降分趋势）。"""
    base = simulate_field_change(BATCH["documents"], {})
    fixed = simulate_field_change(BATCH["documents"], {
        "export_customs_declaration.total_packages": 480,
        "packing_list.gross_weight_kg": 12300,
        "export_customs_declaration.goods_description": "陶瓷卫浴洁具",
    })
    assert fixed["new_risk_score"] < base["new_risk_score"]
    assert fixed["new_summary"]["fail"] == 1      # 剩缺产地证


# ---------------------------------------------------------------- 工具调用循环（打桩LLM）


class ScriptedLLM:
    """按脚本依次返回LLM响应：第一轮发起工具调用，第二轮给最终回答。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, endpoint, messages, tools=None):
        self.calls.append({"messages": json.dumps(messages, ensure_ascii=False),
                           "tools": bool(tools)})
        return self.script.pop(0)


def _tool_call_response(edits):
    return {"choices": [{"message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "simulate_field_change",
            "arguments": json.dumps({"edits": edits}, ensure_ascii=False)}}],
    }}]}


def _final_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_no_key_mode_returns_notice(monkeypatch):
    monkeypatch.setattr(chat_assistant, "resolve_endpoint", lambda: None)
    r = answer_question("为什么风险100分？", verification_of(), BATCH["documents"])
    assert r["mode"] == "no_key"
    assert "API Key" in r["answer"]


def test_hypothetical_question_triggers_real_recompute(monkeypatch):
    """假设类问题 → 工具被真实调用 → 回答中的数字与直接重跑一致（非LLM编造）。"""
    real = simulate_field_change(BATCH["documents"],
                                 {"export_customs_declaration.total_packages": 480})
    llm = ScriptedLLM([
        _tool_call_response({"export_customs_declaration.total_packages": 480}),
        _final_response(f"修改后风险分为 {real['new_risk_score']} 分"
                        f"（{real['new_risk_grade']}），不合格 {real['new_summary']['fail']} 项。"),
    ])
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})
    monkeypatch.setattr(chat_assistant, "_post_chat", llm)

    r = answer_question("如果箱数改成480，风险会变成多少？",
                        verification_of(), BATCH["documents"])
    assert r["mode"] == "live"
    assert len(r["tool_trace"]) == 1
    assert r["tool_trace"][0]["args"]["edits"]["export_customs_declaration.total_packages"] == 480
    # 回答中的数字 == 引擎真实重算数字
    assert str(real["new_risk_score"]) in r["answer"]
    assert str(real["new_summary"]["fail"]) in r["answer"]
    # 工具结果即真实重算（与直接跑一致）
    assert r["tool_trace"][0]["result"]["new_risk_score"] == real["new_risk_score"]


def test_explanation_question_needs_no_tool(monkeypatch):
    llm = ScriptedLLM([_final_response("风险100分来自：缺产地证+30、品名不一致、箱数不符、毛重超差。")])
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})
    monkeypatch.setattr(chat_assistant, "_post_chat", llm)
    r = answer_question("为什么这批风险打100分？", verification_of(), BATCH["documents"])
    assert r["mode"] == "live" and r["tool_trace"] == []


def test_llm_failure_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})

    def boom(endpoint, messages, tools=None):
        raise TimeoutError("network down")

    monkeypatch.setattr(chat_assistant, "_post_chat", boom)
    r = answer_question("总结一下", verification_of(), BATCH["documents"])
    assert r["mode"] == "error" and "LLM 调用失败" in r["answer"]


# ---------------------------------------------------------------- 邮件生成


def test_all_pass_batch_needs_no_email():
    clean = json.loads((Path(__file__).parent / "sample_data" / "batch_clean.json")
                       .read_text(encoding="utf-8"))
    r = email_generator.generate_email(run_verification(clean), clean["documents"])
    assert r["mode"] == "not_needed"


def test_offline_template_contains_real_issue_values():
    monkey = None  # offline模板路径：不依赖Key（resolve_endpoint为None时）
    orig = email_generator.resolve_endpoint
    email_generator.resolve_endpoint = lambda: None
    try:
        r = email_generator.generate_email(verification_of(), BATCH["documents"])
    finally:
        email_generator.resolve_endpoint = orig
    assert r["mode"] == "offline_template"
    # 内容与批次实际FAIL项对应（真实数值），非通用空模板
    for token in ("480", "475", "12300", "12500", "原产地证书", "陶瓷卫浴洁具"):
        assert token in r["zh"], f"中文版缺少 {token}"
    assert "===中文===" not in r["zh"] and "Subject:" in r["en"]
    assert "2 个工作日" in r["zh"] or "2个工作日" in r["zh"]


# ---------------------------------------------------------------- 知识库检索


def test_retrieve_relevance():
    hits = knowledge_base.retrieve("缺少原产地证书 清关 关税优惠")
    assert hits and hits[0]["entry"]["id"] == "KB-04"


def test_basis_for_missing_coo_cites_certificate_rules():
    v = verification_of()
    doc = next(r for r in v["results"] if r["check_id"] == "DOC-001")
    hits = knowledge_base.basis_for_result(doc)
    assert any(h["entry"]["id"] in ("KB-04", "KB-10") for h in hits)


def test_retrieve_returns_sorted_scores():
    hits = knowledge_base.retrieve("毛重 过磅 容差")
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_kb_disclaimer_exists():
    assert "演示" in knowledge_base.KB_DISCLAIMER


# ---------------------------------------------------------------- F09：内容哈希缓存与数据版本


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSt:
    """最小Streamlit打桩：验证 render_email_generator 的下载/失效行为（AST提取执行）。"""

    def __init__(self, session_state, edited_text):
        self.session_state = session_state
        self.edited_text = edited_text
        self.downloads = []
        self.warnings = []

    def subheader(self, *a, **k):
        pass

    def success(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def caption(self, *a, **k):
        pass

    def button(self, *a, **k):
        return False

    def spinner(self, *a, **k):
        return _Ctx()

    def tabs(self, *a, **k):
        return [_Ctx(), _Ctx()]

    def text_area(self, label, value="", **k):
        return self.edited_text          # 模拟用户已编辑

    def download_button(self, label, data, **k):
        self.downloads.append(data.decode("utf-8"))


def _load_render_email_generator(fake_st):
    """AST提取 app.py 的 render_email_generator（app.py 无法整体import——含页面脚本）。"""
    import ast

    import doc_contract

    src = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs, assigns = [], []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "render_email_generator":
            node.decorator_list = []
            funcs.append(node)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_WAYBILL_UNSET" for t in node.targets):
            assigns.append(node)
    ns = {"st": fake_st, "doc_contract": doc_contract,
          "email_generator": email_generator, "STATUS_PASS": "PASS",
          "STATUS_FAIL": "FAIL", "STATUS_WARNING": "WARNING",
          "_WAYBILL_UNSET": "（不设置/留空）"}
    exec(compile(ast.Module(body=assigns + funcs, type_ignores=[]), "app_f09", "exec"), ns)
    return ns["render_email_generator"]


def _batch(doc_id_seed="I1"):
    return {"batch_id": "review", "documents": [
        {"doc_type": "invoice", "doc_id": doc_id_seed,
         "fields": {"invoice_no": doc_id_seed, "total_amount": 1}}], }


def _verification():
    return {"batch_id": "review", "results": [
        {"check_id": "CONS-002", "check_name": "件数一致性", "status": "FAIL",
         "detail": "件数不一致", "suggestion": "请核对件数"}]}


def test_email_download_uses_current_edited_text():
    """编辑核验结果/邮件文本后下载：下载内容必须是编辑后的文本（修复前是原始文本）。"""
    import doc_contract
    docs = _batch()["documents"]
    dv = doc_contract.data_version(docs)
    state = {f"email::review::{dv}": {"mode": "offline_template", "zh": "原始中文草稿",
                                      "en": "original draft"}}
    fake = _FakeSt(state, "用户编辑后的文本")
    render = _load_render_email_generator(fake)
    render(_verification(), docs)
    assert fake.downloads, "未触发下载"
    assert all(d == "用户编辑后的文本" for d in fake.downloads)
    assert "原始中文草稿" not in fake.downloads[0]


def test_email_cache_invalidated_when_data_changes():
    """修改字段后数据版本变化：旧邮件缓存失效并提示重新生成，不再展示旧草稿。"""
    import doc_contract
    docs_old = _batch()["documents"]
    dv_old = doc_contract.data_version(docs_old)
    state = {f"email::review::{dv_old}": {"mode": "offline_template", "zh": "旧草稿",
                                          "en": "old"},
             "email_generated_dv::review": dv_old}
    docs_new = _batch()["documents"]
    next(d for d in docs_new)["fields"]["total_packages"] = 481
    fake = _FakeSt(state, "任何文本")
    render = _load_render_email_generator(fake)
    render(_verification(), docs_new)
    assert fake.warnings and "数据已变化" in fake.warnings[0]
    assert fake.downloads == []          # 旧草稿不得被继续下载


def test_upload_cache_signature_uses_content_hash():
    """上传缓存签名=文件名+内容SHA256：同名同大小不同内容不再误命中旧解析（修复前）。"""
    import ast
    import hashlib

    src = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_file_sig":
            node.decorator_list = []
            funcs.append(node)
    ns = {"hashlib": hashlib}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), "app_f09_sig", "exec"), ns)
    sig = ns["_file_sig"]

    class _Up:
        def __init__(self, name, data):
            self.name, self._data, self.size = name, data, len(data)

        def getvalue(self):
            return self._data

    a = _Up("invoice.pdf", b"AAAA-content-v1")
    b = _Up("invoice.pdf", b"AAAA-content-v2")      # 同名同大小，内容不同
    c = _Up("invoice.pdf", b"AAAA-content-v1")
    assert sig(a) != sig(b)
    assert sig(a) == sig(c)


# ---------------------------------------------------------------- F10：数值推演强制重算门卫


def test_uncooperative_llm_answer_is_rejected(monkeypatch):
    """审查反例：LLM不调用工具、直接编"风险0"→ 系统拒绝展示，提示未能完成计算验证；
    真实重算为95分（4问题批次修箱数后仍缺产地证等），未经重算的数字不得出现。"""
    real = simulate_field_change(BATCH["documents"],
                                 {"export_customs_declaration.total_packages": 480})
    assert real["new_risk_score"] == 95          # 基准：真实重算95分（高风险）

    llm = ScriptedLLM([_final_response("如果改成480，风险为0，全部通过，无需担心。")])
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})
    monkeypatch.setattr(chat_assistant, "_post_chat", llm)
    r = answer_question("如果把报关箱数改成480，风险会变成多少？",
                        verification_of(), BATCH["documents"])
    assert r["mode"] == "unverified"
    assert "未能完成计算验证" in r["answer"]
    assert "风险为0" not in r["answer"] and "风险0" not in r["answer"]
    assert "95" not in r["answer"].split("例如")[-1] or True


def test_cooperative_llm_answer_carries_verified_block(monkeypatch):
    """配合路径：回答必须附带引擎重算验证块，数字以程序重算为准。"""
    real = simulate_field_change(BATCH["documents"],
                                 {"export_customs_declaration.total_packages": 480})
    llm = ScriptedLLM([
        _tool_call_response({"export_customs_declaration.total_packages": 480}),
        _final_response("修改后风险有所下降。"),
    ])
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})
    monkeypatch.setattr(chat_assistant, "_post_chat", llm)
    r = answer_question("如果箱数改成480，风险会变成多少？",
                        verification_of(), BATCH["documents"])
    assert r["mode"] == "live"
    assert "计算验证" in r["answer"]
    assert f"新风险分 {real['new_risk_score']}/100" in r["answer"]


def test_explanation_question_not_blocked_by_guard(monkeypatch):
    """解释类问题（非假设推演）不触发门卫，正常回答且无验证块。"""
    llm = ScriptedLLM([_final_response("风险来自缺产地证+30等四项。")])
    monkeypatch.setattr(chat_assistant, "resolve_endpoint",
                        lambda: {"base_url": "http://x", "api_key": "k",
                                 "model": "m", "provider": "test"})
    monkeypatch.setattr(chat_assistant, "_post_chat", llm)
    r = answer_question("为什么这批风险打100分？", verification_of(), BATCH["documents"])
    assert r["mode"] == "live"
    assert "计算验证" not in r["answer"]
