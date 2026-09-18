# 风险分级量化评估报告

- 评估集：**冻结留出集** 19 组（低/中/高 = 5/7/7），评估只读取、不重新生成不覆盖；文件SHA256前16位见文末，供复核比对
- 标注口径（统一后，独立于评分权重）：低=0 FAIL且≤1 WARNING；中=恰好1 FAIL且≤1 WARNING，或0 FAIL且≥2 WARNING（叠加升级）；高=≥2 FAIL，或1 FAIL且≥2 WARNING
- 校准集：`build_cases()` 生成于 `evaluation_set_calibration/`（权重迭代用，与留出集分离，不参与本报告一致率）

## 核心指标

| 指标 | 数值 |
|---|---|
| 分级一致率（标注=口径=模型） | 19/19 = 100% |
| 漏报率（模型分级低于标注） | 0/19 |
| 误报率（模型分级高于标注） | 0/19 |
| 处理失败率 | 0/19 |
| 字段识别率（文本型PDF实跑） | 35/35 = 100.0% |
| 字段待复核率（需人工修正） | 0/35 = 0.0% |
| PDF处理失败率 | 0/4 |

## 逐批次判定

| 批次 | 场景 | 人工标注 | 口径判定 | 模型判定 | 风险分 | FAIL | WARNING | 一致 |
|---|---|---|---|---|---|---|---|---|
| eval01_clean | 全套单证完全一致、齐全 | 低 | 低 | 低 | 0 | 0 | 0 | ✅ |
| eval02_route_smgs_turkey | SMGS运单走土耳其线（路线合规WARNING） | 低 | 低 | 低 | 10 | 0 | 1 | ✅ |
| eval03_route_composite_ok | CIM/SMGS统一运单走土耳其线（合规） | 低 | 低 | 低 | 0 | 0 | 0 | ✅ |
| eval04_desc_suspect | 报关单品名换序重述（语义存疑0.78） | 低 | 低 | 低 | 15 | 0 | 1 | ✅ |
| eval05_cim_on_cis_route | 纯CIM运单经独联体段（覆盖WARNING） | 低 | 低 | 低 | 10 | 0 | 1 | ✅ |
| eval06_weight_only_invoice | 毛重栏仅发票载明（重标注：doc-rules v2.0 启用箱单/运单/报关单缺毛重=FAIL后，本案例为3 FAIL+1 WARNING，按标注口径判高风险；原标注low已废止——业务口径明确"即使发票载有毛重，各单据自身缺失毛重仍不可接受"） | 高 | 高 | 高 | 95 | 3 | 1 | ✅ |
| eval07_pkg_mismatch | 报关单箱数475≠480 | 中 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval08_weight_drift | 装箱单毛重12500超1%容差 | 中 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval09_desc_mismatch | 报关单品名错报（语义相似度0.55） | 中 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval10_missing_coo | 缺少原产地证书 | 中 | 中 | 中 | 30 | 1 | 0 | ✅ |
| eval11_consignee_typo | 报关单收货人名称不符 | 中 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval12_amount_mismatch | 报关金额低报1.6% | 中 | 中 | 中 | 25 | 1 | 0 | ✅ |
| eval13_pkg_and_weight | 箱数+毛重双不符 | 高 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval14_desc_and_pkg | 品名错报+箱数不符 | 高 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval15_coo_and_route | 缺产地证+SMGS走土耳其线（1 FAIL+1 WARNING，边界批次） | 中 | 中 | 中 | 40 | 1 | 1 | ✅ |
| eval16_full_mess | 品名+箱数+毛重+缺产地证四重问题 | 高 | 高 | 高 | 100 | 4 | 0 | ✅ |
| eval17_party_and_waybillno | 收货人不符+运单号交叉不符 | 高 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval18_container_and_amount | 箱号不符+金额低报 | 高 | 高 | 高 | 55 | 2 | 0 | ✅ |
| eval19_desc_and_coo | 品名错报+缺产地证 | 高 | 高 | 高 | 60 | 2 | 0 | ✅ |

## 冻结留出集哈希（审计用）

```
eval01_clean.json: e097b8b61c9ac763
eval02_route_smgs_turkey.json: 72bca9272d2793df
eval03_route_composite_ok.json: fc8e6a6950abf2e8
eval04_desc_suspect.json: 212cdde53a0f99c2
eval05_cim_on_cis_route.json: 07f936074c6086a9
eval06_weight_only_invoice.json: b6fbfcbddd2f9e01
eval07_pkg_mismatch.json: 28e9352f2f0af4f2
eval08_weight_drift.json: b13a2df1654fc87d
eval09_desc_mismatch.json: 26c5ca4a769502a3
eval10_missing_coo.json: 13c41333b1f09d98
eval11_consignee_typo.json: 3d14fcfbdfde02f2
eval12_amount_mismatch.json: f1165390cd23d21d
eval13_pkg_and_weight.json: fec0907f031d0a1a
eval14_desc_and_pkg.json: d9db6100025341b9
eval15_coo_and_route.json: b1624f70a252049b
eval16_full_mess.json: c183627fca579066
eval17_party_and_waybillno.json: cf8c051d14435a17
eval18_container_and_amount.json: 0f30531301e34e95
eval19_desc_and_coo.json: ee369898130bbea5
```

## 诚实性边界

- 本评估基于模拟单证批次，19组一致率**不代表真实单证或真实OCR的准确率**；
- 真实业务准入需用真实脱敏单证（覆盖不同模板/语言/格式/扫描质量/异常组合）由业务人员独立标注后另行验证；
- 标注口径与评分模型为两套实现：口径只看FAIL/WARNING计数，模型按权重打分，二者在冻结集上必须一致（不一致时评估退出码为1）；已知理论边界：1 FAIL+2条轻度WARNING(各5分)时口径判high而评分≤50分为medium，冻结集内无此类案例，出现即判失败并需重新校准。