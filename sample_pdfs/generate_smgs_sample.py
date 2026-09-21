# -*- coding: utf-8 -*-
"""生成SMGS国际货协运单受控测试夹具（P0任务书A4）。

为什么是"生成的夹具"而不是真实PDF：任务书要求真实单据不得进入仓库
（含客户敏感信息）。本脚本按SMGS标准栏位版式（栏位号+中俄双语标签+分栏
表格）渲染测试PDF，字段值取自任务书给出的人工确认基准。版式结构（而非
字段值）才是回归的对象——适配器按栏位号/标签/坐标工作，未对任何值硬编码。

生成：
  sample_pdfs/smgs_waybill_tkru.pdf        主回归样本（基准值+栏位版式）
  sample_pdfs/smgs_weight_unlabeled.pdf    反例样本：栏位18重量未标毛/净口径
      （验证多重量/无口径重量必须进入 needs_review 而非静默判定）

运行：python sample_pdfs/generate_smgs_sample.py
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).parent

# 任务书人工确认基准（TKRU4625794 单据）
BASELINE = {
    "document_type": "smgs_rail_waybill",
    "consignor_name": "芜湖德菲图汽车技术有限公司",
    "consignee_name": 'ООО "АВТОЗАВОД АГР"',
    "container_no": "TKRU4625794",
    "goods_description": "汽车成套散件 COOLING FAN",
    "gross_weight_kg": 5629.84,
    "net_weight_kg": 3650.0,
    "total_packages": 25,
    "departure_station": "大同 DATONG",
    "destination_station": "谢利亚季诺 SELYATINO",
    "destination_country": "俄罗斯",
    "cargo_value": 332433.16,
    "currency": "CNY",
    "packing_type": "托盘",
    "seal_no": "261918",
}


def _new_canvas(path: Path):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("SMGS Rail Waybill (controlled test fixture)")
    return c, "STSong-Light"


def _field_box(c, font, x, y, w, h, no, labels, value_lines, value_font_size=9):
    """画一个栏位格：左上角栏位号，标签（中俄），格内值文本（可多行）。"""
    c.setStrokeColor(colors.HexColor("#444444"))
    c.setLineWidth(0.7)
    c.rect(x, y, w, h)
    c.setFont(font, 7)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(x + 2, y + h - 8, no)
    c.setFillColor(colors.HexColor("#333333"))
    c.drawString(x + 14, y + h - 8, labels)
    c.setFont(font, value_font_size)
    c.setFillColor(colors.HexColor("#000000"))
    ty = y + h - 22
    for line in value_lines:
        c.drawString(x + 6, ty, line)
        ty -= 11


def build_smgs(path: Path, weight_lines=None):
    """渲染单页SMGS栏位版式。weight_lines 可覆盖栏位18内容（反例样本用）。"""
    c, font = _new_canvas(path)
    W, H = A4

    # 抬头（类型识别信号：СМГС + 国际货协运单）
    c.setFont(font, 13)
    c.drawCentredString(W / 2, H - 42, "МЕЖДУНАРОДНАЯ ЖЕЛЕЗНОДОРОЖНАЯ НАКЛАДНАЯ СМГС")
    c.setFont(font, 11)
    c.drawCentredString(W / 2, H - 58, "国际货协运单（СМГС）· Оригинал")

    # 第一行栏位：1 发货人 | 2 发站
    _field_box(c, font, 30, H - 150, 260, 64, "1",
               "Отправитель 发货人 (Наименование и адрес)",
               [BASELINE["consignor_name"],
                "安徽省芜湖市经济技术开发区 长山路 21 号  241009"])
    _field_box(c, font, 296, H - 150, 260, 64, "2",
               "Станция отправления 发站",
               [BASELINE["departure_station"], "Код ст. 262001"])

    # 第二行：4 收货人（跨行完整名称，不得截断为ООО）| 5 到站
    _field_box(c, font, 30, H - 220, 260, 64, "4",
               "Получатель 收货人 (Наименование и адрес)",
               ['ООО "АВТОЗАВОД АГР"', "ИНН 5029001234  Московская обл."])
    _field_box(c, font, 296, H - 220, 260, 64, "5",
               "Станция назначения 到站",
               [BASELINE["destination_station"], "Код ст. 181201"])

    # 第三行：7 集装箱 | 19 封印
    _field_box(c, font, 30, H - 284, 260, 56, "7",
               "Контейнер 集装箱 (№, тип)",
               [f'{BASELINE["container_no"]}  20\' 8"6\'\'  30480 kg'])
    _field_box(c, font, 296, H - 284, 260, 56, "19",
               "Пломбы 封印 (число и №)",
               [f'{BASELINE["seal_no"]}  (1 шт.)'])

    # 中部：15 货物名称（中英俄混合）
    _field_box(c, font, 30, H - 356, 526, 64, "15",
               "Наименование груза 货物名称",
               [BASELINE["goods_description"], "КОМПЛЕКТ АВТОМОБИЛЬНЫЙ (SKD)"],
               value_font_size=9.5)

    # 16 包装 | 17 件数 | 18 重量（毛/净分行，均带口径标签）
    _field_box(c, font, 30, H - 412, 170, 48, "16",
               "Вид упаковки 包装种类", [BASELINE["packing_type"]])
    _field_box(c, font, 206, H - 412, 160, 48, "17",
               "Число мест 件数", [str(BASELINE["total_packages"])])
    _field_box(c, font, 372, H - 412, 184, 48, "18",
               "Вес 重量 (кг)",
               weight_lines if weight_lines is not None else
               [f'Вес брутто 毛重: {BASELINE["gross_weight_kg"]} кг',
                f'Вес нетто 净重: {BASELINE["net_weight_kg"]} кг'])

    # 22 承运人 | 24 随附文件
    _field_box(c, font, 30, H - 470, 260, 50, "22",
               "Перевозчик 承运人",
               ["КЖД→КЗЖ→РЖД  262001/181201"])
    _field_box(c, font, 296, H - 470, 260, 50, "24",
               "Приложенные документы 随附文件",
               ["装箱单 1 份；商业发票 1 份"])

    # 底部：运抵国 / 货值+币种
    _field_box(c, font, 30, H - 524, 200, 46, "—",
               "Страна назначения 运抵国", [BASELINE["destination_country"]])
    _field_box(c, font, 236, H - 524, 320, 46, "25",
               "Стоимость груза 货值 / Валюта 币种",
               [f'{BASELINE["cargo_value"]} {BASELINE["currency"]}'])

    c.setFont(font, 7)
    c.setFillColor(colors.HexColor("#888888"))
    c.drawString(30, 36, "Контролируемый испытательный образец (сгенерированная копия макета, "
                         "не содержит подлинных данных клиента)")
    c.showPage()
    c.save()
    print(f"generated: {path}")


def build_unlabeled_weight_variant(path: Path):
    """反例：栏位18只有一个'Вес/重量'数值，未标注毛/净口径。
    适配器必须把它放入 needs_review（按惯例暂记毛重+提示），不得静默判定。"""
    build_smgs(path, weight_lines=["Вес груза 重量: 5629.84 кг"])


if __name__ == "__main__":
    build_smgs(OUT_DIR / "smgs_waybill_tkru.pdf")
    build_unlabeled_weight_variant(OUT_DIR / "smgs_weight_unlabeled.pdf")
