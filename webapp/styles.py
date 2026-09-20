# -*- coding: utf-8 -*-
"""webapp 各页面共用的自定义样式。"""

APP_CSS = """
<style>
/* 来源选择：radio 卡片化（P0 首屏操作入口） */
div[data-testid="stRadio"] label {
    border: 1.5px solid #E5E7EB; border-radius: 12px; padding: 12px 16px;
    background: #FFFFFF; margin: 2px 0; transition: all .12s ease; cursor: pointer;
}
div[data-testid="stRadio"] label:hover { border-color: #94A3B8; }
div[data-testid="stRadio"] label:has(input:checked) {
    border: 2px solid #0B5394; background: #EFF6FF;
}
/* 来源选择按钮组（与radio等价的备选形态） */
div[data-testid="stButton"] > button { border-radius: 10px; }
/* 步骤指示器 */
.ceb-step { display:flex; gap:6px; margin:2px 0 14px; flex-wrap:wrap; }
.ceb-step span {
    display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    padding:6px 13px; border-radius:999px; border:1px solid #E5E7EB;
    color:#6B7280; background:#F9FAFB; font-weight:600;
}
.ceb-step span.done { color:#1B5E20; background:#E8F5E9; border-color:#A5D6A7; }
.ceb-step span.cur { color:#0B5394; background:#EFF6FF; border-color:#0B5394;
    box-shadow:0 1px 6px #0B539433; }
/* 问题卡片（FAIL/WARNING） */
.ceb-problem { border-radius:12px; padding:12px 16px; margin:10px 0;
    border-left:6px solid; }
.ceb-problem h4 { margin:0 0 6px 0; font-size:15px; }
.ceb-problem .d { font-size:13.5px; color:#374151; margin:2px 0; }
.ceb-problem .s { font-size:13px; color:#374151; background:#FFFFFFCC;
    border-radius:8px; padding:8px 12px; margin-top:8px; }
/* 通用小卡片 */
.ceb-mini { border-radius:10px; padding:10px 14px; text-align:center; }
.ceb-mini .v { font-size:26px; font-weight:800; line-height:1.2; }
.ceb-mini .k { font-size:12px; color:#6B7280; font-weight:600; }
/* 隐藏Streamlit自带的开发者工具栏/Deploy按钮（任务书问题五；
   与 .streamlit/config.toml 的 toolbarMode="viewer" 双保险） */
[data-testid="stToolbar"] { display: none !important; }
[data-testid="stStatusWidget"] { display: none !important; }
/* 按单据分组的分层结果卡片（任务书问题四） */
.ceb-docgroup { border:1px solid #E5E7EB; border-radius:12px; padding:10px 14px;
    margin:8px 0; background:#FFFFFF; }
.ceb-docgroup .hd { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.ceb-docgroup .badge { font-size:12px; font-weight:700; border-radius:999px;
    padding:2px 10px; border:1px solid; }
/* 登录页 */
.ceb-login-box { max-width:420px; margin:6vh auto 0; }
.ceb-entry-card { border:1.5px solid #E5E7EB; border-radius:16px; padding:22px 24px;
    background:#FFFFFF; height:100%; }
.ceb-entry-card .t { font-size:20px; font-weight:800; margin:4px 0 8px; }
.ceb-entry-card .d { font-size:13.5px; color:#6B7280; line-height:1.65; }
</style>
"""
