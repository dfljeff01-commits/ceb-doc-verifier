# -*- coding: utf-8 -*-
"""原生AI功能 live 实测（需 GLM_API_KEY/BIGMODEL_API_KEY 或 ARK_API_KEY）。

验证验收清单：
  1. 三类问答实测（分数解释 / 假设推演 / 总结），附真实回答
  2. 假设推演回答中的数字与 simulate_field_change 真实重算一致（function calling 真的调了）
  3. 整改邮件 live 生成样本（批次B：含4类问题）
用法：python _selftest/live_ai_test.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chat_assistant
import email_generator
from llm_endpoint import resolve_endpoint
from verification_engine import run_verification

ROOT = Path(__file__).parent.parent
ART = Path(__file__).parent


def main():
    endpoint = resolve_endpoint()
    if endpoint is None:
        print("未检测到API Key，跳过live测试")
        return 1
    print(f"LLM provider: {endpoint['provider']} model={endpoint['model']}")

    batch = json.loads((ROOT / "sample_data" / "batch_with_issues.json").read_text(encoding="utf-8"))
    v = run_verification(batch)
    docs = batch["documents"]
    ok = True

    questions = [
        ("分数解释类", "为什么这批风险打100分？"),
        ("假设推演类", "如果报关单箱数改成480，风险分会变成多少？还会剩哪些问题？"),
        ("总结类", "帮我用一句话总结这批单证的核心问题。"),
    ]
    print("=" * 80)
    for qtype, q in questions:
        r = chat_assistant.answer_question(q, v, docs)
        print(f"\n【{qtype}】问：{q}")
        print(f"模式: {r['mode']} | 工具调用: {len(r.get('tool_trace', []))} 次")
        print(f"答：{r['answer']}")
        if qtype == "假设推演类":
            real = chat_assistant.simulate_field_change(
                docs, {"export_customs_declaration.total_packages": 480})
            matched = (str(real["new_risk_score"]) in r["answer"]
                       and len(r.get("tool_trace", [])) >= 1)
            print(f"  → 真实重算: 风险 {real['new_risk_score']}({real['new_risk_grade']}) "
                  f"FAIL {real['new_summary']['fail']} | 回答数字一致: {'✅' if matched else '❌'}")
            ok = ok and matched
        else:
            ok = ok and r["mode"] == "live" and len(r["answer"]) > 20

    print("\n" + "=" * 80)
    print("【整改邮件 live 生成】（批次B：含4类问题）")
    mail = email_generator.generate_email(v, docs)
    print(f"mode: {mail['mode']}")
    if mail["mode"] == "live":
        sample = f"=== 中文版 ===\n{mail['zh']}\n\n=== English ===\n{mail['en']}"
    else:
        sample = f"（降级模式 {mail['mode']}）\n{mail['zh']}"
    (ART / "live_email_sample.txt").write_text(sample, encoding="utf-8")
    print(sample[:1500])
    print("... 完整样本已存 _selftest/live_email_sample.txt")
    tokens = [t for t in ("480", "475", "原产地证书") if t not in mail["zh"]]
    if tokens:
        print(f"⚠️ 邮件未覆盖: {tokens}")
        ok = False

    print("\n" + ("LIVE TEST PASSED" if ok else "LIVE TEST FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
