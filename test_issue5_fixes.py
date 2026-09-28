# -*- coding: utf-8 -*-
"""Issue #5 三项修复的回归测试。

Bug1 切页状态重置：根因是 Streamlit 在组件卸载（切页）时会清理组件状态
     （含显式key，1.64实测）——修复口径：来源选择/向导组件值持久化到普通
     session键（普通键不受清理影响），第2步文件列表以 wiz_files 为准渲染。
Bug2 开发态信息泄漏：AST 扫描所有页面 st.* 渲染调用里的字符串字面量，
     禁止出现任务书/启动命令/本机地址/内部文档引用等开发态内容。
Bug3 三方对账空数据假绿勾：三方金额未录入齐全时必须给中性提示，
     不给任何 ✅/通过类图标（空数据≠核对通过）。
运行：pytest test_issue5_fixes.py -v
"""

import ast
import sys
from pathlib import Path

import pytest

_PROJECT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT))

import upload_wizard  # noqa: E402
import webapp.doc_verify as dv  # noqa: E402
from webapp import data_check  # noqa: E402


# ---------------------------------------------------------------------------
# Bug1：切页后上传模式/向导进度被重置
# ---------------------------------------------------------------------------

_RADIO_PROBE = '''
import sys
sys.path.insert(0, r"{project}")
import streamlit as st
import webapp.doc_verify as dv

if st.session_state.get("page") == "other":
    st.write("OTHER_PAGE")          # 模拟切到别的页：来源单选组件被卸载
else:
    st.session_state["seen_mode"] = dv._source_mode_radio()
'''.format(project=str(_PROJECT))


def test_widget_keyed_state_is_cleaned_on_unmount():
    """根因留痕：显式key的radio在"卸载→重挂"后会被Streamlit重置为默认值。
    若未来Streamlit版本不再如此，本修复的镜像机制仍正确（幂等），但可考虑简化。"""
    from streamlit.testing.v1 import AppTest

    probe = '''
import streamlit as st
render = st.session_state.get("render", True)
if render:
    st.radio("模式", ["A", "B"], index=0, key="mode_keyed")
'''
    at = AppTest.from_string(probe)
    at.run()
    at.radio(key="mode_keyed").set_value("B").run()
    at.session_state["render"] = False       # 切页：组件卸载
    at.run()
    at.session_state["render"] = True        # 切回：组件重挂
    at.run()
    assert at.session_state["mode_keyed"] == "A"   # 实测被重置（Streamlit 1.64）


def test_source_mode_survives_page_switch():
    """修复验证：来源选择持久化在普通session键，切页往返后不丢。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_RADIO_PROBE)
    at.run()
    at.radio[0].set_value(dv.SOURCE_UPLOAD).run()
    assert at.session_state["seen_mode"] == dv.SOURCE_UPLOAD
    assert at.session_state[dv.SOURCE_MODE_STATE_KEY] == dv.SOURCE_UPLOAD

    at.session_state["page"] = "other"       # 切页：单选组件被卸载
    at.run()
    at.session_state["page"] = "doc"         # 切回：重挂
    at.run()
    assert at.session_state["seen_mode"] == dv.SOURCE_UPLOAD, \
        "切页返回后来源选择必须保留（Issue #5 Bug1）"


def test_source_mode_source_contract():
    """防回归：来源单选不得再挂组件key（必须走普通键同步）。"""
    src = (_PROJECT / "webapp" / "doc_verify.py").read_text(encoding="utf-8")
    assert 'key="source_mode"' not in src
    assert "SOURCE_MODE_STATE_KEY" in src
    assert "st.session_state[SOURCE_MODE_STATE_KEY] = chosen" in src


_WIZARD_PROBE = '''
import sys
sys.path.insert(0, r"{project}")
import streamlit as st
import upload_wizard

def noop_editor(doc, mode=0):
    return doc, 0

if st.session_state.get("page") == "other":
    st.write("OTHER_PAGE")          # 模拟切到别的页：向导整体卸载
else:
    upload_wizard.render_upload_wizard(noop_editor)
'''.format(project=str(_PROJECT))


@pytest.fixture()
def wizard_app(tmp_path):
    from streamlit.testing.v1 import AppTest

    pdf_bytes = (_PROJECT / "sample_pdfs" / "invoice.pdf").read_bytes()
    at = AppTest.from_string(_WIZARD_PROBE)
    at.run()                                  # 第1步
    for t in upload_wizard.COMPOSITION_ORDER:
        at.number_input(key=f"wiz_comp::{t}").set_value(
            1 if t == "invoice" else 0)
    at.run()
    at.button(key="wiz_to_step2").click().run()
    at.file_uploader(key="wiz_up::invoice").set_value(
        [("invoice.pdf", pdf_bytes, "application/pdf")])
    at.run()                                  # 解析入 wiz_files
    return at, upload_wizard._file_sig("invoice.pdf", pdf_bytes)


def test_wizard_files_survive_page_switch(wizard_app):
    """核心复现路径：上传文件→切页→切回，文件必须还在且仍展示。"""
    at, sig = wizard_app
    assert sig in at.session_state["wiz_files"]
    assert at.session_state["wiz_step"] == 2

    at.session_state["page"] = "other"        # 切页：向导卸载、上传控件值被清理
    at.run()
    at.session_state["page"] = "doc"          # 切回
    at.run()

    assert sig in at.session_state["wiz_files"], \
        "切页往返后已上传文件不得被清掉（Issue #5 Bug1）"
    assert at.session_state["wiz_step"] == 2
    labels = "\n".join(str(e.label) for e in at.expander)
    assert "invoice.pdf" in labels, "切回后文件列表必须仍展示已上传文件"


def test_wizard_composition_survives_page_switch(wizard_app):
    """第1步申报构成：切页往返后回到第1步，申报不得被清零（普通键回填）。"""
    at, _ = wizard_app
    at.session_state["page"] = "other"
    at.run()
    at.session_state["page"] = "doc"
    at.run()
    # 用户切回后点"← 上一步（修改申报构成）"回到第1步
    at.button(key="wiz_back1").click().run()
    assert int(at.session_state["wiz_comp::invoice"]) == 1, \
        "切页往返后申报份数不得回落为0（Issue #5 Bug1）"
    assert at.session_state["wiz_composition"]["invoice"] == 1


def test_wizard_remove_button_removes_file(wizard_app):
    """移除按钮：显式移除后文件不再渲染，也不因控件仍选中而被自动加回。"""
    at, sig = wizard_app
    at.button(key=f"wiz_rm::{sig}").click().run()
    assert sig not in at.session_state["wiz_files"]
    assert sig in at.session_state["wiz_removed"]
    at.run()                                  # 控件里文件仍在选中态
    assert sig not in at.session_state["wiz_files"], \
        "显式移除后不得被上传控件自动加回"


# ---------------------------------------------------------------------------
# Bug2：开发态/内部信息泄漏到业务员可见页面
# ---------------------------------------------------------------------------

#: 禁止出现在任何 st.* 渲染调用字符串字面量里的开发态/内部内容
_FORBIDDEN_TOKENS = [
    "任务书", "uvicorn", "localhost", "INSTALL.md", "serve_apk",
    "FastAPI", "PostgreSQL", "Swagger", "API文档", "健康检查",
    "前后端分离", "进程内直连", "启动后端", "同一台后端", "v2任务书",
]

#: 视为"用户可见文本输出"的 st 方法（渲染字符串即上屏）
_RENDER_FUNCS = {
    "title", "header", "subheader", "markdown", "caption", "info", "success",
    "warning", "error", "radio", "checkbox", "button", "form_submit_button",
    "download_button", "selectbox", "multiselect", "text_input", "text_area",
    "number_input", "date_input", "time_input", "file_uploader", "tabs",
    "expander", "code", "write", "metric", "page_link", "toggle",
    "link_button", "camera_input",
}


def _iter_render_strings(tree: ast.AST):
    """收集 st.<render_func>(...) 调用内的全部字符串常量（含f-string片段与关键字实参）。"""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "st" and node.func.attr in _RENDER_FUNCS):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield sub.value


def test_no_dev_info_leak_in_user_visible_strings():
    """全部 webapp 页面的渲染字符串中不得出现开发态/内部内容（Issue #5 Bug2）。"""
    webapp_dir = _PROJECT / "webapp"
    offenders = []
    for py in sorted(webapp_dir.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for text in _iter_render_strings(tree):
            for token in _FORBIDDEN_TOKENS:
                if token in text:
                    offenders.append(f"{py.name}: 含'{token}' → {text[:60]!r}")
    assert not offenders, "以下用户可见文本泄漏开发态信息：\n" + "\n".join(offenders)


def test_page_source_css_comment_no_internal_ref():
    """APP_CSS 会被注入页面源码，其中也不得引用内部任务书表述。"""
    src = (_PROJECT / "webapp" / "styles.py").read_text(encoding="utf-8")
    assert "任务书" not in src


# ---------------------------------------------------------------------------
# Bug3：三方对账空数据显示绿勾
# ---------------------------------------------------------------------------

def test_threeway_status_neutral_when_data_missing():
    """三方金额缺任一项 → 中性提示，绝无 ✅/❌（空数据≠核对通过）。"""
    empty = {"trip_no": "T1"}
    status = data_check._threeway_status(empty)
    assert "✅" not in status and "❌" not in status
    assert "尚未录入" in status

    partial = {"dt_supply_100": 100, "ly_advance_100": 100,
               "auth_confirm_100": None}
    assert "✅" not in data_check._threeway_status(partial)


def test_threeway_status_pass_and_flag():
    """三方齐全：相等→✅；超阈值（flag）→❌ 需人工关注。"""
    ok = {"dt_supply_100": 100, "ly_advance_100": 100,
          "auth_confirm_100": 100, "three_way": {"flag": False}}
    assert data_check._threeway_status(ok) == "✅"
    bad = {"dt_supply_100": 100, "ly_advance_100": 100,
           "auth_confirm_100": 999, "three_way": {"flag": True}}
    assert data_check._threeway_status(bad) == "❌ 需人工关注"


def test_pair_icon_neutral_when_value_missing():
    """差异明细对：缺值给中性占位，不打 ✅（原实现缺值也打绿勾）。"""
    assert data_check._pair_icon({"a_value": None, "b_value": 1,
                                  "flag": False}) == "—"
    assert data_check._pair_icon({"a_value": 1, "b_value": 1,
                                  "flag": False}) == "✅"
    assert data_check._pair_icon({"a_value": 1, "b_value": 2,
                                  "flag": True}) == "❌"


def test_threeway_end_to_end_with_engine_empty_row():
    """与核对引擎串联：真实 three_way_check 的空行 → UI状态必须是中性。"""
    from train_recon import three_way_check

    row = {"trip_no": "T1", "dep_date": "2026-09-28"}   # 三方金额全空
    t = three_way_check(row)
    assert t.get("flag") is False                        # 引擎不标差异
    status = data_check._threeway_status({**row, "three_way": t})
    assert "✅" not in status and "尚未录入" in status


def test_threeway_source_contract():
    """防回归：状态列必须经 _threeway_status 判定，不允许内联'非flag即✅'。"""
    src = (_PROJECT / "webapp" / "data_check.py").read_text(encoding="utf-8")
    assert '"三方一致": _threeway_status(t)' in src
    assert 'else "✅"' not in src.split("def _tab_recon")[1].split("def ")[0]
    assert '"重复值提示": "⚠️ 有" if t["trip_no"] in dup else "—"' in src
