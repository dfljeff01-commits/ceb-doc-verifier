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

**局域网分享**（已实测：容器与裸启动均绑定 `0.0.0.0`，同事浏览器直接访问即可）：

```bash
# 查看本机局域网IP（Windows）：ipconfig   → 示例：192.168.5.44
docker run -p 8501:8501 -p 8000:8000 ceb-doc-verifier
# 同事访问: http://192.168.5.44:8501 （API文档: http://192.168.5.44:8000/docs）
```

**远程分享（cloudflared 临时隧道，已实测可用）**：

```bash
cloudflared tunnel --url http://localhost:8501
# 输出形如 https://xxxx.trycloudflare.com 的临时公网地址，演示结束关闭终端即失效。
# ⚠️ 临时链接无身份验证：请勿分享给不信任的人，不要长时间挂着不关（2026-09-18 实测
#    一次完整验证：HTTP 200、首屏正常渲染；验证后隧道已关闭）。
```

**界面体验（v1.4 前端优化）**：

- **首屏聚焦**：场景说明下方为卡片式来源选择（示例批次 / 上传PDF），配四步流程指示器（选择/上传 → 核对识别 → 查看报告 → 生成材料），当前步骤高亮；
- **风险仪表盘**：核验结果以大号环形风险分仪表（0-100，红/黄/绿）为全页视觉焦点，汇总卡与可解释分数构成同屏；
- **明细分层**：FAIL/WARNING 问题卡片默认展开（含修正建议与规则版本），PASS 项折叠进"全部通过项"，完整明细表二级收纳；
- **配色统一**：红/黄/绿风险色在仪表盘、明细、建议、知识库各区块使用同一组色值（GRADE_COLORS 唯一定义）；
- **交互反馈**：OCR 解析有进度条、核验计算/邮件生成/对话回答均有 loading 指示；对话助手收进折叠面板，避免自动聚焦把页面拉离首屏；
- **移动端说明**：Streamlit 窄屏下布局可纵向堆叠、无错位（390px 实测），但宽表格体验一般——**移动端请优先使用配套 Android App**，网页端建议 PC 浏览（Streamlit 框架本身的局限，未做深度适配）。

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

### 2️⃣ 核验规则（verification_engine.py，14项）

- **输入完整性**：批次结构契约检查（documents元素/fields类型，SYS-001）；
- **齐全性**：发票/装箱单/运单/报关单/原产地证缺一即 FAIL；单证字段完整性（空单证/缺编号显式告警，DOC-002）；同类型重复单证冲突检查（重复且矛盾判FAIL、完全一致提示去重，DOC-003）；
- **一致性**：货物描述（语义相似度分级 **+关键实体守卫**：数字/单位/规格/否定词矛盾时无论相似度多高都判不一致）、件数（**整数契约**，0/负数/非数字FAIL、小数待复核）、毛重（1%容差；**数值合法性门槛**：0/负数/NaN判FAIL、带单位字符串判待复核）、收发货人（对OCR空格噪声稳健）、运单号交叉引用（覆盖每一份报关单）、集装箱号、金额（±0.5%；**币种先行**：缺失待复核、矛盾FAIL）；
- **路线合规**：运单类型**限定受支持枚举**（SMGS / CIM / CIM-SMGS统一运单；枚举外如AIR WAYBILL判"类型不支持，需人工复核"，不进入路线判断）；覆盖集合按运单类型取对应集合，统一运单取SMGS∪CIM**并集**；并集外国家（如美国）判FAIL；每条路线判断结果附带 **rule_version / rule_coverage 字段与适用边界说明**（规则版本 `route-rules v2.0`，仅覆盖中欧班列国际铁路联运25+18国，非全球通用合规判断）；报关单起运/运抵国与运单路线端点核对。

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

**量化评估**（`evaluation.py`，评估集分两层，不混用自证）：

- **冻结留出集** `evaluation_set/`：19组标注批次（6低/7中/6高），固定文件，评估**只读取、不重新生成不覆盖**，文件SHA256写入报告供审计；
- **校准集**：`build_cases()` 生成于 `evaluation_set_calibration/`（`--rebuild-calibration` 时刷新），供权重迭代，不参与一致率口径；
- **标注口径（统一后，独立于评分权重）**：低=0 FAIL且≤1 WARNING；中=恰好1 FAIL且≤1 WARNING，或0 FAIL且≥2 WARNING（叠加升级）；高=≥2 FAIL，或1 FAIL且≥2 WARNING；
- **报告指标**：分级一致率、漏报率（模型分级低于标注）、误报率（高于标注）、处理失败率、字段识别率（文本型PDF实跑高置信提取/应提取）、字段待复核率（需人工修正）。

> **模型风险分级与人工标注三方一致率（标注=口径=模型）：19/19 = 100%**，漏报0、误报0（逐批次明细见 `evaluation_report.md`，可一键复跑验证；此为模拟批次口径，不代表真实单证准确率）

### 5️⃣ LLM 协同推理层（llm_layer.py）——诚实性说明

对灰色地带（语义存疑/路线告警）调用大模型输出"是否同一批货物的合理表述差异"等二次判断与自然语言解释：

- **demo 环境使用预置推理结果**（`llm_presets.json`，文本由大模型离线生成），保证现场演示零外部依赖、不翻车；
- **实时调用路径已实现**（火山方舟 OpenAI 兼容接口）：配置 `ARK_API_KEY`（可选 `ARK_MODEL`/`ARK_BASE_URL`）后，未命中预置的场景自动实时调用；
- 页面每条 AI 意见均标注来源（🟢实时调用 / 📦预置推理结果），**不会让评委误以为全是实时调用**。

### 6️⃣ 原生AI功能（对话式助手 / 整改邮件 / 合规依据检索）

**💬 对话式核验助手**（页面"向AI追问"输入框）：基于当前批次核验结果JSON做自然语言问答（"chat with your data"）。**数值假设类问题走 function calling 范式并带强制重算门卫**——LLM 判断需要推演时调用 `simulate_field_change` 工具，在真实数据上重跑核验引擎与风险模型；**若LLM不调用工具、直接编造数字（如"风险0"），系统识别后拒绝展示该推演结果并提示"未能完成计算验证"**；正常路径下回答强制附带"计算验证（核验引擎真实重算）"权威块，数字以程序重算为准而非模型报数。会话按**数据版本**关联：字段编辑/换文件后旧对话失效并明确提示。**实现方式：真实实时调用**（ARK_API_KEY 或 GLM_API_KEY/BIGMODEL_API_KEY，OpenAI兼容接口自动识别）；无 Key 时显式提示"需配置 API Key 启用"，**不预置任何假问答**。

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

- **API化**：FastAPI `/verify` 接收批次JSON返回核验报告+风险分（`http://localhost:8000/docs` 有Swagger）。**请求体用 Pydantic 模型严格校验**——结构错误（documents含null、fields类型错误等）返回422并指明具体字段，不会500；上传端点有**文件大小（图片10MB/PDF20MB→413）、页数（>20页→413）、处理耗时（>120s→504）三重限制**，OCR等阻塞操作在受限工作线程池执行。Streamlit 优先走API（前后端分离、可被企业系统集成），API未启动自动降级进程内直连并在页面标注当前通道；
- **数据版本**：邮件草稿、对话会话、PDF报告全部关联**单证内容哈希版本**（`doc_contract.data_version`）——字段编辑/换文件后旧草稿失效并提示重新生成，邮件**下载读取当前编辑后的文本**，上传缓存按**文件名+内容SHA256**判重；
- **测试**：pytest 共 114 项（引擎+API契约 52 + 风险/语义/评估 23 + PDF摄取 18 + 原生AI/数据版本 21，含 F01-F10 全部审查反例回归）；另有 `selftest.py`（26项验收自测，纯标准库）、`_selftest/live_ai_test.py`（真实LLM实测问答与邮件）与 Playwright 端到端测试；
- **版本管理**：本项目自审查整改起使用 Git 仓库管理，修复按 F0x 分组提交，commit 信息可追溯；容器构建使用多阶段缓存并内置 HEALTHCHECK；APK 重建产物含 `.sha1` 哈希文件；
- **容器化**：单镜像同时运行 API+网页，内置 tesseract-ocr + 中文包 chi_sim + poppler-utils，`docker build` + `docker run` 即用；`start.sh` 管理 API 进程生命周期（API退出则容器退出，网页不"假活"）；
- **依赖锁定**：requirements.txt 全部精确锁定实测版本（含上传路由所需 python-multipart、APK分发二维码所需 qrcode）。

## 自测与验证

```bash
python selftest.py                         # 规则引擎+评分+语义 验收自测（26项）
python evaluation.py                       # 冻结留出集评估（只读评估集，输出evaluation_report.md）
python evaluation.py --rebuild-calibration # 重新生成校准集（写入evaluation_set_calibration/）
python -m pytest -v                        # 全部单元测试（114项）
python _selftest/visual_test.py            # 端到端视觉/交互测试（需另行 pip install playwright）
python _selftest/upload_e2e.py             # 上传PDF模式端到端测试（需playwright）
python _selftest/live_ai_test.py           # 原生AI功能真实LLM实测（需GLM/ARK API Key）
python sample_pdfs/generate_samples.py     # 重新生成测试PDF（文本/扫描/混合/缺关键词）
```

离线复跑（PowerShell，关闭外部AI凭据与可选语义模型下载路径后执行）：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
foreach ($reviewKey in @('ARK_API_KEY','GLM_API_KEY','BIGMODEL_API_KEY','OPENAI_API_KEY','SEMANTIC_BACKEND')) {
    Remove-Item -LiteralPath ('Env:\' + $reviewKey) -ErrorAction SilentlyContinue
}
.\.venv\Scripts\python.exe -m pytest -v -p no:cacheprovider test_verification_engine.py test_risk_model.py test_pdf_ingest.py test_native_ai.py
.\.venv\Scripts\python.exe selftest.py
```



## 目录结构

```
ceb_doc_verifier/
├── app.py                     # Streamlit 主程序（展示层，含PDF上传模式）
├── verification_engine.py     # 核验规则引擎（14项检查，可独立复用）
├── doc_contract.py            # 数据契约层：类型枚举/必填字段/数值解析/路线规则版本/数据版本
├── risk_model.py              # 风险评分模型（加权评分+可解释分解）
├── semantic.py                # 语义相似度（字符n-gram TF余弦+关键实体守卫；预留句向量后端）
├── llm_layer.py               # LLM协同推理层（预置+实时调用双路径）
├── llm_presets.json           # 预置LLM推理结果（离线生成，如实标注来源）
├── pdf_ingest.py              # PDF摄取：判型/OCR/类型识别/字段解析/置信度(high/review/missing)
├── chat_assistant.py          # 对话式核验助手（LLM+工具调用重算+强制计算验证门卫）
├── email_generator.py         # AI整改邮件（LLM实时 + 离线模板降级）
├── knowledge_base.py          # 合规依据知识库 + 向量检索（RAG-lite）
├── llm_endpoint.py            # LLM端点解析（Ark/GLM OpenAI兼容）
├── api.py                     # FastAPI /verify 核验服务（Pydantic请求契约+上传限制+线程池）
├── start.sh                   # 容器启动脚本（API+网页，API生命周期受管）
├── Dockerfile                 # 含OCR系统依赖的容器镜像（HEALTHCHECK）
├── selftest.py                # 验收自测（26项，纯标准库）
├── evaluation.py              # 量化评估（冻结留出集只读 + 校准集分离 + 多指标报告）
├── evaluation_report.md       # 最近一次评估输出（含留出集SHA256）
├── test_verification_engine.py / test_risk_model.py / test_pdf_ingest.py / test_native_ai.py
├── mobile_app/                # Flutter Android App（侧载演示版，见 mobile_app/INSTALL.md）
│   ├── lib/main.dart              # App全部代码（拍照/相册→识别→契约字段编辑→核验结果）
│   ├── test/                      # Dart单测（6项）
│   └── INSTALL.md                 # 安装说明（未知来源开启/扫码下载/演示脚本）
├── serve_apk.py               # 局域网APK分发 + 二维码生成
├── apk_qrcode.png             # APK扫码下载二维码（指向局域网地址）
├── sample_data/               # 3组示例批次（全部通过/4类问题/路线告警）
├── evaluation_set/            # 19组标注评估批次（冻结留出集，评估只读）
├── evaluation_set_calibration/ # 校准集（--rebuild-calibration 时生成）
├── sample_pdfs/               # 测试PDF与生成器（文本/扫描/混合/缺关键词）
├── requirements.txt
├── README.md
└── _selftest/                 # 开发自测工具（可选，需另行安装playwright）
```

## 能力边界与诚实性声明（演示时必须如实说明）

- **真实校验**：14项交叉核验规则、风险加权评分与分数构成、语义相似度分级与关键实体守卫、上传PDF的判型/文字提取/OCR/字段解析与置信度标记、知识库向量检索、API请求契约与上传限制——以上均为程序真实执行，可离线复跑验证；
- **简化规则**：路线覆盖集合、容差阈值、风险权重为演示用简化口径（每条路线结果附规则版本与覆盖范围，规则版本 `route-rules v2.0`），不代表实际铁路/海关法规全貌；
- **AI部分**：对话助手与邮件在配置API Key后为LLM实时生成（数值推演经强制重算验证）；未配置Key时对话助手显式不可用、邮件降级为数据填充模板，页面均如实标注来源；
- **量化评估**：19组一致率基于模拟批次，不代表真实单证或真实OCR准确率；真实OCR未做独立验收，演示时请使用 `sample_pdfs/` 已验证样本；
- **真实单证验证（待办）**：需由业务人员用真实脱敏单证（覆盖不同模板、语言、格式、扫描质量、异常组合）独立标注验证，当前未完成。

## 已知限制与生产化前置条件（本轮不要求实现，真实业务/生产准入前必须解决）

> 当前状态仅为**竞赛演示准入**目标；以下事项不解决不得用于真实业务单证审核或生产发布。

**鉴权与访问隔离**
- API 无鉴权，任何知道地址的调用方均可访问 `/verify`、`/ingest/*`；需增加身份认证与访问控制；
- 手机端允许明文 HTTP 传输；生产需 TLS；
- APK 使用 debug 签名侧载分发；正式发布需 release 签名与应用商店/MDM 渠道。

**审计与溯源**
- 需建立"原始文件 ↔ 人工修改记录 ↔ 使用的规则版本 ↔ 核验结果"全链路可追溯（当前 PDF 报告已带数据版本与规则版本戳，但人工编辑记录未持久化留痕）；
- 规则维护责任人、知识库来源与更新流程需制度化；
- 正式发布 APK 需有源代码版本号、构建记录、产物哈希的可验证对应关系（本项目已用 Git 管理，产物含 .sha1，但仍需制度化发布流程）。

**数据安全边界**
- AI 对话上下文把单证原始字段拼入 system 消息发送给外部大模型服务，存在"不可信数据"与"系统指令"角色混用风险；需改为 user/tool 角色隔离并限制注入内容；
- 需限制工具调用名称与输入输出 schema 范围（当前仅暴露 simulate_field_change 一个工具，仍需schema白名单化）；
- 需明确哪些企业数据会发送到外部大模型服务、哪些必须本地处理不出域，并提供私有化部署选项。

**评估与模型局限**
- 19组评估为模拟数据自证；权重未经真实业务数据校准；
- 标注口径与评分模型为两套实现，冻结集上必须一致（评估退出码校验），已知理论边界见 `evaluation_report.md`；
- 字符向量后端对跨语言描述相似度趋近0（已知局限），句向量后端未默认启用。

**真实OCR**
- tesseract OCR 对扫描质量、版式变化、手写内容的鲁棒性未经独立验收；生产前需用真实扫描样本验收识别率与人工修正率。
