# -*- coding: utf-8 -*-
"""JSON → PostgreSQL 数据迁移测试（架构升级任务书 §1/§6 验收）。

用旧版存储口径构造代表性JSON记录（含不同单证构成/风险等级/编号索引），
迁移进 PostgreSQL 后校验：批次/单据/问题行数一致、逐批次回读深度比对无损、
created_at/source 原值保留、迁移幂等（重跑不重复）。
运行：pytest test_migration.py -v
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import mobile_store
import migrate_json_to_pg
from verification_engine import run_verification


def _norm(v):
    return re.sub(r"\s+", "", str(v or "")).upper()


def _short(t, limit=60):
    t = " ".join(str(t or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def summarize_v1(docs, ver):
    """旧版 mobile_store.summarize 口径（迁移源数据由旧版本落盘）。"""
    risk = ver.get("risk", {})
    grade = risk.get("grade", "low")
    top = None
    for status in ("FAIL", "WARNING"):
        top = next((r for r in ver.get("results", [])
                    if r.get("status") == status), None)
        if top is not None:
            break
    one = (f"{top.get('check_name', '核验提示')}：{_short(top.get('detail'))}"
           if top else "全部检查项通过，未发现问题")
    return {"risk_grade": grade,
            "risk_level": {"low": "green", "medium": "yellow",
                           "high": "red"}.get(grade, "green"),
            "risk_label": risk.get("grade_label", ""),
            "risk_score": int(risk.get("score", 0)),
            "one_line": one, "doc_count": len(docs),
            "doc_types": sorted({d.get("doc_type") or "unknown" for d in docs})}


def identity_v1(docs):
    idx = {}
    for d in docs:
        for k in ["invoice_no", "packing_list_no", "waybill_no",
                  "declaration_no", "co_no", "container_no"]:
            v = (d.get("fields") or {}).get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            idx.setdefault(k, [])
            if _norm(v) not in idx[k]:
                idx[k].append(_norm(v))
    return idx


def _legacy_record(tag, docs_spec, created_at, source="mobile_app"):
    batch_id = f"MB-20260901-120000-{tag}"
    docs = [{"doc_type": t, "doc_id": f"{batch_id}-{i}", "title": titles.get(t, t),
             "fields": fields}
            for i, (t, fields) in enumerate(docs_spec, start=1)]
    ver = run_verification({"batch_id": batch_id, "batch_name": f"迁移验证 {tag}",
                            "documents": docs})
    return {
        "batch_id": batch_id,
        "created_at": created_at,
        "source": source,
        "lite": summarize_v1(docs, ver),
        "identity_numbers": identity_v1(docs),
        "documents": docs,
        "verification": ver,
    }


titles = {"invoice": "商业发票", "packing_list": "装箱单",
          "railway_waybill": "铁路运单", "export_customs_declaration": "出口报关单",
          "certificate_of_origin": "原产地证书"}


def _build_legacy_dir(tmp_path: Path, count: int = 3) -> Path:
    tag = uuid.uuid4().hex[:4].upper()
    specs = [
        # 干净整批（5类单证，低风险）
        [("invoice", {"invoice_no": f"INV-{tag}-1", "total_packages": 100,
                      "gross_weight_kg": 2500, "currency": "USD",
                      "total_amount": 18000}),
         ("packing_list", {"packing_list_no": f"PL-{tag}-1", "total_packages": 100,
                           "gross_weight_kg": 2500}),
         ("railway_waybill", {"waybill_no": f"SMU/T/{tag}-01",
                              "goods_description": "陶瓷餐具", "total_packages": 100,
                              "gross_weight_kg": 2500,
                              "container_no": f"MSKU{tag}0001"}),
         ("export_customs_declaration", {"declaration_no": f"DEC-{tag}-1",
                                         "total_amount": 18000}),
         ("certificate_of_origin", {"co_no": f"CO-{tag}-1"})],
        # 单发票批次（高风险：缺单证）
        [("invoice", {"invoice_no": f"INV-{tag}-2", "total_packages": 40,
                      "gross_weight_kg": 900, "currency": "EUR",
                      "total_amount": 5200})],
        # 双运单批次（App现场多运单）
        [("invoice", {"invoice_no": f"INV-{tag}-3", "total_packages": 60,
                      "gross_weight_kg": 1500, "currency": "USD",
                      "total_amount": 9000}),
         ("railway_waybill", {"waybill_no": f"SMU/T/{tag}-02",
                              "goods_description": "瓷砖", "total_packages": 30,
                              "gross_weight_kg": 750}),
         ("railway_waybill", {"waybill_no": f"SMU/T/{tag}-03",
                              "goods_description": "瓷砖", "total_packages": 30,
                              "gross_weight_kg": 750})],
    ]
    directory = tmp_path / f"mobile_results_{tag}"
    directory.mkdir()
    for i in range(count):
        rec = _legacy_record(f"{tag}{i:02X}", specs[i % len(specs)],
                             f"2026-09-0{i + 1}T08:30:00")
        (directory / f"{rec['batch_id']}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return directory


def test_migration_lossless_and_idempotent(tmp_path):
    directory = _build_legacy_dir(tmp_path)
    records = migrate_json_to_pg.load_legacy_records(directory)
    assert len(records) == 3
    before = mobile_store.counts()

    # 迁移 + 校验
    assert migrate_json_to_pg.migrate(records) == 3
    ok, result = migrate_json_to_pg.verify(records)
    assert ok, result["problems"]
    after = mobile_store.counts()
    assert after["batches"] == before["batches"] + 3
    # 单据行数与源一致
    src_docs = sum(len(r["documents"]) for r in records)
    assert after["documents"] == before["documents"] + src_docs
    assert after["issues"] >= before["issues"]

    # 逐字段无损：created_at / source / 元信息原值保留
    for rec in records:
        got = mobile_store.get_batch(rec["batch_id"])
        assert got is not None
        assert got["created_at"] == rec["created_at"]
        assert got["source"] == rec["source"]
        assert got["lite"] == rec["lite"]
        assert got["identity_numbers"] == rec["identity_numbers"]
        assert got["documents"] == rec["documents"]
        assert got["verification"] == rec["verification"]
        # 正式表行：单据状态与问题级别
        issues = _issues_of(rec["batch_id"])
        src_levels = _src_levels(rec)
        assert issues == src_levels

    # 幂等重跑：不新增批次/单据/问题行
    migrate_json_to_pg.migrate(records)
    ok2, result2 = migrate_json_to_pg.verify(records)
    assert ok2, result2["problems"]
    again = mobile_store.counts()
    assert again["batches"] == after["batches"]
    assert again["documents"] == after["documents"]
    assert again["issues"] == after["issues"]


def _issues_of(batch_id):
    rows = mobile_store.db.query(
        "SELECT doc_id, level FROM verification_issues WHERE batch_id=%s"
        " ORDER BY id", (batch_id,))
    return [(r["doc_id"], r["level"]) for r in rows]


def _src_levels(rec):
    levels = []
    for g in rec["verification"].get("document_groups", []):
        for issue in g.get("issues", []):
            if issue.get("status") in ("FAIL", "WARNING"):
                levels.append((g.get("doc_id"), issue["status"]))
    for issue in rec["verification"].get("batch_level_issues", []):
        if issue.get("status") in ("FAIL", "WARNING"):
            levels.append((None, issue["status"]))
    return levels


def test_verify_only_mode_does_not_write(tmp_path):
    directory = _build_legacy_dir(tmp_path, count=1)
    records = migrate_json_to_pg.load_legacy_records(directory)
    before = mobile_store.counts()["batches"]
    # 未迁移直接校验：应检出"库内未找到"
    ok, result = migrate_json_to_pg.verify(records)
    assert not ok
    assert any("数据库中未找到" in p for p in result["problems"])
    assert mobile_store.counts()["batches"] == before
