# -*- coding: utf-8 -*-
"""
视觉/交互自测（开发期工具，不属于交付应用本体）。

用本机 Edge(headless) 对运行中的 Streamlit 应用做端到端验证：
  1. 三个批次 URL 直达，读取汇总卡片与建议框
  2. 明细表格 canvas 像素采样，验证状态列红/黄/绿底色真实渲染
  3. 批次B 手动把报关单件数 475 改成 480，验证 FAIL 数实时 4→3
  4. 点击导出PDF按钮，捕获下载文件并校验 %PDF 头

前置：`streamlit run app.py --server.port 8501` 已在运行。
用法：python _selftest/visual_test.py
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8501"
ART = Path(__file__).parent
TARGET_COLORS = {
    "PASS_green(#E8F5E9)": (232, 245, 233),
    "WARNING_yellow(#FFF8E1)": (255, 248, 225),
    "FAIL_red(#FFEBEE)": (255, 235, 238),
}
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def read_cards(page):
    """读取汇总迷你卡（.ceb-mini：label 与数值两个子div）。"""
    return page.evaluate("""() => {
      return [...document.querySelectorAll('.ceb-mini')].map(d => {
        const divs = d.querySelectorAll(':scope > div');
        return (divs[1] ? divs[1].textContent : '') + '=' + (divs[0] ? divs[0].textContent : '');
      });
    }""")


def expand_flat_table(page):
    """展开『完整核验明细表』折叠区，使状态列底色真实渲染（供像素采样）。"""
    for s in page.locator("summary").all():
        if "完整核验明细表" in s.inner_text():
            if "keyboard_arrow_right" in s.inner_text():
                s.scroll_into_view_if_needed()
                s.click()
                page.wait_for_timeout(1500)
            break


def read_alerts(page):
    return page.evaluate("""() =>
      [...document.querySelectorAll('[data-testid="stAlert"]')].map(a => a.textContent.slice(0, 60))
    """)


def sample_canvas_colors(page):
    page.evaluate("""() =>
      document.querySelector('[data-testid="stDataFrame"]').scrollIntoView({block: 'center'})
    """)
    page.wait_for_timeout(2000)
    return page.evaluate("""(targets) => {
      const canvas = document.querySelector('[data-testid="stDataFrame"] canvas');
      if (!canvas) return null;
      const ctx = canvas.getContext('2d');
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      const counts = {};
      for (const k of Object.keys(targets)) counts[k] = 0;
      for (let i = 0; i < img.length; i += 4) {
        const r = img[i], g = img[i+1], b = img[i+2];
        for (const [name, [tr, tg, tb]] of Object.entries(targets)) {
          if (Math.abs(r-tr) <= 4 && Math.abs(g-tg) <= 4 && Math.abs(b-tb) <= 4) { counts[name]++; break; }
        }
      }
      return counts;
    }""", {k: list(v) for k, v in TARGET_COLORS.items()})


def open_batch(page, batch_id):
    page.goto(f"{BASE}/?batch={batch_id}")
    page.wait_for_load_state("domcontentloaded")
    # 导出PDF按钮位于汇总卡片之后，出现即代表整页渲染完成
    page.get_by_role("button", name="导出PDF报告").wait_for(timeout=25000)
    page.wait_for_timeout(2000)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge",
            headless=True,
            args=["--disable-renderer-backgrounding",
                  "--disable-background-timer-throttling",
                  "--disable-backgrounding-occluded-windows"],
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        # ---- 批次A：干净数据 ----
        print("== 批次A（全部通过） ==")
        open_batch(page, "batch_clean")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        check("卡片 0 FAIL 0 WARNING",
              cards.get("不合格 FAIL") == "0" and cards.get("警告 WARNING") == "0", str(cards))
        # 19 = 14项批级/交叉检查 + 5份单据各1条单据规范检查（DOC-101，任务书问题三）
        check("卡片 19 项全 PASS", cards.get("通过 PASS") == "19", str(cards))
        check("场景句显示", "38.7" in page.inner_text("body"))
        body = page.inner_text("body")
        check("分层视图：5份单据均无问题标记",
              body.count("✅ 本份无问题") == 5 and "按单据查看问题" in body, str(body.count("本份无问题")))
        expand_flat_table(page)
        colors = sample_canvas_colors(page)
        check("表格绿色PASS底色渲染", bool(colors) and colors["PASS_green(#E8F5E9)"] > 500, str(colors))
        # 无误报判定含噪声容差：字体/emoji抗锯齿会产生少量近色像素（实测<300），
        # 而真实WARNING/FAIL行像素量在数千以上（批次C黄≈3391、批次B红≈13419）
        check("表格无红黄底色（不误报）", bool(colors) and colors["FAIL_red(#FFEBEE)"] <= 100
              and colors["WARNING_yellow(#FFF8E1)"] <= 1000, str(colors))
        page.screenshot(path=str(ART / "batch_A_clean.png"), full_page=True)

        # ---- 批次B：4类问题 ----
        print("== 批次B（含4类问题） ==")
        open_batch(page, "batch_with_issues")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        check("卡片恰好 4 FAIL 0 WARNING",
              cards.get("不合格 FAIL") == "4" and cards.get("警告 WARNING") == "0", str(cards))
        alerts = page.inner_text("body")
        check("缺失产地证告警", "原产地证书" in alerts)
        check("货物描述不一致告警", ("货物描述不一致" in alerts) or ("货物描述与多数单证不一致" in alerts))
        check("件数不一致告警(480/475)", "件数不一致" in alerts)
        check("毛重超差告警", "毛重超出1%容差" in alerts)
        expand_flat_table(page)
        colors = sample_canvas_colors(page)
        check("表格红色FAIL底色渲染", bool(colors) and colors["FAIL_red(#FFEBEE)"] > 500, str(colors))
        page.screenshot(path=str(ART / "batch_B_issues.png"), full_page=True)

        # ---- P1: 手动编辑实时复核（把报关单件数475改成480）----
        print("== P1 手动编辑实时复核 ==")
        customs_exp = page.locator('[data-testid="stExpander"]',
                                   has_text="DEC-29152026000123456").first
        customs_exp.locator("summary, button").first.click()
        page.wait_for_timeout(1500)
        pkg_input = customs_exp.locator('input[aria-label*="件数"]').first
        pkg_input.fill("480")
        pkg_input.press("Enter")
        page.wait_for_timeout(4000)
        pkg_input.blur()
        page.wait_for_timeout(2500)
        cards = dict(s.split("=", 1) for s in read_cards(page))
        check("改箱数后 FAIL 实时 4→3", cards.get("不合格 FAIL") == "3", str(cards))
        body = page.inner_text("body")
        check("显示已手动修改提示", "已手动修改" in body)
        page.screenshot(path=str(ART / "batch_B_edited.png"), full_page=True)

        # ---- P1: PDF 导出 ----
        print("== P1 导出PDF ==")
        with page.expect_download(timeout=20000) as dl:
            page.get_by_role("button", name="导出PDF报告").click()
        pdf_path = ART / "report_download_test.pdf"
        dl.value.save_as(str(pdf_path))
        head = pdf_path.read_bytes()[:5]
        size = pdf_path.stat().st_size
        check("PDF下载成功且为有效PDF", head == b"%PDF-" and size > 5000,
              f"head={head!r} size={size}")

        # ---- 批次C：路线合规 WARNING ----
        print("== 批次C（路线合规告警） ==")
        open_batch(page, "batch_route_warning")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        check("卡片 0 FAIL 1 WARNING",
              cards.get("不合格 FAIL") == "0" and cards.get("警告 WARNING") == "1", str(cards))
        alerts = page.inner_text("body")
        check("WARNING 内容：SMGS+土耳其", "SMGS" in alerts and "土耳其" in alerts, alerts[:120])
        expand_flat_table(page)
        colors = sample_canvas_colors(page)
        check("表格黄色WARNING底色渲染", bool(colors) and colors["WARNING_yellow(#FFF8E1)"] > 500, str(colors))
        page.screenshot(path=str(ART / "batch_C_route_warning.png"), full_page=True)

        browser.close()

    print(f"\n{len(results)} 项检查，{'全部通过：VISUAL TEST PASSED' if all(results) else '存在失败项：VISUAL TEST FAILED'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
