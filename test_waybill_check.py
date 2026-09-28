# -*- coding: utf-8 -*-
"""运单核对模块测试（Issue #4 任务二：车底号/铅封号核对）。

合成夹具按真实订车系统版式复刻关键几何（第7/8栏同词干扰、第19栏
"数量|记号"子栏结构、散布的6位干扰数字），坐标基准取自真实样本
TKRU4625794-260925-102205 的实测值。

真实受控样本回归（任务书硬性验收：260281 vs 错误的261918）使用
sample_pdfs/_real/ 下的受控副本，缺失时跳过（含客户敏感信息，绝不入库）。
运行：pytest test_waybill_check.py -v
"""

import io
from pathlib import Path

import pytest

import waybill_extract as we

_PROJECT = Path(__file__).parent
_REAL_DIR = _PROJECT / "sample_pdfs" / "_real"

# 真实版式关键坐标（pdfplumber top坐标系，实测自 TKRU4625794-260925-102205）
_PAGE_H = 841.92


def _rl():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas as rl_canvas
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    return c, buf


def _draw(c, x: float, top: float, text: str, size: float = 7.6):
    """按"距页顶距离"落笔（与实测坐标同口径），baseline≈top+字高。"""
    c.setFont("STSong-Light", size)
    c.drawString(x, _PAGE_H - top - size, text)


def build_waybill(*, wagon="5480825", seal="260281",
                  stream_decoy="135790", col8_decoy="987654",
                  with_col7_label=True, with_col19_label=True,
                  container="TKRU4625794") -> bytes:
    """复刻真实版式：第7栏(左)与第8栏(右)同排、第19栏"数量|记号"子栏、
    底部集装箱信息块；stream_decoy 在文本流中先于铅封号出现（页眉区），
    col8_decoy 落在第8栏框内（文本流顺序法的高危干扰源）。"""
    c, buf = _rl()

    # 文本流最前：页眉区6位干扰数字（车站代码样式）
    _draw(c, 100, 45, f"运单号 {stream_decoy}", 8)

    # 第7/8栏表头行（top≈226）与上方第8栏续行（top≈209）
    if with_col7_label:
        _draw(c, 202.7, 226.2, "7")
        _draw(c, 210.3, 225.9, "车辆—Вагон")
    _draw(c, 326.5, 209.5, "车辆由何方提供—Вагон предоставлен")
    _draw(c, 328.3, 226.2, "8")

    # 第7/8栏框线与取值（col7: x 196..326.5；col8: x 326.5..456.5）
    c.rect(196, _PAGE_H - 320, 130.5, 87)
    c.rect(326.5, _PAGE_H - 320, 130, 87)
    if col8_decoy:
        _draw(c, 330, 248, col8_decoy, 8)
    if with_col7_label and wagon:
        _draw(c, 202, 295, wagon, 8)

    # 第19栏（top 350..399）：外框 + 数量|记号 子栏分界线
    if with_col19_label:
        _draw(c, 458, 351.4, "19")
        _draw(c, 469.3, 351.1, "封印—Пломбы")
    c.rect(452.4, _PAGE_H - 399.2, 128.7, 49.2)
    c.line(477, _PAGE_H - 399.2, 477, _PAGE_H - 360.2)
    for y_top in (360.2, 374.9, 386.9):
        c.line(452.4, _PAGE_H - y_top, 581.1, _PAGE_H - y_top)
    if with_col19_label:
        _draw(c, 458.6, 360.3, "数量")
        _draw(c, 458.7, 366.8, "К-во")
        _draw(c, 509, 361.8, "记号—знаки")
    _draw(c, 457, 378, "1", 8)            # 数量子栏的枚数（非铅封号）
    if with_col19_label and seal:
        _draw(c, 517.7, 378, seal, 8)     # 记号子栏内的铅封号

    # 底部集装箱信息块
    _draw(c, 41.2, 524, f"{container}  45G1", 8)

    c.showPage()
    c.save()
    return buf.getvalue()


def _ext(container="TKRU4625794", wagon="5480825", seal="260281",
         c_status=we.FIELD_OK, w_status=we.FIELD_OK, s_status=we.FIELD_OK,
         filename=None) -> we.WaybillExtraction:
    """合成提取结果（compare_batch 逻辑测试用，不经PDF解析）。"""
    return we.WaybillExtraction(
        filename=filename or f"{container}.pdf",
        container=we.FieldExtraction("container_no", container, c_status),
        wagon_no=we.FieldExtraction(
            "wagon_no", wagon if w_status == we.FIELD_OK else None, w_status),
        seal_no=we.FieldExtraction(
            "seal_no", seal if s_status == we.FIELD_OK else None, s_status))


def _row(seq="48", wagon="5480825", container="TKRU4625794", seal="260281",
         row_no=12, wagon_type="NX70AF") -> we.PackingRow:
    return we.PackingRow(row_no=row_no, seq=seq, wagon_type=wagon_type,
                         wagon_no=wagon, container_no=container, seal_no=seal)


# ---------------------------------------------------------------------------
# 坐标定位提取（核心约束：禁用文本流顺序推断）
# ---------------------------------------------------------------------------

def test_seal_by_coordinate_not_stream_order():
    """铅封号必须取第19栏"记号"子栏框内值，不受文本流更早的6位干扰数字影响。"""
    ext = we.extract_waybill(build_waybill(), "t.pdf")
    assert ext.seal_no.status == we.FIELD_OK
    assert ext.seal_no.value == "260281"        # 而非文本流更早的 135790
    assert ext.seal_no.method == "coordinate"
    assert ext.seal_no.anchor and "знаки" in ext.seal_no.anchor


def test_wagon_excludes_col8_same_word_interference():
    """车底号取第7栏框内值，排除第8栏"Вагон предоставлен"的干扰值。"""
    ext = we.extract_waybill(build_waybill(), "t.pdf")
    assert ext.wagon_no.status == we.FIELD_OK
    assert ext.wagon_no.value == "5480825"      # 而非第8栏框内的 987654
    assert ext.container.value == "TKRU4625794"


def test_extraction_extracts_box_truth_without_value_preference():
    """框内是旧封261918就如实提取261918——换封判断交给与清单的比对，
    提取层绝不内置"偏好值"（任务书验收案例的另一面）。"""
    ext = we.extract_waybill(build_waybill(seal="261918"), "t.pdf")
    assert ext.seal_no.value == "261918"


def test_format_anomaly_flagged_not_silent():
    """位数不对/含非数字 → 提取异常，绝不静默输出可疑值（任务书健壮性）。"""
    ext = we.extract_waybill(build_waybill(wagon="54808X5", seal="26028"), "t.pdf")
    assert ext.wagon_no.status == we.FIELD_FORMAT_ANOMALY
    assert ext.wagon_no.value is None
    assert "54808X5" in ext.wagon_no.note       # 候选值留痕供人工核查
    assert ext.seal_no.status == we.FIELD_FORMAT_ANOMALY
    assert ext.seal_no.value is None


def test_anchor_missing_when_label_vanishes():
    """栏位标签消失（模板变化信号）→ anchor_missing，而不是乱抓数字。"""
    ext = we.extract_waybill(
        build_waybill(with_col7_label=False, with_col19_label=False), "t.pdf")
    assert ext.wagon_no.status == we.FIELD_ANCHOR_MISSING
    assert ext.seal_no.status == we.FIELD_ANCHOR_MISSING
    assert ext.wagon_no.value is None and ext.seal_no.value is None


def test_bad_file_becomes_read_error_not_crash():
    """坏文件不拖垮整批：safe包装转为read_error异常结果。"""
    ext = we.extract_waybill_safe(b"not a pdf", "broken.pdf")
    assert ext.container.status == we.FIELD_READ_ERROR
    assert ext.anomaly_fields() == ["container_no", "wagon_no", "seal_no"]


# ---------------------------------------------------------------------------
# 现场清单Excel解析
# ---------------------------------------------------------------------------

def _build_list_workbook() -> bytes:
    """复刻真实清单结构：第1个sheet是空白打印模板（同有表头，干扰项），
    "电子版"sheet 才有数据（表头第2行，铅封号为数值单元格）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "发运清单"
    ws1.append([None, None, "大同晋宏公司集装箱发运清单"])
    ws1.append(["序号", "车板号", "箱号", "封号", "箱号", "封号"])
    for i in range(1, 4):
        ws1.append([i])
    ws2 = wb.create_sheet("集装箱发运清单电子版")
    ws2.append([None, None, None, "大同-晋宏40英尺箱登记表"])
    ws2.append(["序号", "车种", "车底号", "箱号", "铅封号", "货主"])
    ws2.append([33, "NX70AF", "5742270", "TKRU4725983", 260296])
    ws2.append([48, "NX70AF", "5480825", "TKRU4625794", 260281])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_load_packing_list_picks_data_sheet_and_normalizes():
    rows = we.load_packing_list(_build_list_workbook())
    assert len(rows) == 2                        # 空白模板sheet的空行被跳过
    by_container = {r.container_no: r for r in rows}
    r48 = by_container["TKRU4625794"]
    assert r48.seq == "48" and r48.row_no == 4   # 序号列与Excel实际行号都保留
    assert r48.wagon_no == "5480825"
    assert r48.seal_no == "260281"               # 数值单元格 → 字符串归一化


# ---------------------------------------------------------------------------
# 批量核对（箱号主键 + 状态判定 + 批级合理性告警）
# ---------------------------------------------------------------------------

def test_compare_mismatch_statuses_are_field_specific():
    rows = [_row()]
    res = we.compare_batch(rows, [_ext(wagon="5480826"),
                                  _ext(seal="261918"),
                                  _ext(wagon="5480826", seal="261918")])
    statuses = [r["status"] for r in res["results"]]
    assert statuses == [we.ST_BOTH_MISMATCH, we.ST_WAGON_MISMATCH,
                        we.ST_SEAL_MISMATCH]     # 不一致置顶，两项不一致最重
    wagon_row = res["results"][1]
    assert wagon_row["mismatch_fields"] == ["wagon_no"]


def test_compare_match_and_summary():
    res = we.compare_batch([_row()], [_ext()])
    assert res["results"][0]["status"] == we.ST_MATCH
    assert res["summary"]["match"] == 1


def test_unmatched_wording_explains_both_directions():
    """未匹配提示必须说明"箱号录错或清单没有"两种可能，不能写"缺失"误导。"""
    rows = [_row()]
    res = we.compare_batch(rows, [_ext(container="TKRU9999999",
                                       filename="mystery.pdf")])
    pdf_side = next(r for r in res["results"]
                    if r["status"] == we.ST_PDF_UNMATCHED)
    assert "录入有误" in we.PDF_UNMATCHED_TEXT and "清单里没有" in we.PDF_UNMATCHED_TEXT

    res2 = we.compare_batch([_row()], [])          # 清单有、PDF没来
    excel_side = res2["results"][0]
    assert excel_side["status"] == we.ST_EXCEL_UNMATCHED
    assert "漏传" in we.EXCEL_UNMATCHED_TEXT and "录错" in we.EXCEL_UNMATCHED_TEXT


def test_extraction_anomaly_not_miscounted_as_unmatched():
    """箱号提取失败的PDF归"提取异常"，不落"PDF未匹配"（避免误导方向）。"""
    res = we.compare_batch([_row()], [_ext(c_status=we.FIELD_READ_ERROR,
                                           w_status=we.FIELD_READ_ERROR,
                                           s_status=we.FIELD_READ_ERROR)])
    assert res["results"][0]["status"] == we.ST_EXTRACTION_ANOMALY
    assert res["summary"][we.ST_PDF_UNMATCHED] == 0


def test_wagon_anomaly_still_surfaces_seal_mismatch():
    """旧导出场景：车底号提取异常的同时，铅封号不一致的明细必须保留可见。"""
    rows = [_row()]
    ext = _ext(wagon="5480825", seal="261918",
               w_status=we.FIELD_NOT_FOUND)
    res = we.compare_batch(rows, [ext])
    r = res["results"][0]
    assert r["status"] == we.ST_EXTRACTION_ANOMALY
    assert r["field_results"]["seal_no"]["result"] == "mismatch"
    assert r["field_results"]["seal_no"]["pdf_value"] == "261918"
    assert r["field_results"]["seal_no"]["excel_value"] == "260281"


def test_batch_anomaly_warning_threshold():
    """异常占比超阈值（默认20%）→ 批级告警"模板可能已变化"。"""
    exts = [_ext(container=f"TKRU{i:07d}", filename=f"f{i}.pdf")
            for i in range(7)]
    bad = [_ext(container=f"TKRU9{i:06d}", w_status=we.FIELD_ANCHOR_MISSING,
                filename=f"b{i}.pdf") for i in range(3)]
    res = we.compare_batch([], exts + bad)       # 3/10 = 30% 超阈值
    anomaly = res["anomaly"]
    assert anomaly["anomaly_ratio"] == pytest.approx(0.3, abs=0.001)
    assert anomaly["batch_warning"] is True
    assert "模板可能已变化" in anomaly["warning_text"]
    assert anomaly["anchor_missing_files"] == ["b0.pdf", "b1.pdf", "b2.pdf"]

    # 2/10=20% 未超过阈值 → 不告警
    exts10 = [_ext(container=f"TKRU{i:07d}", filename=f"f{i}.pdf")
              for i in range(8)]
    bad2 = [_ext(container=f"TKRU9{i:06d}", s_status=we.FIELD_NOT_FOUND,
                 filename=f"b{i}.pdf") for i in range(2)]
    res2 = we.compare_batch([], exts10 + bad2)
    assert res2["anomaly"]["batch_warning"] is False


def test_batch_anomaly_threshold_configurable_via_env(monkeypatch):
    """阈值可用环境变量调整（任务书"建议阈值可配置"）。"""
    monkeypatch.setenv(we.ENV_ANOMALY_RATIO, "0.1")
    exts = [_ext(container=f"TKRU{i:07d}", filename=f"f{i}.pdf")
            for i in range(8)]
    bad = [_ext(container="TKRU9999999", s_status=we.FIELD_NOT_FOUND,
                filename="b.pdf")]
    res = we.compare_batch([], exts + bad)       # 1/9 ≈ 11% > 10%
    assert res["anomaly"]["threshold"] == 0.1
    assert res["anomaly"]["batch_warning"] is True


def test_duplicate_container_note():
    """清单箱号重复时按第一行核对并留痕，不静默吞掉。"""
    rows = [_row(row_no=2), _row(seq="49", row_no=3)]
    res = we.compare_batch(rows, [_ext()])
    assert res["results"][0]["status"] == we.ST_MATCH
    assert any("出现2行" in n for n in res["results"][0]["notes"])


# ---------------------------------------------------------------------------
# 真实受控样本回归（任务书硬性验收；样本缺失时跳过）
# ---------------------------------------------------------------------------

def _real(name: str) -> Path | None:
    p = _REAL_DIR / name
    return p if p.exists() else None


@pytest.fixture(scope="module")
def real_rows():
    xlsx = _real("9-25-55中欧班列(车板).xlsx")
    if xlsx is None:
        pytest.skip("真实清单不在本机（受控文件不入库）")
    return we.load_packing_list(xlsx)


def test_real_acceptance_seal_260281_not_261918(real_rows):
    """任务书硬性验收：新导出必须提取出换封后的260281，而不是261918；
    车底号5480825与现场清单序号48行完全一致。"""
    pdf = _real("TKRU4625794-260925-102205已加密.pdf")
    if pdf is None:
        pytest.skip("真实运单不在本机（受控文件不入库）")
    ext = we.extract_waybill(pdf.read_bytes(), pdf.name)
    assert ext.seal_no.value == "260281"
    assert ext.seal_no.value != "261918"
    assert ext.wagon_no.value == "5480825"
    assert ext.container.value == "TKRU4625794"
    assert not ext.anomaly_fields()

    res = we.compare_batch(real_rows, [ext])
    r = res["results"][0]
    assert r["status"] == we.ST_MATCH
    assert r["seq"] == "48"                      # 现场清单序号48行


def test_real_stale_export_flags_mismatch(real_rows):
    """旧导出（260921）：第19栏框内是装车旧封261918（如实提取），
    第7栏为空→提取异常；与清单比对标出铅封不一致——防静默出错的实战场景。"""
    pdf = _real("TKRU4625794-260921-085933已加密.pdf")
    if pdf is None:
        pytest.skip("真实旧导出不在本机（受控文件不入库）")
    ext = we.extract_waybill(pdf.read_bytes(), pdf.name)
    assert ext.seal_no.value == "261918"
    assert ext.wagon_no.is_anomaly
    res = we.compare_batch(real_rows, [ext])
    r = res["results"][0]
    assert r["status"] == we.ST_EXTRACTION_ANOMALY
    assert r["field_results"]["seal_no"]["result"] == "mismatch"
    assert r["field_results"]["seal_no"]["pdf_value"] == "261918"
    assert r["field_results"]["seal_no"]["excel_value"] == "260281"


def test_real_full_batch_all_match(real_rows):
    """全量批次回归：清单55行应与真实导出运单一一匹配（环境里PDF目录
    存在时执行；仅校验计数与告警，不落任何数据）。"""
    import os
    pdf_dir = os.environ.get("CEBS_REAL_SMGS_DIR", "").strip()
    if not pdf_dir or not Path(pdf_dir).exists():
        pytest.skip("设置 CEBS_REAL_SMGS_DIR 指向真实运单目录后运行")
    pdfs = sorted(Path(pdf_dir).glob("*.pdf"))
    assert pdfs, "目录中没有PDF运单"
    exts = [we.extract_waybill_safe(p.read_bytes(), p.name) for p in pdfs]
    res = we.compare_batch(real_rows, exts)
    anomaly = res["anomaly"]
    assert not anomaly["batch_warning"], anomaly["warning_text"]
    assert res["summary"][we.ST_EXTRACTION_ANOMALY] == 0
