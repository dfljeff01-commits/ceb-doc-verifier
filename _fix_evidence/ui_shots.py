# -*- coding: utf-8 -*-
"""UI截图工具（开发期工具）：对运行中的 Streamlit 应用采集指定截面的截图。
用法：python _fix_evidence/ui_shots.py <输出目录> [batch_id]
前置：API(8000) 与 Streamlit(8501) 已运行。
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "_fix_evidence/ui_shots")
BATCH = sys.argv[2] if len(sys.argv) > 2 else "batch_with_issues"
BASE = "http://localhost:8501"
OUT.mkdir(parents=True, exist_ok=True)


def shot(page, name, full=False):
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    print(f"  📸 {name}.png")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        # 1) 首屏（示例批次A，未展开核验）
        page.goto(f"{BASE}/?batch=batch_clean")
        page.wait_for_timeout(6000)
        shot(page, "01_首屏_示例批次A")
        # 2) 问题批次B：滚动到风险仪表盘与汇总
        page.goto(f"{BASE}/?batch={BATCH}")
        page.wait_for_timeout(7000)
        shot(page, "02_批次B_汇总与风险仪表盘")
        # 3) 明细表格区
        page.evaluate("""() => {
            const el = [...document.querySelectorAll('h3,h4,div')].find(
                d => d.textContent === '详细核验明细');
            if (el) el.scrollIntoView({block: 'start'});
        }""")
        page.wait_for_timeout(1500)
        shot(page, "03_批次B_核验明细表")
        # 4) 建议区与知识库
        page.evaluate("""() => window.scrollBy(0, 700)""")
        page.wait_for_timeout(1200)
        shot(page, "04_批次B_AI建议与合规依据", full=False)
        # 5) 邮件区
        page.evaluate("""() => {
            const el = [...document.querySelectorAll('h2,div')].find(
                d => d.textContent && d.textContent.includes('整改邮件'));
            if (el) el.scrollIntoView({block: 'start'});
        }""")
        page.wait_for_timeout(1500)
        shot(page, "05_批次B_整改邮件区")
        # 6) 窄屏模拟（P2 移动端检查）
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(f"{BASE}/?batch={BATCH}")
        mobile.wait_for_timeout(7000)
        shot(mobile, "06_窄屏390px_批次B首屏")
        mobile.evaluate("""() => window.scrollBy(0, 900)""")
        mobile.wait_for_timeout(1200)
        shot(mobile, "07_窄屏390px_明细区")
        browser.close()
    print("done ->", OUT)


if __name__ == "__main__":
    main()
