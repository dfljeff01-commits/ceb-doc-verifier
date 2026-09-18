# -*- coding: utf-8 -*-
"""
生成PDF识别环节的测试单证（开发期工具，不随应用运行时使用）：

  sample_pdfs/
    invoice.pdf               文本型（reportlab直出，含文字层）
    packing_list.pdf          文本型
    waybill.pdf               文本型
    customs_declaration.pdf   文本型
    scan_invoice.pdf          扫描型（上述发票栅格化成图片再回填，无文字层）
    scan_packing_list.pdf     扫描型
    mixed_docs.pdf            混合型（第1页文本发票 + 第2页扫描装箱单）
    invoice_missing_weight.pdf 含缺失关键词的文本型（无毛重栏，验证置信度标记）
    waybills_x3.pdf           多运单混合PDF：3份独立运单（多车厢分批场景，
                              用于单据边界识别/拆分与逐单据核验的验收场景）
    waybill_missing_consignee.pdf 缺少收货人栏的运单（单据级规则知识库验收：
                              应判FAIL并给出"补充收货人"建议）

用法：python sample_pdfs/generate_samples.py
"""

from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def _lines(c: canvas.Canvas, x: float, y: float, lines: list, lh: float = 18) -> None:
    for line in lines:
        c.setFont("STSong-Light", 11)
        c.drawString(x, y, line)
        y -= lh


def draw_invoice(missing_weight: bool = False) -> bytes:
    """文本型商业发票。missing_weight=True 时不画毛重栏。"""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("STSong-Light", 16)
    c.drawString(200, 800, "商业发票 COMMERCIAL INVOICE")
    _lines(c, 60, 760, [
        "INVOICE NO: INV-PDF-2026-0912-001",
        "DATE: 2026-09-10",
        "SELLER(发货人): 山西洁康陶瓷制品有限公司",
        "BUYER(收货人): ISTANBUL YAPI SANAYI A.S.",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(总件数): 480",
        "NET WEIGHT(净重): 10800 KG",
    ])
    if not missing_weight:
        _lines(c, 60, 634, ["GROSS WEIGHT(毛重): 12300 KG"])
        y = 616
    else:
        y = 634
    _lines(c, 60, y, [
        "TOTAL AMOUNT(金额合计): USD 86,400.00",
        "CURRENCY(币种): USD",
        "INCOTERM: CIF ISTANBUL",
    ])
    c.save()
    return buf.getvalue()


def draw_packing_list() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("STSong-Light", 16)
    c.drawString(200, 800, "装箱单 PACKING LIST")
    _lines(c, 60, 760, [
        "PACKING LIST NO: PL-PDF-2026-0912-001",
        "SELLER(发货人): 山西洁康陶瓷制品有限公司",
        "BUYER(收货人): ISTANBUL YAPI SANAYI A.S.",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        "TOTAL PACKAGES(总件数): 480",
        "GROSS WEIGHT(毛重): 12300 KG",
        "NET WEIGHT(净重): 10800 KG",
        "CONTAINER NO(箱号): TCLU1234567",
    ])
    c.save()
    return buf.getvalue()


def draw_waybill(waybill_no: str = "SMU/789456/2026",
                 consignee: str = "ISTANBUL YAPI SANAYI A.S.",
                 wagon_no: str = "WGN-6501",
                 total_packages: str = "480",
                 gross_weight: str = "12300") -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("STSong-Light", 16)
    c.drawString(180, 800, "国际铁路运单 RAILWAY CONSIGNMENT NOTE")
    _lines(c, 60, 760, [
        f"WAYBILL NO(运单号): {waybill_no}",
        "WAYBILL TYPE(运单类型): CIM/SMGS统一运单",
        "SHIPPER(发货人): 山西洁康陶瓷制品有限公司",
        f"CONSIGNEE(收货人): {consignee}",
        "FROM(发站): 中国 西安新筑站",
        "TO(到站): 土耳其 伊斯坦布尔 Halkali站",
        "VIA(经由): 中国、哈萨克斯坦、阿塞拜疆、格鲁吉亚、土耳其",
        "DESCRIPTION OF GOODS(货物描述): 陶瓷卫浴洁具",
        f"TOTAL PACKAGES(件数): {total_packages}",
        f"GROSS WEIGHT(毛重): {gross_weight} KG",
        f"WAGON NO(车厢号): {wagon_no}",
        "CONTAINER NO(箱号): TCLU1234567",
    ])
    c.save()
    return buf.getvalue()


def draw_customs_declaration() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("STSong-Light", 16)
    c.drawString(180, 800, "中华人民共和国海关出口货物报关单")
    _lines(c, 60, 760, [
        "报关单编号(DECLARATION NO): 29152026000123456",
        "境内发货人(发货人): 山西洁康陶瓷制品有限公司",
        "境外收货人(收货人): ISTANBUL YAPI SANAYI A.S.",
        "品名及规格(货物描述): 卫浴陶瓷制品",
        "件数(箱数): 475",
        "毛重(KG): 12300",
        "总价(申报金额): USD 86,400.00",
        "币种(CURRENCY): USD",
        "运抵国(目的国): 土耳其",
        "起运国(发货国): 中国",
        "随附单证运单号: SMU/789456/2026",
        "集装箱号: TCLU1234567",
    ])
    c.save()
    return buf.getvalue()


def text_pdf_to_scanned(pdf_bytes: bytes, dpi: int = 200) -> bytes:
    """文本PDF → 栅格化图片 → 重新封装为无文字层的扫描型PDF。"""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes("png")
        img = fitz.open(stream=img_bytes, filetype="png")
        page_rect = page.rect
        new_page = out.new_page(width=page_rect.width, height=page_rect.height)
        new_page.insert_image(page_rect, stream=img_bytes)
        img.close()
    data = out.tobytes()
    src.close()
    out.close()
    return data


def make_mixed(text_pdf: bytes, scanned_pdf: bytes) -> bytes:
    """混合型：第1页取文本发票，第2页取扫描装箱单。"""
    a = fitz.open(stream=text_pdf, filetype="pdf")
    b = fitz.open(stream=scanned_pdf, filetype="pdf")
    out = fitz.open()
    out.insert_pdf(a, from_page=0, to_page=0)
    out.insert_pdf(b, from_page=0, to_page=0)
    data = out.tobytes()
    a.close(), b.close(), out.close()
    return data


def make_multi_page(pdf_bytes_list: list) -> bytes:
    """把多份单页PDF按顺序合并为一份多页PDF（多运单混合PDF的构造方式）。"""
    out = fitz.open()
    for data in pdf_bytes_list:
        src = fitz.open(stream=data, filetype="pdf")
        out.insert_pdf(src)
        src.close()
    merged = out.tobytes()
    out.close()
    return merged


def main() -> None:
    invoice = draw_invoice()
    invoice_no_w = draw_invoice(missing_weight=True)
    packing = draw_packing_list()

    (OUT / "invoice.pdf").write_bytes(invoice)
    (OUT / "packing_list.pdf").write_bytes(packing)
    (OUT / "waybill.pdf").write_bytes(draw_waybill())
    (OUT / "customs_declaration.pdf").write_bytes(draw_customs_declaration())
    (OUT / "invoice_missing_weight.pdf").write_bytes(invoice_no_w)

    scan_invoice = text_pdf_to_scanned(invoice)
    scan_packing = text_pdf_to_scanned(packing)
    (OUT / "scan_invoice.pdf").write_bytes(scan_invoice)
    (OUT / "scan_packing_list.pdf").write_bytes(scan_packing)
    (OUT / "mixed_docs.pdf").write_bytes(make_mixed(invoice, scan_packing))

    # 多运单混合PDF：同一批货物分3节车厢，3份独立运单（运单号/车厢号/件数/毛重各不相同）
    waybills = [
        draw_waybill(waybill_no="SMU/789456/2026", wagon_no="WGN-6501",
                     total_packages="160", gross_weight="4100"),
        draw_waybill(waybill_no="SMU/789457/2026", wagon_no="WGN-6502",
                     total_packages="160", gross_weight="4150"),
        draw_waybill(waybill_no="SMU/789458/2026", wagon_no="WGN-6503",
                     total_packages="160", gross_weight="4050"),
    ]
    (OUT / "waybills_x3.pdf").write_bytes(make_multi_page(waybills))
    # 缺收货人的运单（单据级规则知识库验收样本：应判FAIL并给出补充建议）
    (OUT / "waybill_missing_consignee.pdf").write_bytes(
        draw_waybill(waybill_no="SMU/789999/2026", consignee=""))

    (OUT / "_tmp.pdf").unlink(missing_ok=True)
    print("generated:", sorted(p.name for p in OUT.glob("*.pdf")))


if __name__ == "__main__":
    main()
