# -*- coding: utf-8 -*-
"""向导流程汇报截图（任务书验收材料）：
走完整向导流程复现架构方原始场景（多运单PDF），并叠加"运单缺收货人"
知识库规则演示（第4份运单），在每个关键步骤截取可视区截图。

产出：_selftest/report_screenshots/step{1..5}_*.png
前置：streamlit 8501 已运行。
用法：python _selftest/wizard_screenshots.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8501"
PDF_DIR = Path(__file__).parent.parent / "sample_pdfs"
ART = Path(__file__).parent / "report_screenshots"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def shot(page, name):
    page.wait_for_timeout(600)
    page.screenshot(path=str(ART / name))
    print(f"  📸 {name}")


def main() -> int:
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 2600})
        page.goto(BASE)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

        # ---------- 第1步：声明单据构成 ----------
        page.get_by_text("📎 上传PDF单证").click()
        page.wait_for_timeout(1500)
        page.get_by_text("多车厢分批：全套 + 运单3份").click()
        page.wait_for_timeout(800)
        loc = page.locator('input[aria-label="国际铁路运单"]')
        loc.fill("4")          # 3份正常运单 + 1份缺收货人运单
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check("第1步申报构成（运单×4）", "国际铁路运单×4" in body)
        shot(page, "step1_声明单据构成.png")

        page.get_by_text("下一步 · 按申报生成上传槽位").click()
        page.wait_for_timeout(1200)

        # ---------- 第2步：逐类型上传 + 自动拆分 ----------
        inputs = page.locator('input[type="file"]')
        inputs.nth(0).set_input_files(str(PDF_DIR / "invoice.pdf"))
        page.wait_for_timeout(2200)
        inputs.nth(1).set_input_files(str(PDF_DIR / "packing_list.pdf"))
        page.wait_for_timeout(2200)
        inputs.nth(2).set_input_files([
            str(PDF_DIR / "waybills_x3.pdf"),
            str(PDF_DIR / "waybill_missing_consignee.pdf")])
        page.wait_for_selector("text=SMU/789999/2026", timeout=60000)
        page.wait_for_timeout(1000)
        inputs.nth(3).set_input_files(str(PDF_DIR / "customs_declaration.pdf"))
        page.wait_for_timeout(3500)
        body = page.inner_text("body")
        check("3运单PDF自动拆分预览", "拆分第3份" in body)
        check("缺收货人运单进入拆分预览", "SMU/789999/2026" in body)
        # 滚到运单区块截图
        page.get_by_text("国际铁路运单（申报 4 份）").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(page, "step2_逐类型上传_多运单自动拆分.png")

        page.get_by_text("下一步 · 逐份核对识别结果").click()
        page.wait_for_timeout(2000)

        # ---------- 第3步：逐份确认（展开缺收货人的运单#4） ----------
        body = page.inner_text("body")
        check("按单据实例列出（运单#4）", "运单#4" in body)
        check("缺收货人字段失败标记", "提取失败" in body)
        for s in page.locator("summary").all():
            if "运单#4" in s.inner_text():
                if "keyboard_arrow_right" in s.inner_text():   # 折叠态才展开
                    s.click()
                    page.wait_for_timeout(900)
                break
        page.get_by_text("运单#4").first.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        shot(page, "step3_逐份核对确认_运单4缺收货人.png")

        # 逐份勾选确认
        for s in page.locator("summary").all():
            t = s.inner_text()
            if any(m in t for m in ("发票#", "装箱单#", "运单#", "报关单#", "产地证#")):
                try:
                    s.scroll_into_view_if_needed()
                    if "keyboard_arrow_right" in t:
                        s.click()
                        page.wait_for_timeout(400)
                except Exception:
                    pass
        labels = page.locator('label:has-text("本份已核对无误")')
        n_ok = 0
        for i in range(labels.count()):
            lbl = labels.nth(i)
            try:
                if lbl.is_visible() and not lbl.locator('input[type="checkbox"]').is_checked():
                    lbl.scroll_into_view_if_needed()
                    lbl.click()
                    n_ok += 1
                    page.wait_for_timeout(400)
            except Exception:
                continue
        check(f"逐份确认（{n_ok}份）", n_ok >= 7)
        shot(page, "step3b_全部单据已确认.png")

        page.get_by_text("确认全部单据，开始核验").click()
        page.wait_for_selector("text=核验结果汇总", timeout=30000)
        page.wait_for_timeout(2500)

        # ---------- 第4步：分层结果 ----------
        page.wait_for_timeout(800)
        # Streamlit 的滚动容器不是 window，优先滚主容器回到顶部
        page.evaluate("""() => {
            const cands = [window, ...document.querySelectorAll(
                'section.main, section.stMain, [data-testid="stMain"], [data-testid="stAppViewContainer"]')];
            for (const c of cands) { try { c.scrollTo(0, 0); } catch (e) {} }
        }""")
        page.wait_for_timeout(600)
        shot(page, "step4a_总评分仪表与分层结果.png")

        # 展开运单#1与运单#4分组、批次级问题
        for s in page.locator("summary").all():
            t = s.inner_text()
            if any(k in t for k in ("运单#4", "运单#1", "批次级问题")):
                try:
                    s.scroll_into_view_if_needed()
                    if "keyboard_arrow_right" in t:   # 已展开的不动（再点会折叠）
                        s.click()
                        page.wait_for_timeout(500)
                except Exception:
                    pass
        body = page.inner_text("body")
        check("运单#4分组含缺收货人FAIL", "缺少收货人" in body)
        check("批次级问题独立分组", "批次级问题" in body)
        target = None
        for s in page.locator("summary").all():
            if "运单#4" in s.inner_text():
                target = s
                break
        if target:
            target.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(page, "step4b_按单据分组_运单4缺收货人FAIL.png")

        browser.close()

    passed = all(results)
    print(f"\n{len(results)} 项检查，{'全部通过：SCREENSHOTS OK' if passed else '存在失败项'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
