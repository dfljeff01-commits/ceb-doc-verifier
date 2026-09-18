# -*- coding: utf-8 -*-
"""合规依据知识库 + 向量检索（原生AI功能·任务书第3节）。

为核验判断补充"依据引用"：对 FAIL/WARNING 项检索最相关的知识库条文，
展示"判断结论 + 引用依据"。

诚实性说明（与README口径一致）：
  - 知识库为**公开资料整理摘录**（初级版口径，约15条，非官方全文，以现行法规为准）；
  - 检索为**真实的向量检索**：复用 semantic 模块的字符n-gram词频向量+余弦相似度
    对知识条目排序（与语义比对同一套向量化技术），非静态固定映射；
  - 检索结果的"解释组织"在无API Key时为模板拼接（判断+条文并列呈现），
    配置Key后可由LLM整合为连贯叙述。
"""

from __future__ import annotations

import semantic

KB_DISCLAIMER = ("知识库条目为公开资料整理摘录（初级版，非官方全文，以现行法规为准），"
                 "实际作业请以现行法规及口岸要求为准。")

ENTRIES = [
    {"id": "KB-01", "title": "SMGS《国际货协》运单覆盖范围",
     "text": "SMGS运单适用于《国际铁路货物联运协定》参加铁路之间：包括中国、蒙古、哈萨克斯坦、俄罗斯、白俄罗斯、波兰（部分）、乌克兰等独联体及中东欧国家。土耳其、欧盟多数国家不是SMGS适用路，仅持SMGS运单进入这些区段缺乏运输合同凭证。",
     "tags": ["waybill_type", "route", "smgs"]},
    {"id": "KB-02", "title": "CIM运单与OTIF成员",
     "text": "CIM运单适用于《国际铁路运输公约》(OTIF)成员铁路，包括德国、法国、波兰、捷克、匈牙利、土耳其等。中间走廊（经阿塞拜疆-格鲁吉亚-土耳其BTK线）通常需要CIM/SMGS统一运单或在卡尔斯换装点办理运单转换衔接。",
     "tags": ["waybill_type", "route", "cim", "turkey"]},
    {"id": "KB-03", "title": "CIM/SMGS统一运单",
     "text": "为贯通欧亚跨法规区运输，铁路合作组织(OSJD)与OTIF推出CIM/SMGS统一运单（联合运单），一份运单同时适用两个公约区段，可避免中间口岸重新缮制运单，是中间走廊/跨黑海线路的推荐做法。",
     "tags": ["waybill_type", "unified", "route"]},
    {"id": "KB-04", "title": "原产地证书的签发与用途",
     "text": "出口原产地证书(CO)通常由 CCPIT（中国国际贸易促进委员会）或海关签发，用于进口国清关时证明货物原产地、申请协定税率/关税优惠。缺少产地证可能导致无法享受优惠税率甚至无法清关；补办需在出口前/装运后规定期限内申请。",
     "tags": ["certificate_of_origin", "missing_doc", "ccpit"]},
    {"id": "KB-05", "title": "报关单申报不实的法律后果",
     "text": "依照《中华人民共和国海关法》及报关管理规定，进出口货物申报应当真实、准确。品名、数量、重量、金额申报不实的，海关可责令改正、罚款，情节严重的按走私或违反海关监管规定处理，并影响企业信用等级。",
     "tags": ["declaration", "accuracy", "penalty"]},
    {"id": "KB-06", "title": "报关单随附单证与运单号/箱号一致性",
     "text": "出口报关单『随附单证』栏填写的运输单证号（铁路运单号）应与实际运单一致，集装箱号应与实际箱号一致，否则口岸放行、转关衔接时会因单单不符被退单或查验扣货。",
     "tags": ["waybill_no", "container_no", "crossref"]},
    {"id": "KB-07", "title": "品名与HS归类一致性（陶瓷卫生设备示例）",
     "text": "各单证品名应与报关HS归类对应一致。陶瓷卫生设备（卫生洁具）通常归入HS 6910（瓷制/陶制卫生设备）。发票、装箱单、运单、报关单品名表述不一致，易被认定归类申报错误，触发人工审单与改单。",
     "tags": ["goods_description", "hs", "classification"]},
    {"id": "KB-08", "title": "毛重申报与口岸过磅",
     "text": "铁路口岸对敞顶/抽箱货物常安排过磅。各单证毛重应基于衡器记录填写；行业惯例上单证间毛重差异超过约1%即可能被认定为申报异常，需复核称重记录并更正，否则过磅不符会引发查验与滞留。",
     "tags": ["gross_weight", "tolerance", "weighbridge"]},
    {"id": "KB-09", "title": "件数与铅封查验",
     "text": "装箱单件数应与报关单、运单一致，并与实际装箱计数吻合。口岸查验时会核对集装箱铅封号与件数，件数不符将导致开箱复点、补录或退运，产生口岸作业费与滞留费。",
     "tags": ["total_packages", "seal", "count"]},
    {"id": "KB-10", "title": "欧盟进口清关单证要求",
     "text": "货物进入欧盟（如波兰马拉舍维奇、德国杜伊斯堡清关）通常需要：商业发票、装箱单、铁路运单(CIM)、原产地证明（享受协定税率时）、以及收货人EORI号等。单证缺失或不一致将无法完成进口申报，产生仓储滞留费用。",
     "tags": ["eu", "import", "documents"]},
    {"id": "KB-11", "title": "哈萨克斯坦/俄罗斯段转关要求",
     "text": "经阿拉山口/霍尔果斯口岸出境后，哈萨克斯坦、俄罗斯段凭SMGS运单办理转关。运单收发货人、品名、件数须与出境报关数据一致，否则境外段海关可扣关要求补正；过境转关单证错误是班列境外滞留的常见原因。",
     "tags": ["transit", "kazakhstan", "russia"]},
    {"id": "KB-12", "title": "收发货人名称与海关注册信息",
     "text": "境内发货人应与海关注册登记的企业名称一致，境外收货人名称在各单证间应统一（含大小写与标点）。收发货人不一致会触发海关企业信息比对异常与舱单不匹配，影响退税与收付汇。",
     "tags": ["consignor", "consignee", "party"]},
    {"id": "KB-13", "title": "班列口岸换装与滞留成本",
     "text": "中欧班列在欧亚口岸（如马拉舍维奇、阿拉山口、卡尔斯）需宽轨/标轨换装。行业调研显示欧洲枢纽换装平均耗时约38.7小时，单证不符是造成额外滞留的主因之一；口岸滞留将产生仓储、调箱、滞留等多项费用。",
     "tags": ["transshipment", "detention", "cost"]},
    {"id": "KB-14", "title": "申报金额与发票金额一致",
     "text": "报关单申报总价应与商业发票金额一致（币种相同）。低报可能被认定偷逃税款与骗取退税，高报影响收付汇额度；差额超过极小容差即需更正申报。",
     "tags": ["declared_value", "amount", "invoice"]},
    {"id": "KB-15", "title": "货物描述同义表述与制单规范",
     "text": "外贸制单惯例要求全套单证同一货物使用一致品名。不同单证对同一货物的合理同义改写（词序、简称）在人工审单中通常可辨认，但跨单证品名实体不一致（不同物品词元）会被视为单单不符，需统一后再申报。",
     "tags": ["goods_description", "semantic", "consistency"]},
]

# 检查项 → 附加检索关键词（提升命中相关性）
CHECK_HINTS = {
    "DOC-001": "单证缺失 原产地证书 清关要求",
    "CONS-001": "货物描述 品名 归类一致",
    "CONS-002": "件数 装箱计数 查验",
    "CONS-003": "毛重 过磅 容差",
    "CONS-004": "发货人 名称 注册一致",
    "CONS-005": "收货人 名称 一致",
    "CONS-006": "运单号 随附单证 报关",
    "CONS-007": "集装箱号 箱号 铅封",
    "CONS-008": "申报金额 发票金额",
    "ROUTE-001": "运单类型 SMGS CIM 覆盖 路线",
    "ROUTE-002": "起运国 运抵国 路线 转关",
}


def retrieve(query: str, top_k: int = 2, min_score: float = 0.12) -> list:
    """真实向量检索：对知识条目（标题+正文）计算余弦相似度并排序。"""
    scored = []
    for e in ENTRIES:
        score = semantic.similarity(query, f"{e['title']} {e['text']} {' '.join(e['tags'])}")
        scored.append((e, score))
    scored.sort(key=lambda x: -x[1])
    return [{"entry": e, "score": round(s, 3)} for e, s in scored[:top_k] if s >= min_score]


def basis_for_result(result: dict, top_k: int = 2) -> list:
    """针对单条核验结果检索合规依据。"""
    hint = CHECK_HINTS.get(result.get("check_id", ""), "")
    query = f"{result.get('check_name', '')} {result.get('detail', '')} {hint}"
    return retrieve(query, top_k=top_k)
