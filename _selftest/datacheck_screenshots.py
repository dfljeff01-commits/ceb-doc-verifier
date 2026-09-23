# -*- coding: utf-8 -*-
"""数据核对模块v1 截图自测（开发期工具，汇报材料用）。

前置：`streamlit run webapp/Home.py --server.port 8501` 已在运行；
      PostgreSQL 可达（.env）。
脚本自备演示数据（脱敏：金额为夹具同源的平移值，无企业信息）：
  1. 财务演示账号 finance_demo（已存在则复用）；
  2. 按夹具写入 9 条班列 + 结算 + 补贴（7/8/9 三条联运垫付补贴100% 完全重复）；
  3. 08-09 一条补贴故意拉大三方差异（>5% 阈值 → 需人工关注）；
  4. EST-20250910 → 锁定为 20250912（预付款自动改挂）→ 台账"编号沿革"；
  5. 生成一份与库内记录冲突的导入文件（结算合计不同）。

产出（_selftest/ 下）：
  datacheck_recon_duplicates.png     三方对账：阈值告警 + 相邻重复值提示
  datacheck_ledger_est_history.png   台账详情：编号沿革 + 预付款改挂留痕
  datacheck_import_conflict.png      Excel导入：冲突差异预览
用法：python _selftest/datacheck_screenshots.py
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import sync_playwright

import auth_service
import db
import json
import train_store

BASE = "http://localhost:8502"
ART = Path(__file__).parent
FIXTURE = json.loads((PROJECT_ROOT / "sample_data" / "datacheck"
                      / "datacheck_trips_v1.json").read_text(encoding="utf-8"))


def prepare_demo() -> None:
    """写入演示数据（幂等：先清后插；金额全部来自脱敏夹具）。"""
    db.execute("DELETE FROM train_prepayments")
    db.execute("DELETE FROM train_est_numbers")
    db.execute("DELETE FROM train_settlements")
    db.execute("DELETE FROM train_subsidies")
    db.execute("DELETE FROM train_trips")
    for rec, expected in zip(FIXTURE["records"], FIXTURE["expected_numbers"]):
        train_store.create_trip(rec["dep_date"], rec["station"], rec["port"],
                                rec["dest"], rec["train_type"], "finance_demo",
                                {"goods_name": rec["goods_name"],
                                 "wagon_count": rec["wagon_count"],
                                 "container_40hd": rec["container_40hd"],
                                 "container_20hd": rec["container_20hd"],
                                 "teu_total": rec["teu_total"]})
        train_store.upsert_settlement(expected, {"settle_total": rec["settle_total"],
                                                 "actual_freight": rec["settle_total"],
                                                 "diff_reason": ""},
                                      "finance_demo")
        sub = {k: ("" if v is None else str(v))
               for k, v in rec["subsidy"].items() if k != "diff_reason"}
        train_store.upsert_subsidy(expected, sub, "finance_demo")
    # 10-03：上级拨付与测算拉大差异（>5% → 三方对账"需人工关注"）
    train_store.upsert_subsidy("20251003-PW-MZL-RU-T",
                               {"auth_confirm_100": "700000"}, "finance_demo")
    # 预付款（覆盖核对演示）：10-03 一笔预付少于结算
    train_store.add_prepayment("20251003-PW-MZL-RU-T", "2025-09-28",
                               "2500000", "首笔预付", "finance_demo")
    # EST 生命周期演示：预估登记 → 预付款挂预估编号 → 锁定改挂
    train_store.create_est("2026-09-10", "PW", "MZL", "RU", "T", "finance_demo")
    train_store.add_prepayment("EST-20260910-PW-MZL-RU-T", "2026-09-01",
                               "800000", "按预估编号预付", "finance_demo")
    train_store.lock_est("EST-20260910-PW-MZL-RU-T", "2026-09-15", "finance_demo")
    # 存疑标记演示（10-10图）
    train_store.mark_suspect("20251010-PW-MZL-RU-T", "suspect",
                             "与相邻记录数值完全相同，疑似测算表复制未更新",
                             "finance_demo")
    train_store.refresh_anomalies("finance_demo")


def build_conflict_import() -> Path:
    """生成与库内 20251004 记录冲突的导入文件（结算合计不同）。"""
    from openpyxl import Workbook
    import datacheck_import as imp
    cols = imp.TRIP_COLUMNS
    wb = Workbook()
    ws = wb.active
    ws.append([c[1] for c in cols])
    ws.append([c[2] for c in cols])
    ws.append(["2025-10-04", "平旺", "满洲里", "俄罗斯", "T", "汽车零配件",
               55, 27, 55, 109, "", "", "", "", "2799999.00", "", "", ""])
    buf = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(buf.name)
    return Path(buf.name)


def login(page) -> None:
    """登录（登录态存在 Streamlit 内存会话中——绝不能再整页 goto，否则丢会话）。"""
    page.goto(f"{BASE}/data-check", wait_until="networkidle")
    page.wait_for_timeout(3000)
    user_input = page.locator('input[aria-label="用户名"]')
    if user_input.count():                       # 未登录 → 登录后原地重渲染
        user_input.fill("finance_demo")
        page.locator('input[aria-label="密码"]').fill("DataCheck1")
        page.locator('button[kind="primaryFormSubmit"]').click()
        page.wait_for_timeout(5000)
        assert page.locator('input[aria-label="用户名"]').count() == 0, \
            "登录未生效（检查演示账号 finance_demo 是否存在/密码是否正确）"
    # 登录后落在首页：点侧栏导航进入数据核对（客户端切换，会话不丢）
    if page.locator('[role="tab"]').count() == 0:
        page.locator('[data-testid="stSidebarNav"] a', has_text="数据核对").click()
        page.wait_for_timeout(5000)
    assert page.locator('[role="tab"]').count() >= 5, "数据核对页签未渲染"


def click_tab(page, name: str) -> None:
    """Streamlit 1.64 的 st.tabs 渲染为 [role="tab"]（非 data-baseweb）。"""
    page.locator('[role="tab"]', has_text=name).first.click()
    page.wait_for_timeout(2500)


def select_trip(page, trip_no: str) -> None:
    """台账页 selectbox 选中指定班列（st.tabs 会渲染全部页签的 DOM，
    必须用 :visible 过滤出当前可见的下拉框，否则点到隐藏页签的元素）。"""
    page.locator('[data-testid="stSelectbox"] button:visible').first.click()
    page.wait_for_timeout(1000)
    page.locator('[role="option"]:visible', has_text=trip_no).first.click()
    page.wait_for_timeout(2500)


def main() -> int:
    try:
        auth_service.create_user("finance_demo", "DataCheck1", "finance")
    except auth_service.AuthError:
        pass                                      # 已存在则复用
    prepare_demo()
    conflict_file = build_conflict_import()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        login(page)

        # ---- 截图1：三方对账（阈值告警 + 重复值提示） ----
        click_tab(page, "三方对账")
        # 等数据真正渲染出来（"当前阈值"是取数完成后才出现的说明行）
        page.get_by_text("当前阈值").first.wait_for(timeout=20000)
        page.wait_for_timeout(1500)
        anchor = page.get_by_text("需人工关注的差异明细")
        if anchor.count():
            anchor.first.scroll_into_view_if_needed()
        else:
            page.get_by_text("当前阈值").first.scroll_into_view_if_needed()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(ART / "datacheck_recon_duplicates.png"))
        print("✅ datacheck_recon_duplicates.png")

        # ---- 截图2：台账详情（编号沿革 + 预付款改挂留痕） ----
        click_tab(page, "班列台账")
        select_trip(page, "20260915-PW-MZL-RU-T")
        # 编号沿革区块只在存在映射留痕时渲染——等待即验证
        page.get_by_text("编号沿革").first.wait_for(timeout=20000)
        page.get_by_text("编号沿革").first.scroll_into_view_if_needed()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(ART / "datacheck_ledger_est_history.png"))
        print("✅ datacheck_ledger_est_history.png")

        # ---- 截图3：Excel导入冲突预览 ----
        click_tab(page, "Excel导入")
        page.set_input_files('input[type="file"]', str(conflict_file))
        # 按钮在上传/解析期间为禁用态——轮询等待可点击
        btn = page.locator('button:has-text("解析并预览差异")').first
        for _ in range(30):
            if btn.is_enabled():
                break
            page.wait_for_timeout(1000)
        btn.click()
        # 等汇总行渲染（"共 N 行：…" st.info）
        page.locator('[data-testid="stAlert"]', has_text="共").first \
            .wait_for(timeout=30000, state="visible")
        # 点开"冲突"行的折叠区（差异表为 canvas 渲染，等待其表头进入 DOM 即可）
        conflict_header = page.locator("summary", has_text="冲突").first
        conflict_header.wait_for(timeout=15000, state="visible")
        conflict_header.click()
        page.wait_for_timeout(3000)
        conflict_header.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)
        page.screenshot(path=str(ART / "datacheck_import_conflict.png"))
        print("✅ datacheck_import_conflict.png")

        browser.close()
    conflict_file.unlink(missing_ok=True)
    print("截图完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
