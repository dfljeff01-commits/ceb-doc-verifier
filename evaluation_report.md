# 风险分级量化评估报告

- 测试集：19 组模拟单证批次（低/中/高 = 6/7/6）
- 标注口径（独立于模型权重预先确定）：0 个 FAIL=低风险；恰好 1 个 FAIL=中风险；≥2 个 FAIL=高风险
- **模型判定与人工标注一致率：19/19 = 100%**

| 批次 | 场景 | 人工标注 | 模型判定 | 风险分 | FAIL | WARNING | 一致 |
|---|---|---|---|---|---|---|---|
| eval01_clean | 全套单证完全一致、齐全 | 低 | 低 | 0 | 0 | 0 | ✅ |
| eval02_route_smgs_turkey | SMGS运单走土耳其线（路线合规WARNING） | 低 | 低 | 10 | 0 | 1 | ✅ |
| eval03_route_composite_ok | CIM/SMGS统一运单走土耳其线（合规） | 低 | 低 | 0 | 0 | 0 | ✅ |
| eval04_desc_suspect | 报关单品名换序重述（语义存疑0.78） | 低 | 低 | 15 | 0 | 1 | ✅ |
| eval05_cim_on_cis_route | 纯CIM运单经独联体段（覆盖WARNING） | 低 | 低 | 10 | 0 | 1 | ✅ |
| eval06_weight_only_invoice | 毛重栏仅发票载明（可比对单证不足WARNING） | 低 | 低 | 5 | 0 | 1 | ✅ |
| eval07_pkg_mismatch | 报关单箱数475≠480 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval08_weight_drift | 装箱单毛重12500超1%容差 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval09_desc_mismatch | 报关单品名错报（语义相似度0.55） | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval10_missing_coo | 缺少原产地证书 | 中 | 中 | 30 | 1 | 0 | ✅ |
| eval11_consignee_typo | 报关单收货人名称不符 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval12_amount_mismatch | 报关金额低报1.6% | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval15_coo_and_route | 缺产地证+SMGS走土耳其线（1 FAIL+1 WARNING，边界批次） | 中 | 中 | 40 | 1 | 1 | ✅ |
| eval13_pkg_and_weight | 箱数+毛重双不符 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval14_desc_and_pkg | 品名错报+箱数不符 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval16_full_mess | 品名+箱数+毛重+缺产地证四重问题 | 高 | 高 | 100 | 4 | 0 | ✅ |
| eval17_party_and_waybillno | 收货人不符+运单号交叉不符 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval18_container_and_amount | 箱号不符+金额低报 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval19_desc_and_coo | 品名错报+缺产地证 | 高 | 高 | 60 | 2 | 0 | ✅ |