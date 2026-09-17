# 🚂 中欧班列单证智能核验 Demo

> **一句话场景**：中欧班列欧洲枢纽换装平均耗时38.7小时，单证不一致是主因之一——本工具在发运前自动核验单证一致性、齐全性与路线合规性，几秒内发现人工容易漏掉的单证问题，降低边境滞留与退运风险。

面向"AI+行业"类竞赛的单点 AI 工具 Demo：上传真实 PDF 单证或加载模拟批次，系统自动完成 **判型 → 文字提取/OCR → 字段解析 → 多维交叉核验 → 风险评分 → AI 修正建议**，输出结构化核验报告（红/黄/绿 + 可解释的风险分数构成），并支持一键导出 PDF 报告与 API 集成。

## 系统架构（混合AI叙事）

```
PDF上传 ──► 判型(文本/扫描) ──► 文字层直取 / pytesseract OCR
                                    │
示例批次(模拟OCR) ──────────────────┤
                                    ▼
              规则引擎 verification_engine（齐全性/一致性/路线合规 11项检查）
                                    │
                     语义比对 semantic（描述字段语义相似度分级）
                                    ▼
              风险评分模型 risk_model（0-100加权评分 + 可解释分数构成）
                                    │
                       灰色地带（存疑/边界）→ LLM协同推理 llm_layer
                                    │                       （预置结果 + 可实时调用）
                                    ▼
        风险评分 risk_model ──► 💬 对话式助手(工具调用重算) 📧 整改邮件
                                    ▼                              ▼
                    📖 合规依据知识库检索（向量）                    中英双语草稿
        Streamlit 界面（仪表盘/明细/建议/AI推理说明/PDF导出）  +  FastAPI /verify
```

**"规则引擎做筛选 → LLM做复杂判断和解释"**：确定性规则负责硬性校验；语义相似度落在 0.6-0.85 灰色区、或路线合规告警等边界案例，交给 LLM 二次判断并输出自然语言解释。

## 快速开始

```bash
# 1. 需要 Python 3.11+（在 3.12 上开发自测），先创建虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3a. 启动核验API（推荐，页面会自动检测并走API；未启动时页面自动降级直连，不中断演示）
uvicorn api:app --port 8000        # Swagger文档: http://localhost:8000/docs

# 3b. 启动网页
streamlit run app.py               # http://localhost:8501
```

**Docker 一键运行**（已含 OCR 系统依赖，推荐评委/演示环境使用）：

```bash
docker build -t ceb-doc-verifier .
docker run -p 8501:8501 -p 8000:8000 ceb-doc-verifier
# 网页 http://localhost:8501 · API文档 http://localhost:8000/docs
```

## 功能总览

### 1️⃣ 两种单证来源

| 模式 | 说明 |
|---|---|
| 📁 示例批次 | 3组预置批次（等同"OCR已完成"），可用URL直达如 `?batch=batch_with_issues` |
| 📎 上传PDF | 上传最多4份PDF发票/箱单/运单/报关单，自动判型提取，**预览表人工纠正后点"开始核验"** |

PDF处理管线（按页路由，同一PDF可混合判型）：文本型PDF直接抽取文字层（毫秒级）；某页可提取字符数 <50 判为扫描页 → 光栅化后 pytesseract OCR（中文包 chi_sim）。**实测耗时**：文本型 0.06s/份；扫描型 OCR 约 1.7s/页；混合型 1.45s（2页）。

- 单证类型自动判断（文件名+内容关键词打分），界面可人工纠正；
- 字段提取采用"关键词+正则"定位（按单证类型分别设计规则）；
- 必需字段提取失败时**显式标记"提取失败/需人工核对"**，绝不静塞错误值给核验引擎。

### 2️⃣ 核验规则（verification_engine.py，11项）

- **齐全性**：发票/装箱单/运单/报关单/原产地证缺一即 FAIL；
- **一致性**：货物描述（语义相似度分级）、件数、毛重（1%容差）、收发货人（对OCR空格噪声稳健）、运单号交叉引用、集装箱号、金额（±0.5%）；
- **路线合规**：SMGS/CIM 运单覆盖范围 vs 经停国家（如SMGS运单走土耳其线 → WARNING）；报关单起运/运抵国与运单路线端点核对。

### 3️⃣ 语义相似度（semantic.py）

货物描述从"字面全等"升级为相似度分级：**≥0.85 一致 / 0.60-0.85 存疑(需人工复核) / <0.60 不一致**，页面展示具体分数。

> **技术选型如实说明**：默认后端为**字符 1+2-gram 词频向量 + 余弦相似度**（任务书允许的向量化退化方案）。未默认启用 sentence-transformers 多语言句向量的原因：① 模型470MB下载依赖外部网络，现场演示风险高；② 更关键的是多语言句向量对"陶瓷卫浴洁具 vs 卫浴陶瓷制品"这类**字面高度重合的错报对评分偏高（普遍≥0.8）**，会把业务上必须拦下的不一致误判为"一致"，与核验场景的召回要求冲突。代码已通过 `SemanticBackend` 接口预留句向量后端，设置 `SEMANTIC_BACKEND=st` 并安装依赖即可启用。已知局限：跨语言描述（如"卫浴洁具" vs "bathroom sanitary ware"）相似度趋近0，当前后端无法识别为同义。

### 4️⃣ 风险评分模型（risk_model.py）

0-100 加权综合评分 + **可解释分数构成**（每项扣分一目了然）：

| 信号 | 扣分 |
|---|---|
| 必需单证缺失 | 30（每多缺一份 +5） |
| 一致性 FAIL | 首个25，此后每个递增+5（多处硬伤=系统性错报，风险非线性叠加） |
| 语义存疑 WARNING | 15 |
| 路线合规 WARNING | 10 |
| 其他 WARNING | 5 |

分级：**0-20 低风险（绿）/ 21-50 中风险（黄）/ 51-100 高风险（红）**，页面顶部仪表盘展示分数条+分数构成。

**量化评估**（`evaluation.py`，19组标注批次：6低/7中/6高，标注口径独立于权重预先确定为"0 FAIL=低 / 恰好1 FAIL=中 / ≥2 FAIL=高"）：

> **模型风险分级与人工标注一致率：19/19 = 100%**（逐批次明细见 `evaluation_report.md`，可一键复跑验证）

### 5️⃣ LLM 协同推理层（llm_layer.py）——诚实性说明

对灰色地带（语义存疑/路线告警）调用大模型输出"是否同一批货物的合理表述差异"等二次判断与自然语言解释：

- **demo 环境使用预置推理结果**（`llm_presets.json`，文本由大模型离线生成），保证现场演示零外部依赖、不翻车；
- **实时调用路径已实现**（火山方舟 OpenAI 兼容接口）：配置 `ARK_API_KEY`（可选 `ARK_MODEL`/`ARK_BASE_URL`）后，未命中预置的场景自动实时调用；
- 页面每条 AI 意见均标注来源（🟢实时调用 / 📦预置推理结果），**不会让评委误以为全是实时调用**。

### 6️⃣ 原生AI功能（对话式助手 / 整改邮件 / 合规依据检索）

**💬 对话式核验助手**（页面"向AI追问"输入框）：基于当前批次核验结果JSON做自然语言问答（"chat with your data"）。**数值假设类问题走 function calling 范式**——LLM 判断需要推演时调用 `simulate_field_change` 工具，在真实数据上重跑核验引擎与风险模型，再基于真实数字作答（实测："如果箱数改成480" → 工具真实调用 → 回答"风险95/高风险"与引擎重算完全一致，非模型编造）。**实现方式：真实实时调用**（ARK_API_KEY 或 GLM_API_KEY/BIGMODEL_API_KEY，OpenAI兼容接口自动识别）；无 Key 时显式提示"需配置 API Key 启用"，**不预置任何假问答**；页面标注"回答由AI实时生成，可能存在误差，请以核验报告明细为准"。

**📧 AI整改邮件**：一键生成可发送给供应商/货代的中英双语邮件草稿（问题概述→差异点列表（引用真实数值）→需确认内容→期望回复时间），页面上可编辑、可下载。**实现方式：有 Key 时 LLM 实时生成（mode=live）；无 Key 时降级为本批次真实明细数据填充的结构化草稿（mode=offline_template，页面明确标注）**。全部 PASS 的批次提示"无需生成"，不硬造空邮件。

**📖 合规依据检索（RAG-lite）**：为每个 FAIL/WARNING 项检索知识库条文，展示"判断结论+引用依据"（如缺产地证 → 引用产地证签发用途与欧盟清关单证要求条款）。**实现方式：检索为真实的向量检索**（15条公开资料整理摘录构成知识库，复用 semantic 模块的字符n-gram TF余弦排序），解释组织为模板拼接（配置Key后可由LLM整合）。知识库条目为演示用整理摘录、非官方全文，页面与本文均如实标注——**这是简化版RAG，不是全文法规库动态检索**。

### 7️⃣ Android 原生App（Flutter，侧载演示版）

`mobile_app/` 为 Flutter 构建的 Android 演示 App（**侧载安装，未上架应用商店**）：

- **首页**：拍照识别单证 / 相册多选 → 上传后端 `/ingest/image` OCR识别（识别中加载态）；
- **字段预览与人工纠正**：每份单证列出提取字段，低置信度/失败字段 ⚠️ 标注（与Web端同一诚实性设计语言），可编辑后保存；
- **核验结果页**：大号圆环风险分卡片（0-100+红/黄/绿等级色）、分数构成chips、明细列表（状态色条）、AI修正建议；
- **后端地址可配置**：设置页填局域网IP（演示现场不写死）；网络失败（后端未启动/IP错误/超时）均有明确弹窗提示，不白屏不崩溃；
- App 本身零核验逻辑，全部复用 FastAPI `/ingest/image` 与 `/verify`（前后端分离形态）。

**构建产物**（`mobile_app/build/app/outputs/flutter-apk/`）：
- `app-release.apk` 49.0MB（通用，含全部ABI）
- `app-arm64-v8a-release.apk` 17.2MB（现代手机推荐）、armeabi-v7a 14.7MB、x86_64 18.6MB

**安装与演示**：见 `mobile_app/INSTALL.md`（含未知来源开启路径、扫码下载、演示脚本）。
演示后端需 `uvicorn api:app --host 0.0.0.0 --port 8000` 启动（含OCR需在Docker容器内或本机装tesseract）；
演示用单证图片在 `sample_pdfs/images/`（clean 5张全部正确 / issues 4张含问题），
`python serve_apk.py` 可在局域网分发APK并自动生成下载二维码（`apk_qrcode.png`）。

> 构建环境说明：Flutter 3.47.4 stable + JDK17(Temurin) + Android SDK 36，经国内镜像与本机代理完成依赖拉取；
> Dart 单测 2/2 通过（错误映射与文档结构转换）。

### 8️⃣ 工程化

- **API化**：FastAPI `/verify` 接收批次JSON返回核验报告+风险分（`http://localhost:8000/docs` 有Swagger）；Streamlit 优先走API（前后端分离、可被企业系统集成），API未启动自动降级进程内直连并在页面标注当前通道；
- **测试**：pytest 共 61 项（引擎 17 + 风险/语义 18 + PDF摄取 11 + 原生AI 13，含语义分级 0.6/0.85 边界、容差边界、权重递增/封顶、**工具调用循环用脚本化假LLM打桩验证**）；另有 `selftest.py`（26项验收自测，纯标准库）、`_selftest/live_ai_test.py`（真实LLM实测问答与邮件）与 Playwright 端到端测试；
- **容器化**：单镜像同时运行 API+网页，内置 tesseract-ocr + 中文包 chi_sim + poppler-utils，`docker build` + `docker run` 即用；
- **依赖锁定**：requirements.txt 全部精确锁定实测版本。

## 自测与验证

```bash
python selftest.py                         # 规则引擎+评分+语义 验收自测（26项）
python evaluation.py                       # 19组量化评估（输出evaluation_report.md）
python -m pytest -v                        # 全部单元测试（48项）
python _selftest/visual_test.py            # 端到端视觉/交互测试（需另行 pip install playwright）
python _selftest/upload_e2e.py             # 上传PDF模式端到端测试（需playwright）
python _selftest/live_ai_test.py           # 原生AI功能真实LLM实测（需GLM/ARK API Key）
python sample_pdfs/generate_samples.py     # 重新生成测试PDF（文本/扫描/混合/缺关键词）
```



## 目录结构

```
ceb_doc_verifier/
├── app.py                     # Streamlit 主程序（展示层，含PDF上传模式）
├── verification_engine.py     # 核验规则引擎（11项检查，可独立复用）
├── risk_model.py              # 风险评分模型（加权评分+可解释分解）
├── semantic.py                # 语义相似度（字符n-gram TF余弦；预留句向量后端）
├── llm_layer.py               # LLM协同推理层（预置+实时调用双路径）
├── llm_presets.json           # 预置LLM推理结果（离线生成，如实标注来源）
├── pdf_ingest.py              # PDF摄取：判型/OCR/类型识别/字段解析/置信度
├── chat_assistant.py          # 对话式核验助手（LLM+工具调用重算）
├── email_generator.py         # AI整改邮件（LLM实时 + 离线模板降级）
├── knowledge_base.py          # 合规依据知识库 + 向量检索（RAG-lite）
├── llm_endpoint.py            # LLM端点解析（Ark/GLM OpenAI兼容）
├── api.py                     # FastAPI /verify 核验服务
├── start.sh                   # 容器启动脚本（API+网页）
├── Dockerfile                 # 含OCR系统依赖的容器镜像
├── selftest.py                # 验收自测（26项，纯标准库）
├── evaluation.py              # 量化评估（19组批次 → 一致率报告）
├── evaluation_report.md       # 最近一次评估输出
├── test_verification_engine.py / test_risk_model.py / test_pdf_ingest.py / test_native_ai.py
├── mobile_app/                # Flutter Android App（侧载演示版，见 mobile_app/INSTALL.md）
│   ├── lib/main.dart              # App全部代码（拍照/相册→识别→纠正→核验结果）
│   ├── test/                      # Dart单测
│   └── INSTALL.md                 # 安装说明（未知来源开启/扫码下载/演示脚本）
├── serve_apk.py               # 局域网APK分发 + 二维码生成
├── apk_qrcode.png             # APK扫码下载二维码（指向局域网地址）
├── sample_data/               # 3组示例批次（全部通过/4类问题/路线告警）
├── evaluation_set/            # 19组标注评估批次
├── sample_pdfs/               # 测试PDF与生成器（文本/扫描/混合/缺关键词）
├── requirements.txt
├── README.md
└── _selftest/                 # 开发自测工具（可选，需另行安装playwright）
```
