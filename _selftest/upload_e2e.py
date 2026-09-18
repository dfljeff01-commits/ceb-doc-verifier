# -*- coding: utf-8 -*-
"""向导式上传模式端到端测试（Playwright + 本机Edge，任务书问题一/二/四验收）。

覆盖：声明单据构成 → 逐类型上传（多运单PDF自动拆分预览）→ 逐份核对确认 →
核验分层结果（按单据分组）。每步截图存 _selftest/wizard_*.png。

前置：streamlit 8501 已运行（核验API 8000 可选，未启动自动进程内直连）。
用法：python _selftest/upload_e2e.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8501"
PDF_DIR = Path(__file__).parent.parent / "sample_pdfs"
ART = Path(__file__).parent

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def set_number(page, label: str, value: str) -> bool:
    """设置 st.number_input 的值（按 aria-label 定位 spinbutton）。"""
    try:
        loc = page.locator(f'input[aria-label="{label}"]')
        loc.fill(value)
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        return True
    except Exception:
        return False


def confirm_all_docs(page) -> int:
    """展开全部单据卡，逐份点击『本份已核对无误』复选框，返回勾选数。
    （Streamlit checkbox 的 input 视觉隐藏，须点击 label 本体）"""
    confirmed = 0
    for s in page.locator("summary").all():
        text = s.inner_text()
        if any(mark in text for mark in ("发票#", "装箱单#", "运单#", "报关单#", "产地证#")):
            try:
                s.scroll_into_view_if_needed()
                s.click()
                page.wait_for_timeout(600)
            except Exception:
                pass
    labels = page.locator('label:has-text("本份已核对无误")')
    for i in range(labels.count()):
        lbl = labels.nth(i)
        try:
            if not lbl.is_visible():
                continue
            box = lbl.locator('input[type="checkbox"]')
            if not box.is_checked():
                lbl.click()
                confirmed += 1
                page.wait_for_timeout(600)
        except Exception:
            continue
    return confirmed


def main() -> int:
    files = {
        "invoice": str(PDF_DIR / "invoice.pdf"),
        "packing": str(PDF_DIR / "packing_list.pdf"),
        "waybill": str(PDF_DIR / "waybills_x3.pdf"),
        "customs": str(PDF_DIR / "customs_declaration.pdf"),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

        # 切到向导上传模式
        page.get_by_text("📎 上传PDF单证").click()
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("进入第1步：声明单据构成", "声明单据构成" in body)

        # 预设：全套 + 运单3份（产地证保持申报1份但不上传，验证构成差异提示路径）
        page.get_by_text("多车厢分批：全套 + 运单3份").click()
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        check("申报构成回显（运单×3）", "国际铁路运单×3" in body)
        page.screenshot(path=str(ART / "wizard_step1_composition.png"), full_page=True)

        page.get_by_text("下一步 · 按申报生成上传槽位").click()
        page.wait_for_timeout(1200)

        # 第2步：逐类型上传（每类型独立上传位，按上传位分别设置文件）
        body = page.inner_text("body")
        check("第2步出现独立上传槽位（4类）",
              body.count("申报 1 份") + body.count("申报 3 份") >= 4)
        inputs = page.locator('input[type="file"]')
        expect_n = 4    # 发票/装箱单/运单/报关单 各一个上传位（产地证未上传也保留槽位）
        assert inputs.count() >= expect_n, f"上传位数量异常：{inputs.count()}"
        inputs.nth(0).set_input_files(files["invoice"])
        page.wait_for_timeout(2500)
        inputs.nth(1).set_input_files(files["packing"])
        page.wait_for_timeout(2500)
        inputs.nth(2).set_input_files(files["waybill"])
        page.wait_for_selector("text=拆分第3份", timeout=60000)
        page.wait_for_timeout(1000)
        inputs.nth(3).set_input_files(files["customs"])
        page.wait_for_timeout(4000)
        body = page.inner_text("body")
        check("多运单PDF自动拆分为3份预览", "拆分第3份" in body)
        check("拆分结果展示各自运单号", "SMU/789456/2026" in body
              and "SMU/789457/2026" in body and "SMU/789458/2026" in body)
        check("产地证未上传给出构成差异提示", "尚未上传任何文件" in body)
        page.screenshot(path=str(ART / "wizard_step2_upload_split.png"), full_page=True)

        page.get_by_text("下一步 · 逐份核对识别结果").click()
        page.wait_for_timeout(1500)

        # 第3步：逐份核对确认（6份：发票/箱单/运单×3/报关单）
        body = page.inner_text("body")
        check("第3步按单据实例列出（运单#1）", "运单#1" in body)
        check("第3步按单据实例列出（运单#3）", "运单#3" in body)
        n = confirm_all_docs(page)
        check(f"逐份勾选确认（实际{n}份）", n >= 6, f"confirmed={n}")
        page.screenshot(path=str(ART / "wizard_step3_confirm.png"), full_page=True)

        page.get_by_text("确认全部单据，开始核验").click()
        page.wait_for_selector("text=核验结果汇总", timeout=30000)
        page.wait_for_timeout(2500)

        # 第4步：分层结果
        body = page.inner_text("body")
        check("总评分仪表盘渲染", "单证组风险分" in body)
        check("分层结果区出现", "按单据查看问题" in body)
        check("单据实例分组展示（运单#1/运单#3）",
              "运单#1" in body and "运单#3" in body)
        check("批次级问题独立归组（缺产地证/构成核对）", "批次级问题" in body)
        check("问题带字段与修改建议", "怎么改" in body or "建议" in body)
        check("规则知识库版本标注", "doc-rules" in body)
        page.screenshot(path=str(ART / "wizard_step4_grouped_result.png"), full_page=True)

        browser.close()

    passed = all(results)
    print(f"\n{len(results)} 项检查，{'全部通过：WIZARD E2E PASSED' if passed else '存在失败项'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
