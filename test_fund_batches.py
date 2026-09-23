# -*- coding: utf-8 -*-
"""资金/费用批次测试（数据核对模块v1.1）。

覆盖：
  - 真实回归数据：7-18两笔预付（1对多）、8月三趟车共用一笔（多对1，
    含8月9日记0不应报警）、10月五趟车两笔预付（多对多）；
  - 批次级核对在批次总量层面判定，不逐趟车报警；
  - 用途分类（修订版）：保证金/补贴回款不参与运费覆盖核对，默认值由用途带出，
    人工改写需原因；「其他」必须带说明；
  - 单趟车来源：来自批次时 suppress_single_alert；
  - 孤儿检测：不存在编号、旧表与批次重复登记。

所有金额直接采用任务书给定真实数字（无企业名称：对方主体留空/占位）。
运行：pytest test_fund_batches.py -v
"""

from decimal import Decimal

import fund_recon
import fund_store
import train_store
import train_recon


# ---------------------------------------------------------------- 测试夹具

# 真实班列编号（任务书表）
TRIP_0718 = "20250718-PW-MZL-RU-T"
TRIP_0808 = "20250808-DT-EL-RU-T"
TRIP_0809 = "20250809-ZD-HGS-ZY-T"
TRIP_0813 = "20250813-ZD-HGS-ZY-T"
TRIP_1003 = "20251003-PW-MZL-RU-T"
TRIP_1004 = "20251004-PW-MZL-RU-T"
TRIP_1005 = "20251005-PW-MZL-RU-T"
TRIP_1010_L = "20251010-PW-MZL-RU-L"
TRIP_1010_T = "20251010-PW-MZL-RU-T"

OCTOBER_TRIPS = [TRIP_1003, TRIP_1004, TRIP_1005, TRIP_1010_L, TRIP_1010_T]


def _settle_row(trip_no: str, settle_total) -> dict:
    return {"trip_no": trip_no, "settle_total": settle_total,
            "rail_freight": None, "customs_fee": None,
            "service_fee": None, "other_fee": None}


# ---------------------------------------------------------------- 1. 真实批次核对

def test_july_two_prepays_one_trip():
    """7-18：同一趟车分两笔钱结清（1对多）。批次维度各自核对：
    批次1总额1,426,003.11、批次2总额931,934.17，均少于该车结算合计
    （任务书未给出7-18结算合计，按批次总量提示差额即可）。"""
    settlement = _settle_row(TRIP_0718, Decimal("2357937.28"))
    b1 = {"batch_id": "BATCH-20250701-PREPAY-01", "batch_type": "prepay",
          "fund_purpose": "预付运费", "total_amount": Decimal("1426003.11"),
          "participates_in_freight_recon": True}
    r1 = fund_recon.batch_coverage_check(b1, [settlement])
    # 批次1单独看不足（差额 = 2,357,937.28 - 1,426,003.11）
    assert r1["verdict"] == "shortage"
    assert r1["diff"] == Decimal("931934.17")

    # 两笔合在一起（模拟业务员视角的批次组）应恰好覆盖
    b2 = dict(b1, batch_id="BATCH-20250709-PREPAY-01",
              total_amount=Decimal("931934.17"))
    combined = fund_recon.batch_coverage_check(
        dict(b1, total_amount=b1["total_amount"] + b2["total_amount"]),
        [settlement])
    assert combined["verdict"] == "covered"
    assert combined["diff"] == Decimal("0")


def test_august_multi_to_one_batch_totals():
    """多对1：三趟车共用一笔5,663,525.40预付款。
    批次覆盖核对在批次层面：三车结算合计 6,647,085.30 vs 批次额，
    正确反映差额 -983,559.90，而不是逐趟车判断（任务书第70行专项）。"""
    trip_rows = [
        _settle_row(TRIP_0808, Decimal("2581233.30")),
        _settle_row(TRIP_0809, Decimal("2034027.60")),
        _settle_row(TRIP_0813, Decimal("2031824.40")),
    ]
    batch = {"batch_id": "BATCH-20250801-PREPAY-01", "batch_type": "prepay",
             "fund_purpose": "预付运费",
             "total_amount": Decimal("5663525.40"),
             "participates_in_freight_recon": True}
    result = fund_recon.batch_coverage_check(batch, trip_rows)
    assert result["covered_total"] == Decimal("6647085.30")
    assert result["verdict"] == "shortage"
    assert result["diff"] == Decimal("983559.90")
    assert "批次" in result["message"]


def test_aug09_trip_no_false_shortage_alert():
    """8月9日这趟车单独看预付款记0（联运记账在第一天）——
    trip_funding_context 必须抑制单车层面的覆盖报警。"""
    links = [{
        "batch_id": "BATCH-20250801-PREPAY-01",
        "batch_type": "prepay",
        "fund_purpose": "预付运费",
        "trip_count": 3,
        "allocated_amount": None,          # 未分摊：本车账面预付为0
        "batch_verdict": "shortage",
    }]
    ctx = fund_recon.trip_funding_context(TRIP_0809, links)
    assert ctx["suppress_single_alert"] is True
    assert "本趟车不再单独判断" in ctx["message"]
    assert "3趟车" in ctx["message"]


def test_october_many_to_many_two_batches():
    """多对多：五趟车、两笔预付款（11,500,000 + 994,777.62 = 12,494,777.62）。
    批次层面合并核对五车结算合计；任务书未给10月结算合计，
    构造口径验证批次合计取数与逐趟车不单独判断。"""
    # 五车结算合计（构造：与两笔批次总额一致，验证批次层 covered）
    settle = Decimal("12494777.62")
    trip_rows = [_settle_row(no, settle / 5) for no in OCTOBER_TRIPS]

    def check(amount):
        batch = {"batch_id": "BATCH-X", "batch_type": "prepay",
                 "fund_purpose": "预付运费", "total_amount": amount,
                 "participates_in_freight_recon": True}
        return fund_recon.batch_coverage_check(batch, trip_rows)

    # 仅第一笔（11,500,000）：不足
    r1 = check(Decimal("11500000"))
    assert r1["verdict"] == "shortage"
    # 两笔合计：覆盖
    r_all = check(Decimal("11500000") + Decimal("994777.62"))
    assert r_all["verdict"] == "covered"
    assert r_all["covered_total"] == settle


# ---------------------------------------------------------------- 2. 用途分类（修订版）

def test_purpose_default_participates():
    assert fund_recon.default_participates("预付运费") is True
    assert fund_recon.default_participates("尾款结算") is True
    assert fund_recon.default_participates("补贴回款") is False
    assert fund_recon.default_participates("保证金") is False


def test_deposit_batch_skipped_from_recon():
    """保证金批次不参与运费覆盖核对：结果 skipped，无差异。"""
    trip_rows = [_settle_row(TRIP_0809, Decimal("2034027.60"))]
    batch = {"batch_id": "BATCH-HIST", "batch_type": "prepay",
             "fund_purpose": "保证金",
             "total_amount": Decimal("500000"),
             "participates_in_freight_recon": False}
    result = fund_recon.batch_coverage_check(batch, trip_rows)
    assert result["skipped"] is True
    assert result["verdict"] == "skipped"
    assert result["diff"] is None


def test_subsidy_purpose_hint_links_module():
    """补贴回款批次跳过且提示去补贴对账模块。"""
    batch = {"batch_id": "BATCH-SUB", "batch_type": "prepay",
             "fund_purpose": "补贴回款", "total_amount": Decimal("100000"),
             "participates_in_freight_recon": False}
    result = fund_recon.batch_coverage_check(batch, [])
    assert "补贴对账" in result["message"]


def test_other_purpose_requires_remark():
    assert fund_recon.validate_purpose("其他", "") is not None
    assert fund_recon.validate_purpose("其他", "代垫海运费") is None
    assert fund_recon.validate_purpose("未知科目", "x") is not None


def test_historical_purpose_hidden_from_default_options():
    """新建表单默认选项不含保证金（收进历史科目）。"""
    assert "保证金" not in fund_recon.CURRENT_PURPOSES
    assert "保证金" in fund_recon.HISTORICAL_PURPOSES


# ---------------------------------------------------------------- 3. 端到端存储：创建→查询→改写→删除

def test_store_create_and_get_batch():
    # 先通过 v1 登记三趟班列，用其实际生成的统一编号（编号口径由v1决定）
    specs = [
        ("2025-08-08", "DT", "EL", "RU", "2581233.30"),
        ("2025-08-09", "ZD", "HGS", "RU", "2034027.60"),
        ("2025-08-13", "ZD", "HGS", "RU", "2031824.40"),
    ]
    trip_nos = []
    for dep, station, port, dest, total in specs:
        trip = train_store.create_trip(
            dep, station, port, dest, "T", "t", code_map={
                "station": {"DT": "二连", "ZD": "中鼎"},
                "port": {"EL": "二连", "HGS": "霍尔果斯"},
                "dest": {"RU": "俄罗斯"}})
        no = trip["trip_no"]
        trip_nos.append(no)
        fund_store.db.query(
            "INSERT INTO train_settlements (trip_no, settle_total)"
            " VALUES (%s, %s)", (no, Decimal(total)))
    batch = fund_store.create_batch(
        batch_type="prepay", total_amount="5663525.40",
        paid_at="2025-08-01", fund_purpose="预付运费",
        trip_nos=trip_nos,
        allocations={trip_nos[0]: "2581233.30"},
        by="finance_test")
    assert batch["batch_id"].startswith("BATCH-20250801-PREPAY-")
    assert len(batch["trips"]) == 3
    # 只对第一趟车填了分摊
    allocated = {t["trip_no"]: t["allocated_amount"]
                 for t in batch["trips"] if t["allocated_amount"] is not None}
    assert allocated == {trip_nos[0]: Decimal("2581233.30")}
    # 三车结算合计 6,647,085.30 > 批次额 5,663,525.40 → shortage
    assert batch["coverage"]["verdict"] == "shortage"
    assert batch["coverage"]["diff"] == Decimal("983559.90")


def test_store_cost_batch_requires_category():
    import pytest
    with pytest.raises(fund_store.FundStoreError):
        fund_store.create_batch(
            batch_type="cost", total_amount="10000",
            paid_at="2025-08-01", trip_nos=[TRIP_0809], by="t")


def test_store_cost_batch_coverage_by_fee_field():
    """费用批次：覆盖金额取对应费用科目（报关费），不是结算合计。"""
    batch = fund_store.create_batch(
        batch_type="cost", total_amount="3200",
        paid_at="2025-08-01", fund_purpose="预付运费",
        cost_category="报关费", trip_nos=[TRIP_0808, TRIP_0809],
        by="finance_test")
    # 关联班列无结算行 → 口径合计0，批次总额3200，under
    assert batch["coverage"]["verdict"] == "under"
    assert batch["coverage"]["covered_total"] == 0


def test_set_participates_requires_reason_and_audits():
    batch = fund_store.create_batch(
        batch_type="prepay", total_amount="5000",
        paid_at="2025-08-01", trip_nos=[TRIP_0809], by="t")
    import pytest
    with pytest.raises(fund_store.FundStoreError):
        fund_store.set_participates(batch["batch_id"], False, "", "t")
    updated = fund_store.set_participates(
        batch["batch_id"], False, "该笔为历史保证金误登记", "t")
    assert updated["participates_in_freight_recon"] is False
    assert updated["recon_override_reason"] == "该笔为历史保证金误登记"


def test_trip_batch_links_lookup():
    fund_store.create_batch(
        batch_type="prepay", total_amount="5663525.40",
        paid_at="2025-08-01", trip_nos=[TRIP_0808, TRIP_0809], by="t")
    links = fund_store.trip_batch_links(TRIP_0809)
    assert len(links) == 1
    assert links[0]["batch_id"].startswith("BATCH-20250801-PREPAY-")
    assert links[0]["trip_count"] == 2


# ---------------------------------------------------------------- 4. 孤儿检测

def test_orphan_check_clean():
    # 关联的编号都存在：需先登记班列（create_batch 允许先关联，
    # 这里直接测纯函数判定：全部已知 → 无问题）
    result = fund_recon.orphan_check(
        batch_links=[{"batch_id": "B1", "trip_no": TRIP_0809,
                      "batch_type": "prepay", "participates": True}],
        known_trip_nos={TRIP_0809}, legacy_prepay_trip_nos=set())
    assert result["has_issue"] is False


def test_orphan_check_unknown_trip():
    result = fund_recon.orphan_check(
        batch_links=[{"batch_id": "B1", "trip_no": "20250999-XX-XX-XX-X",
                      "batch_type": "prepay", "participates": True}],
        known_trip_nos=set(), legacy_prepay_trip_nos=set())
    assert result["has_issue"] is True
    assert len(result["unknown_trip_links"]) == 1


def test_orphan_check_duplicate_registration():
    """同一趟车既在旧表登记预付、又纳入参与核对的批次 → 重复风险。"""
    result = fund_recon.orphan_check(
        batch_links=[{"batch_id": "B1", "trip_no": TRIP_0809,
                      "batch_type": "prepay", "participates": True}],
        known_trip_nos={TRIP_0809},
        legacy_prepay_trip_nos={TRIP_0809})
    assert len(result["duplicate_risk"]) == 1
    assert result["duplicate_risk"][0]["batches"] == ["B1"]


def test_orphan_check_ignores_non_participating_duplicate():
    """不参与核对的批次（保证金）即使与旧表同车也不报重复计算。"""
    result = fund_recon.orphan_check(
        batch_links=[{"batch_id": "B1", "trip_no": TRIP_0809,
                      "batch_type": "prepay", "participates": False}],
        known_trip_nos={TRIP_0809},
        legacy_prepay_trip_nos={TRIP_0809})
    assert result["has_issue"] is False
