# -*- coding: utf-8 -*-
"""上传PDF模式的端到端测试（Playwright + 本机Edge）。

覆盖补充任务书验收项：文本型直取、类型自动判断、预览表展示、
人工纠正、开始核验门控、核验结果与风险仪表盘渲染。

前置：streamlit 8501 已运行。
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


def main() -> int:
    files = [str(PDF_DIR / f) for f in
             ("invoice.pdf", "packing_list.pdf", "waybill.pdf", "customs_declaration.pdf")]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

        # 切到上传模式
        page.get_by_text("📎 上传PDF单证").click()
        page.wait_for_timeout(1500)

        # 上传4份文本型PDF
        page.set_input_files('input[type="file"]', files)
        page.wait_for_selector("text=提取结果预览", timeout=30000)
        page.wait_for_timeout(1500)

        body = page.inner_text("body")
        check("上传后出现提取结果预览", "提取结果预览" in body)
        check("4份文件均自动判型并给出关键词得分",
              body.count("单证类型：自动判断（关键词得分") == 4,
              str(body.count("单证类型：自动判断")))
        check("文本型PDF关键数值提取（480/12300）",
              "480" in body and "12300" in body)
        check("提取置信度标记展示", "需人工核对" in body or "高（关键词命中）" in body)
        check("混合判型提示可见", "页面判型：P1:文本" in body)

        # 开始核验门控：未点击前不应有核验结果
        check("未点开始核验时无结果区", "核验结果汇总" not in page.inner_text("body"))
        page.get_by_text("▶️ 开始核验").click()
        page.wait_for_timeout(4000)

        body = page.inner_text("body")
        check("点击后出现核验结果汇总", "核验结果汇总" in body)
        check("风险评分仪表盘渲染", "单证组风险分" in body and "分数构成" in body)
        # 上传的单证自带缺产地证/品名差异/箱数475 vs 480 → 应为高风险
        check("上传批次风险判定为高风险", "高风险" in body)
        page.screenshot(path=str(ART / "upload_mode.png"), full_page=True)

        # 手动改字段后核验实时更新：把报关单品名改为与发票一致
        page.get_by_text("出口报关单 Export Customs Declaration（PDF-").first.click()
        page.wait_for_timeout(1200)
        desc_input = page.locator('input[aria-label*="货物描述"]').last
        desc_input.fill("陶瓷卫浴洁具")
        desc_input.press("Enter")
        page.wait_for_timeout(3500)
        body = page.inner_text("body")
        check("修改品名后实时复核生效", "已手动修改" in body)

        browser.close()

    passed = all(results)
    print(f"\n{len(results)} 项检查，{'全部通过：UPLOAD E2E PASSED' if passed else '存在失败项'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
