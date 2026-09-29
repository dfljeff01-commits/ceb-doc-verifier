# -*- coding: utf-8 -*-
"""webapp 各页面共用的自定义样式。"""

from urllib.parse import quote as _urlquote

APP_CSS = """
<style>
/* 品牌色板（P1-2）：全站强调色只有一处来源，与 config.toml 的 [theme] 一致 */
:root {
    --ceb-brand: #0B5394;      /* 主强调色：主按钮/选中态/链接 */
    --ceb-brand-soft: #EFF6FF;  /* 强调色浅底（选中卡片背景） */
    --ceb-muted: #6B7280;       /* 次要文字（对白底 4.8:1，满足 WCAG AA） */
    --ceb-border: #E5E7EB;      /* 常规描边 */
}
/* 来源选择：radio 卡片化（P0 首屏操作入口） */
div[data-testid="stRadio"] label {
    border: 1.5px solid var(--ceb-border); border-radius: 12px; padding: 12px 16px;
    background: #FFFFFF; margin: 2px 0; transition: all .12s ease; cursor: pointer;
}
div[data-testid="stRadio"] label:hover { border-color: var(--ceb-muted); }
div[data-testid="stRadio"] label:has(input:checked) {
    border: 2px solid var(--ceb-brand); background: var(--ceb-brand-soft);
}
/* 来源选择按钮组（与radio等价的备选形态） */
div[data-testid="stButton"] > button { border-radius: 10px; }
/* 步骤指示器 */
.ceb-step { display:flex; gap:6px; margin:2px 0 14px; flex-wrap:wrap; }
.ceb-step span {
    display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    padding:6px 13px; border-radius:999px; border:1px solid var(--ceb-border);
    color:var(--ceb-muted); background:#F9FAFB; font-weight:600;
}
.ceb-step span.done { color:#1B5E20; background:#E8F5E9; border-color:#A5D6A7; }
.ceb-step span.cur { color:var(--ceb-brand); background:var(--ceb-brand-soft);
    border-color:var(--ceb-brand);
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
/* 隐藏Streamlit自带的开发者工具栏/Deploy按钮（与 .streamlit/config.toml 的
   toolbarMode="viewer" 双保险） */
[data-testid="stToolbar"] { display: none !important; }
[data-testid="stStatusWidget"] { display: none !important; }
/* 按单据分组的分层结果卡片 */
.ceb-docgroup { border:1px solid #E5E7EB; border-radius:12px; padding:10px 14px;
    margin:8px 0; background:#FFFFFF; }
.ceb-docgroup .hd { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.ceb-docgroup .badge { font-size:12px; font-weight:700; border-radius:999px;
    padding:2px 10px; border:1px solid; }

/* ============ 引导式入口（卡片即按钮，单击直达，Issue #6） ============ */
/* 入口层标记 .ceb-cards-anchor 所在页面：全部 stButton 呈现为整张大卡片，
   标题行加粗放大、说明行弱化，整张卡片都可点击，不再"卡片展示+另找按钮"。 */
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        [data-testid="stButton"] > button {
    width: 100%; min-height: 132px; padding: 18px 22px; text-align: left;
    white-space: normal; line-height: 1.55;
    border: 1.5px solid var(--ceb-border); border-radius: 16px; background: #FFFFFF;
    transition: border-color .12s ease, box-shadow .12s ease;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        [data-testid="stButton"] > button:hover {
    border-color: var(--ceb-brand); box-shadow: 0 3px 14px #0B539426;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        [data-testid="stButton"] > button p:first-of-type {
    font-size: 17.5px; font-weight: 800; color: #111827; margin: 0 0 6px;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        [data-testid="stButton"] > button p:last-of-type {
    font-size: 13px; font-weight: 400; color: var(--ceb-muted); margin: 0;
}
/* 按钮标签内层 p 自带 nowrap+省略号（单行设计），且被置为 display:inline，
   卡片化必须恢复换行与块级堆叠，否则"标题+说明"两行文案会被截断/挤成一行 */
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        [data-testid="stButton"] > button p {
    display: block !important;
    white-space: normal !important; overflow: visible !important;
    text-overflow: clip !important; text-align: left !important;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"] p {
    display: block !important;
}
/* 首页模块大卡：page_link 整卡可点（单元素即卡片，无第二跳） */
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"] {
    display: block; min-height: 132px; padding: 18px 22px; text-decoration: none;
    border: 1.5px solid var(--ceb-border); border-radius: 16px; background: #FFFFFF;
    transition: border-color .12s ease, box-shadow .12s ease;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"]:hover {
    border-color: var(--ceb-brand); box-shadow: 0 3px 14px #0B539426;
}
/* page_link 默认按单行链接设计（不换行+省略号），卡片化必须恢复自动换行 */
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"] * {
    white-space: normal !important; overflow: visible !important; text-overflow: clip !important;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"] p:first-of-type {
    font-size: 17.5px; font-weight: 800; color: #111827; margin: 0 0 6px;
}
div[data-testid="stVerticalBlock"]:has(.ceb-cards-anchor)
        a[data-testid="stPageLink-NavLink"] p:last-of-type {
    font-size: 13px; color: var(--ceb-muted); margin: 0;
}
/* 次要入口（查看示例、管理员快捷入口等）：在卡片层中明显弱化。
   结构约定：次要项放在 st.container(border=True) 内（外壳边框用CSS隐藏），
   选择器只命中该容器后代，避免 :has 经公共祖先泄漏到主卡片。 */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ceb-secondary-anchor) {
    border: none !important; padding: 0 !important;
    background: transparent !important; box-shadow: none !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ceb-secondary-anchor)
        [data-testid="stButton"] > button,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ceb-secondary-anchor)
        a[data-testid="stPageLink-NavLink"] {
    min-height: 0 !important; padding: 10px 18px !important;
    background: #F9FAFB !important; border-style: dashed !important;
    box-shadow: none !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ceb-secondary-anchor)
        [data-testid="stButton"] > button p,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ceb-secondary-anchor)
        a[data-testid="stPageLink-NavLink"] p {
    font-size: 14px !important; font-weight: 600 !important;
    color: var(--ceb-muted) !important; margin: 0 !important;
}
/* 不可用的模块入口（灰态展示卡，明确不可点） */
.ceb-entry-card-disabled { border:1.5px dashed var(--ceb-border); border-radius:16px;
    padding:22px 24px; background:#F9FAFB; height:100%; opacity:.75; }
.ceb-entry-card-disabled .t { font-size:18px; font-weight:800; margin:4px 0 8px;
    color:#374151; }
.ceb-entry-card-disabled .d { font-size:13px; color:var(--ceb-muted); line-height:1.6; }
</style>
"""


# ---------------------------------------------------------------- 登录页专属样式
# 自绘矢量底图：中欧班列集装箱班列 + 铁轨 + 班列路线（发站→口岸→目的地）。
# 内网离线可用，不引用外部图片；赞助商审定实拍照片后，替换下方
# background-image 一处即可切换，不影响版式结构。
_LOGIN_BRAND_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900" preserveAspectRatio="xMidYMax slice">
<defs>
<radialGradient id="glow" cx="0.18" cy="0.16" r="0.55">
<stop offset="0" stop-color="#7FB2E8" stop-opacity="0.38"/>
<stop offset="1" stop-color="#7FB2E8" stop-opacity="0"/>
</radialGradient>
</defs>
<rect width="1200" height="900" fill="url(#glow)"/>
<path d="M-60 700 L 300 430 L 660 700 Z" fill="#0A3A6E" opacity="0.35"/>
<path d="M420 700 L 820 400 L 1260 700 Z" fill="#0A3A6E" opacity="0.28"/>
<path d="M90 460 C 420 325, 780 315, 1110 445" fill="none" stroke="#8FBDF0" stroke-width="3" stroke-linecap="round" stroke-dasharray="1 16" opacity="0.8"/>
<circle cx="90" cy="460" r="9" fill="#FFD166"/>
<circle cx="600" cy="346" r="7" fill="#B7D6F7"/>
<circle cx="1110" cy="445" r="9" fill="#FFD166"/>
<g stroke="#3B6CA3" stroke-width="5" opacity="0.85">
<line x1="40" y1="762" x2="40" y2="782"/><line x1="86" y1="762" x2="86" y2="782"/>
<line x1="132" y1="762" x2="132" y2="782"/><line x1="178" y1="762" x2="178" y2="782"/>
<line x1="224" y1="762" x2="224" y2="782"/><line x1="270" y1="762" x2="270" y2="782"/>
<line x1="316" y1="762" x2="316" y2="782"/><line x1="362" y1="762" x2="362" y2="782"/>
<line x1="408" y1="762" x2="408" y2="782"/><line x1="454" y1="762" x2="454" y2="782"/>
<line x1="500" y1="762" x2="500" y2="782"/><line x1="546" y1="762" x2="546" y2="782"/>
<line x1="592" y1="762" x2="592" y2="782"/><line x1="638" y1="762" x2="638" y2="782"/>
<line x1="684" y1="762" x2="684" y2="782"/><line x1="730" y1="762" x2="730" y2="782"/>
<line x1="776" y1="762" x2="776" y2="782"/><line x1="822" y1="762" x2="822" y2="782"/>
<line x1="868" y1="762" x2="868" y2="782"/><line x1="914" y1="762" x2="914" y2="782"/>
<line x1="960" y1="762" x2="960" y2="782"/><line x1="1006" y1="762" x2="1006" y2="782"/>
<line x1="1052" y1="762" x2="1052" y2="782"/><line x1="1098" y1="762" x2="1098" y2="782"/>
<line x1="1144" y1="762" x2="1144" y2="782"/>
</g>
<line x1="0" y1="760" x2="1200" y2="760" stroke="#6FA5DC" stroke-width="6"/>
<line x1="0" y1="784" x2="1200" y2="784" stroke="#4E82BE" stroke-width="5"/>
<g>
<rect x="120" y="700" width="170" height="16" rx="5" fill="#082B4F"/>
<rect x="130" y="656" width="72" height="44" rx="3" fill="#1773B8"/>
<rect x="208" y="656" width="72" height="44" rx="3" fill="#12568F"/>
<rect x="130" y="612" width="72" height="40" rx="3" fill="#1B84D6"/>
<circle cx="150" cy="726" r="13" fill="#082B4F"/><circle cx="262" cy="726" r="13" fill="#082B4F"/>
<rect x="310" y="700" width="170" height="16" rx="5" fill="#082B4F"/>
<rect x="320" y="656" width="72" height="44" rx="3" fill="#1B84D6"/>
<rect x="398" y="656" width="72" height="44" rx="3" fill="#1773B8"/>
<rect x="398" y="612" width="72" height="40" rx="3" fill="#E8913A"/>
<circle cx="340" cy="726" r="13" fill="#082B4F"/><circle cx="452" cy="726" r="13" fill="#082B4F"/>
<rect x="500" y="700" width="170" height="16" rx="5" fill="#082B4F"/>
<rect x="510" y="656" width="72" height="44" rx="3" fill="#12568F"/>
<rect x="588" y="656" width="72" height="44" rx="3" fill="#1B84D6"/>
<circle cx="530" cy="726" r="13" fill="#082B4F"/><circle cx="642" cy="726" r="13" fill="#082B4F"/>
<rect x="690" y="700" width="170" height="16" rx="5" fill="#082B4F"/>
<rect x="700" y="656" width="72" height="44" rx="3" fill="#1773B8"/>
<rect x="778" y="656" width="72" height="44" rx="3" fill="#12568F"/>
<rect x="700" y="612" width="72" height="40" rx="3" fill="#1773B8"/>
<circle cx="720" cy="726" r="13" fill="#082B4F"/><circle cx="832" cy="726" r="13" fill="#082B4F"/>
<rect x="890" y="648" width="196" height="68" rx="10" fill="#0F4C86"/>
<rect x="996" y="606" width="96" height="52" rx="9" fill="#0F4C86"/>
<rect x="1010" y="618" width="58" height="28" rx="4" fill="#BFE0FA"/>
<rect x="902" y="660" width="70" height="10" rx="5" fill="#BFE0FA" opacity="0.5"/>
<circle cx="930" cy="728" r="14" fill="#082B4F"/><circle cx="1044" cy="728" r="14" fill="#082B4F"/>
<circle cx="1084" cy="688" r="6" fill="#FFD166"/>
</g>
<g>
<line x1="1120" y1="760" x2="1120" y2="560" stroke="#3B6CA3" stroke-width="6"/>
<line x1="1120" y1="580" x2="1050" y2="580" stroke="#3B6CA3" stroke-width="5"/>
<circle cx="1050" cy="580" r="8" fill="#3ECF8E"/>
</g>
</svg>"""

LOGIN_CSS = f"""
<style>
/* 登录页专属（仅未登录时注入）：左右分栏 = 品牌区 + 登录表单（Issue #6）。
   注意：不隐藏侧边栏——隐藏后没有任何"展开侧边栏"的可见入口，
   业务员会不知道点哪里；登录前侧栏本来就只有"登录"一项（P2-9 口径）。 */
[data-testid="stAppViewContainer"] .main .block-container {{
    padding-top: 3vh; max-width: 1200px;
}}
.ceb-brand-panel {{
    position: relative; overflow: hidden;
    min-height: 82vh; border-radius: 20px; padding: 52px 46px;
    background-color: #083A6B;
    background-image:
        linear-gradient(160deg, #0D5BAE 0%, #0B5394 38%, #083A6B 74%, #06294D 100%);
    background-size: cover; background-position: center;
    color: #FFFFFF; display: flex; flex-direction: column; justify-content: space-between;
}}
.ceb-brand-panel::before {{
    content: ""; position: absolute; inset: 0;
    background-image: url("data:image/svg+xml,{_urlquote(_LOGIN_BRAND_SVG)}");
    background-size: cover; background-position: center bottom;
}}
.ceb-brand-panel > * {{ position: relative; z-index: 1; }}
.ceb-brand-panel .co {{
    font-size: 32px; font-weight: 900; letter-spacing: 1.5px; line-height: 1.45;
    text-shadow: 0 2px 14px rgba(0, 0, 0, .45);
}}
.ceb-brand-panel .sys {{
    font-size: 18px; font-weight: 700; margin-top: 12px;
    color: #DCEBFA; text-shadow: 0 1px 8px rgba(0, 0, 0, .4);
}}
.ceb-brand-panel .tag {{
    font-size: 13.5px; margin-top: 8px; color: #B9D4F0; line-height: 1.7;
}}
.ceb-brand-panel .foot {{ font-size: 12.5px; color: #9FC0E4; }}
div[data-testid="column"]:has(.ceb-login-form-anchor) {{
    background: #FFFFFF; border: 1px solid var(--ceb-border); border-radius: 20px;
    box-shadow: 0 8px 34px #0B53941F; padding: 42px 38px; align-self: center;
}}
div[data-testid="column"]:has(.ceb-login-form-anchor) h3 {{ margin-top: 0; }}
@media (max-width: 900px) {{
    .ceb-brand-panel {{ min-height: 300px; padding: 30px 26px; }}
    .ceb-brand-panel .co {{ font-size: 24px; }}
}}
</style>
"""
