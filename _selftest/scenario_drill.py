# -*- coding: utf-8 -*-
"""真实使用场景操练：发运前2小时的单证把关（Playwright驱动真实页面，逐步截图）。

业务剧本：
  1. 单证员上传4份PDF（发票/装箱单/运单/报关单）——单证中埋着制单错漏
  2. 检查提取预览（类型自动识别、字段置信度）
  3. 首次核验 → 高风险100：4项FAIL
  4. 制单员改单三处（毛重→件数→品名），每处实时看到风险下降
  5. 剩余唯一红灯：缺原产地证 → 必须补办才能发运
  6. 导出PDF报告存档
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8501"
PDF_DIR = Path(__file__).parent.parent / "sample_pdfs"
ART = Path(__file__).parent


def read_cards(page):
    return page.evaluate("""() => [...document.querySelectorAll('div')]
      .filter(d => (d.style.borderLeftWidth || '') === '6px')
      .map(d => { const dv = d.querySelectorAll(':scope > div');
        return (dv[0]?dv[0].textContent:'') + '=' + (dv[1]?dv[1].textContent:''); })""")


def edit_field(page, expander_text, aria_label, value):
    exp = page.locator('[data-testid="stExpander"]', has_text=expander_text)
    assert exp.count() == 1, f"展开器不唯一: {expander_text} -> {exp.count()}"
    inp = exp.locator(f'input[aria-label*="{aria_label}"]').first
    if not inp.is_visible():          # 展开器若因上次rerun保持打开，再点会折叠
        exp.locator("summary, button").first.click()
        page.wait_for_timeout(1200)
        inp = exp.locator(f'input[aria-label*="{aria_label}"]').first
    inp.fill(value)
    inp.press("Enter")
    inp.blur()
    page.wait_for_timeout(3500)


def main():
    files = [str(PDF_DIR / f) for f in
             ("invoice.pdf", "packing_list.pdf", "waybill.pdf", "customs_declaration.pdf")]
    log = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

        # 1. 上传单证
        page.get_by_text("📎 上传PDF单证").click()
        page.wait_for_timeout(1200)
        page.set_input_files('input[type="file"]', files)
        page.wait_for_selector("text=提取结果预览", timeout=30000)
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        types = body.count("单证类型：自动判断")
        log.append(f"【步骤1】4份PDF上传完毕，系统自动识别单证类型（{types}/4命中），无人工干预")
        page.screenshot(path=str(ART / "drill_1_提取预览.png"), full_page=True)

        # 2. 首次核验
        page.get_by_text("▶️ 开始核验").click()
        page.wait_for_timeout(4500)
        cards = dict(s.split("=", 1) for s in read_cards(page))
        log.append(f"【步骤2】首次核验：{cards.get('不合格 FAIL')}项不合格 / "
                   f"{cards.get('警告 WARNING')}项警告 → 风险分 {cards.get('单证组风险分（0-100）')}，红灯拦下发运")
        page.screenshot(path=str(ART / "drill_2_首次核验.png"), full_page=True)

        # 3. 改单1：装箱单毛重 12500 → 12300（衡器单复核后改正）
        edit_field(page, "装箱单 Packing List（PDF-packing_list）", "毛重", "12300")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        log.append(f"【步骤3】对照衡器单修正装箱单毛重12500→12300：FAIL {cards.get('不合格 FAIL')}项，"
                   f"风险分 {cards.get('单证组风险分（0-100）')}（实时下降）")

        # 4. 改单2：报关单件数 475 → 480
        edit_field(page, "出口报关单 Export Customs Declaration（PDF-customs_declaration）", "件数", "480")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        log.append(f"【步骤4】报关单箱数按现场计数更正475→480：FAIL {cards.get('不合格 FAIL')}项，"
                   f"风险分 {cards.get('单证组风险分（0-100）')}")

        # 5. 改单3：报关单品名与发票统一
        edit_field(page, "出口报关单 Export Customs Declaration（PDF-customs_declaration）", "货物描述", "陶瓷卫浴洁具")
        cards = dict(s.split("=", 1) for s in read_cards(page))
        body = page.inner_text("body")
        log.append(f"【步骤5】报关单品名统一为「陶瓷卫浴洁具」：FAIL {cards.get('不合格 FAIL')}项，"
                   f"风险分 {cards.get('单证组风险分（0-100）')} —— 剩余唯一红灯：缺原产地证")
        page.screenshot(path=str(ART / "drill_3_改单后.png"), full_page=True)
        assert "原产地证书" in body

        # 6. 导出PDF报告存档
        with page.expect_download(timeout=20000) as dl:
            page.get_by_role("button", name="导出PDF报告").click()
        out = ART / "drill_4_核验报告存档.pdf"
        dl.value.save_as(str(out))
        log.append(f"【步骤6】导出核验报告存档：{out.name}（{out.stat().st_size}字节）")

        browser.close()

    print("\n".join(log))


if __name__ == "__main__":
    main()
