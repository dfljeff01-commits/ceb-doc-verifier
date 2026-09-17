# -*- coding: utf-8 -*-
"""生成App演示用的单证图片集（开发期工具）：

  sample_pdfs/images/clean/   全部通过组（5份，含原产地证书，杜伊斯堡线）
  sample_pdfs/images/issues/  含4类问题组（4份，伊斯坦布尔线：品名/箱数/毛重/缺COO）

图片由 reportlab 绘制文本层PDF后光栅化为JPG（200dpi），可直接放进手机相册，
在App里用"从相册选择"模拟"拍照识别单证"演示；也可用于 /ingest/image 接口测试。

用法：python sample_pdfs/generate_app_samples.py
"""

from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as rl_canvas

OUT = Path(__file__).parent / "images"

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

# 优先用Windows黑体（栅格化后OCR识别显著更稳）；无则退回内置CID字体
import os

from reportlab.pdfbase.ttfonts import TTFont

_FONT_PATH = next((p for p in (r"C:\Windows\Fonts\simhei.ttf",
                               r"C:\Windows\Fonts\msyh.ttc") if os.path.exists(p)), None)
if _FONT_PATH:
    pdfmetrics.registerFont(TTFont("CJK", _FONT_PATH, subfontIndex=0))
    CJK_FONT = "CJK"
else:
    CJK_FONT = "STSong-Light"

CLEAN = dict(
    consignee="RHEINBAU HANDEL GMBH",
    dest_country="德国", dest_station="德国 杜伊斯堡 DIT场站",
    route="中国,哈萨克斯坦,俄罗斯,白俄罗斯,波兰,德国",
    waybill_type="SMGS国际货协运单", waybill_no="SMU/512301/2026",
    desc_customs="陶瓷卫浴洁具", pkg_customs=480, gw_packing=12300,
    decl_no="29152026000118888", container="MSKU8765432",
)
ISSUES = dict(
    consignee="ISTANBUL YAPI SANAYI A.S.",
    dest_country="土耳其", dest_station="土耳其 伊斯坦布尔 Halkalı站",
    route="中国,哈萨克斯坦,阿塞拜疆,格鲁吉亚,土耳其",
    waybill_type="CIM SMGS 统一运单", waybill_no="SMU/789456/2026",
    desc_customs="卫浴陶瓷制品", pkg_customs=475, gw_packing=12500,
    decl_no="29152026000123456", container="TCLU1234567",
)


def _draw(title: str, lines: list) -> bytes:
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setFont(CJK_FONT, 19)
    c.drawString(180, 800, title)
    y = 762
    lh = 26 if len(lines) <= 11 else max(20, 700 // max(len(lines), 1))
    for line in lines:
        c.setFont(CJK_FONT, line.get("size", 15) if isinstance(line, dict) else 15)
        c.drawString(60, y, line["text"] if isinstance(line, dict) else line)
        y -= lh
    c.showPage()
    c.save()
    return buf.getvalue()


def _to_jpg(pdf_bytes: bytes, name: str, dpi: int = 200) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    OUT.joinpath(name).parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(OUT / name), jpg_quality=88)
    doc.close()


def make_invoice(s: dict) -> bytes:
    return _draw("商业发票 COMMERCIAL INVOICE", [
        "INVOICE NO: INV-APP-2026-001", "DATE: 2026-09-10",
        "SELLER(发货人): 山西洁康陶瓷制品有限公司",
        f"BUYER(收货人): {s['consignee']}",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(总件数): 480",
        "NET WEIGHT(净重): 10800 KG",
        "GROSS WEIGHT(毛重): 12300 KG",
        "TOTAL AMOUNT(金额合计): USD 86,400.00",
    ])


def make_packing(s: dict) -> bytes:
    return _draw("装箱单 PACKING LIST", [
        "PACKING LIST NO: PL-APP-2026-001",
        "SELLER(发货人): 山西洁康陶瓷制品有限公司",
        f"BUYER(收货人): {s['consignee']}",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(总件数): 480",
        f"GROSS WEIGHT(毛重): {s['gw_packing']} KG",
        "NET WEIGHT(净重): 10800 KG",
        f"CONTAINER NO(箱号): {s['container']}",
    ])


def make_waybill(s: dict) -> bytes:
    return _draw("国际铁路运单 RAILWAY CONSIGNMENT NOTE", [
        f"WAYBILL NO(运单号): {s['waybill_no']}",
        f"WAYBILL TYPE(运单类型): {s['waybill_type']}",
        "SHIPPER(发货人): 山西洁康陶瓷制品有限公司",
        f"CONSIGNEE(收货人): {s['consignee']}",
        "FROM(发站): 中国 西安新筑站",
        f"TO(到站): {s['dest_station']}",
        {"text": f"VIA: {s['route']}", "size": 15},
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(件数): 480",
        "GROSS WEIGHT(毛重): 12300 KG",
        f"CONTAINER NO(箱号): {s['container']}",
    ])


def make_customs(s: dict) -> bytes:
    return _draw("中华人民共和国海关出口货物报关单", [
        f"报关单编号(DECLARATION NO): {s['decl_no']}",
        "境内发货人(发货人): 山西洁康陶瓷制品有限公司",
        f"境外收货人(收货人): {s['consignee']}",
        f"品名及规格(货物描述): {s['desc_customs']}",
        f"件数(箱数): {s['pkg_customs']}",
        "毛重(KG): 12300",
        "总价(申报金额): USD 86,400.00",
        "起运国(发货国): 中国",
        f"运抵国(目的国): {s['dest_country']}",
        f"随附单证运单号: {s['waybill_no']}",
        f"集装箱号: {s['container']}",
    ])


def make_coo(s: dict) -> bytes:
    return _draw("原产地证书 CERTIFICATE OF ORIGIN", [
        "CERTIFICATE NO: CCPIT-APP-2026-001",
        "ISSUER(签发机构): 中国国际贸易促进委员会(CCPIT)",
        "EXPORTER(发货人): 山西洁康陶瓷制品有限公司",
        f"CONSIGNEE(收货人): {s['consignee']}",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(总件数): 480",
        "ORIGIN(原产地): 中国",
    ])


def main() -> None:
    clean = CLEAN
    issues = ISSUES
    sets = {
        "clean": [("invoice", make_invoice(clean)), ("packing_list", make_packing(clean)),
                  ("waybill", make_waybill(clean)), ("customs", make_customs(clean)),
                  ("coo", make_coo(clean))],
        "issues": [("invoice", make_invoice(issues)), ("packing_list", make_packing(issues)),
                   ("waybill", make_waybill(issues)), ("customs", make_customs(issues))],
    }
    for set_name, docs in sets.items():
        for name, pdf_bytes in docs:
            _to_jpg(pdf_bytes, f"{set_name}/{name}.jpg")
    print("generated:", sorted(str(p.relative_to(OUT.parent)) for p in OUT.rglob("*.jpg")))


if __name__ == "__main__":
    main()
