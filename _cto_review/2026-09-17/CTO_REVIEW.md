# CTO 项目审查：中欧班列单证智能核验

审查日期：2026-09-17。对象：当前工作目录 `C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier`。

**结论：竞赛演示交付 NEED CHANGES；真实业务核验或生产发布 STOP。** 项目场景清楚，模块划分适合现阶段，已具备可以继续完善的演示基础。但多个核心反例仍输出“11 项 PASS、风险 0”，上传与人工纠正也存在闭环缺口。当前结果不足以作为发运放行或企业单证可靠性依据。

本次是源码审查和 Windows 本地运行验证。主审并行安排三个独立子审：核心引擎、摄取与 AI、移动端与部署，并再次复现关键结论。没有绑定源提交、运行独立 Linux/Docker、验收真实供应商 AI 或真机 OCR，因此不作生产或跨环境验收声明。

## 产品和架构判断

目标用户是单证员、货代和外贸人员；主要价值是提前发现单证矛盾并形成可解释整改材料。README 明确定位为竞赛 Demo，按这个目标评价，而不是要求它立即成为完整 ERP 或正式法规判断服务。

主要链路：PDF/图片 → 判型与 OCR/文字提取 → 结构化字段 → 11 项核验 → 加权风险评分 → 网页/移动展示与 PDF 导出。AI 分支提供解释、对话、模拟字段修改与邮件；知识库提供字符向量检索。

值得保留的设计：

- 核验引擎独立于界面，网页和手机可复用 API；适合先修正确性，再逐步扩展。
- 风险分可以追溯到检查项，模拟修改复用真实引擎而非另写计算逻辑。
- README 披露了字符相似度、预置推理、模板降级、知识库摘录与侧载 App 的局限，产品叙事基本诚实。
- 已有自动化测试、示例文件和容器入口，能够在现有结构上补充有效反例。

工程上的主要缺口是统一数据契约：摄取、编辑、API 和引擎之间只传递松散字典；字段缺失、错误类型、处理不完整和“一致性通过”尚未形成统一状态。风险评分目前是经验规则评分，不是经过真实业务数据验证的风险概率模型。

建议继续采用现有 Python 服务和 Flutter 客户端。当前投资优先放在数据契约、错误拒绝路径和真实样本验证；无需先拆微服务或更换全部 AI 技术。

## 本次验证结果

| 检查 | 本次结果 | 能支撑的结论 |
|---|---|---|
| 四个现有 Python 测试文件 | 61 项收集，60 passed、1 skipped | 现有用例通过；真实中文 OCR 用例因缺 Tesseract 跳过 |
| `selftest.py` | 26/26 通过 | 三个固定示例与现有评分行为符合脚本预期 |
| 19 个评估场景 | 内存重跑 19/19 一致 | 选定模拟模板的分级符合率；未覆盖写原评估文件 |
| 核心反例、ASGI 请求 | 已运行，见 evidence.json | 全绿漏报、非法输入 500 等生产代码路径已复现 |
| 24 个 Python 源文件语法解析 | 23 个通过；serve_apk.py 失败 | 分发入口包含确定的语法错误 |
| 移除 multipart 的导入模拟 | API 导入失败 | 上传依赖缺失会阻止 API 注册；不是实际 Docker 构建结果 |
| 网页函数 | 提取原函数并打桩 UI 执行 | 缺字段不可补、编辑后下载旧文本已复现；不是浏览器端到端结果 |
| AI 非配合响应 / OCR 丢页 | transport / OCR 故障打桩 | 应用不能拒绝这些错误响应；不是实际供应商误答率或真实 OCR 精度 |

没有运行真实 LLM、Playwright、Docker 构建、Flutter 单测/重建 APK 或真机拍照。本机现有 `.venv` 已额外安装 multipart，不能用其启动成功替代干净环境验证。

## 必须先修的问题

以下 P1 表示会破坏核验可信度或承诺的交付路径，应在完整演示交付前关闭；P2 表示应紧随其后修复。生产安全条件另列，避免把演示限制当作已验证的攻击。

### F01 / P1：非法数值基准和币种矛盾被判通过

位置：[verification_engine.py:312](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:312)、[金额核验:456](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:456)。

发票毛重改成 `0`、`-1` 或字符串 `NaN`，其余单证保持 12300kg，仍为 11 PASS、风险 0。发票金额改成 0 而报关金额仍为 86400，结果相同。报关币种改 EUR、发票保持 USD，数字相同时同样全绿，详情还把报关金额展示为 USD。

修复验收：先校验数字有限性、字段允许范围与币种，再计算容差；无法证明一致时给出明确未通过/待复核结果。计件数应按整数契约校验。增加零、负数、NaN/Inf、币种缺失与冲突的反例。

### F02 / P1：空单证和重复单证存在核验盲区

位置：[齐全性:141](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:141)、[缺字段过滤:128](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:128)、[金额只读首份:456](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:456)。

装箱单或原产地证的 `fields={}` 仍全 PASS、风险 0。追加第二份报关单，金额 1、运单号 WRONG、运抵国 UNKNOWN，仍全绿：若干检查只取第一份单证。

修复验收：分别记录文件收到、提取完整、必填字段完整和比对结论；明确每种单证的必填字段。对重复类型选择明确拒绝规则或完整配对规则，保证每份输入都被覆盖。

### F03 / P1：语义归一化抹掉关键规格差异

位置：[semantic.py:37](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/semantic.py:37)。

`钢板厚度1.5mm` 与 `钢板厚度15mm` 去标点后完全相同，相似度 1.0；引擎输出全 PASS、风险 0。独立子审还发现电池容量和否定词差异可达到匹配阈值。

修复验收：数字、单位、规格和否定信息独立核对，保留小数点等有意义符号。字符相似度只辅助描述比对，不能覆盖关键实体矛盾。无需先下载更大模型才能解决此问题。

### F04 / P1：提取错误和丢页没有可靠复核门槛

位置：[pdf_ingest.py:131](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/pdf_ingest.py:131)、[needs_review:111](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/pdf_ingest.py:111)、[OCR失败处理:280](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/pdf_ingest.py:280)、[网页预览:647](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:647)。

空的 `INVOICE NO:` 会取下一行 `SELLER:` 作为编号；空 SELLER 会取下一行 BUYER 标签，仍标 high。件数 `480.5` 被截成 480，毛重 `12,300 LB` 被当成 12300kg，仍 high。

unknown 和原产地证类型没有字段解析规则，返回空字段、`needs_review=False`；unknown 还被转换成 invoice。第一页字段完整、第二页 OCR 强制失败时，结果仍 `error=None`、全部字段 high、无需复核；网页预览不显示其 warnings。

修复验收：限制同行匹配，校验完整数值与单位；未知类型要求人工指定，已识别类型标出缺项；任一页处理失败都必须显式提示并进入复核流程。应将“规则命中”与经验证的识别置信度区别展示。

### F05 / P1：人工纠正不能形成可靠闭环

位置：[app.py:479](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:479)、[main.dart:478](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/mobile_app/lib/main.dart:478)、[路线保存:515](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/mobile_app/lib/main.dart:515)。

网页遇到未提取字段直接跳过控件，无法按提示补齐。手机 unknown 的 confidence 为空，修改单证类型后仍没有目标类型字段可填。手机修改 `route_countries` 将国家列表变成字符串；同一路线从 PASS/PASS 变成 WARNING/FAIL，主审整批复现风险从 0 变成 35。

修复验收：编辑表单来自类型字段定义，允许补齐缺字段；按字段类型序列化，国家路线保持字符串列表。API 同时校验契约，防止坏类型进入引擎。增加“识别失败→补齐→重算”和手机编辑路线的跨端用例。

### F06 / P1：部署和扫码分发入口存在确定缺陷

位置：[requirements.txt](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/requirements.txt)、[Dockerfile:25](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/Dockerfile:25)、[serve_apk.py:20](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/serve_apk.py:20)、[start.sh:3](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/start.sh:3)。

requirements 缺少上传路由注册需要的 python-multipart，裸 FastAPI 的依赖不包含它；屏蔽该包后 `import api` 立即报 RuntimeError。没有实际构建容器，此处是依赖证据和缺包模拟。serve_apk 多一个引号，Python 语法解析失败；还需明确其 qrcode 依赖。API 在容器后台启动，退出后网页仍可继续运行并降级直连，网页可用不能证明手机 API 可用。

修复验收：补齐运行依赖、修复分发脚本、管理 API 进程生命周期。用干净镜像分别验证 health、verify、图片/PDF 摄取、网页及 APK 下载；OCR 环境缺失应明确失败，不计作功能通过。

### F07 / P1：未知运单类型和统一运单路线默认放过

位置：[verification_engine.py:508](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:508)。

AIR WAYBILL 被判类型与路线匹配；CIM/SMGS 配合 `中国→美国→德国` 也全绿，美国不在项目自身覆盖集合里。这里只证明内部规则遗漏，没有验收真实铁路法规正确性。

修复验收：类型限定为受支持枚举，未知类型复核；统一运单也核对覆盖集合并集。对每个路线判断保留规则版本和范围说明。

### F08 / P2：API 只验证外层，常见错误变成 500

位置：[api.py:41](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/api.py:41)、[非法毛重基准:329](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/verification_engine.py:329)。

实际 ASGI 请求 `documents=[null]` 或 `fields="bad"` 返回 500。发票毛重 `12300kg` 触发 TypeError；现有测试只扰动装箱单，未覆盖坏基准。

修复验收：定义嵌套请求/响应 schema，对输入结构错误返回可理解的 4xx，对识别不确定给复核结果；不能让普通坏数据使整批崩溃。上传端点还需要明确文件大小、页数和处理耗时限制，异步路由中的阻塞 OCR 应移到受限工作执行器。

### F09 / P2：邮件编辑、缓存与对话上下文缺少数据版本

位置：[邮件缓存:296](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:296)、[下载:314](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:314)、[上传缓存:543](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:543)、[对话缓存:336](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/app.py:336)。

用户编辑 text_area 后下载仍使用原始 result 文本；原函数打桩执行已复现。上传缓存仅看文件名和大小，同名同大小不同内容会命中旧解析。邮件/聊天只按 batch_id 缓存，PDF 批次又恒为 pdf_upload；源码缺少换文件或修改字段后的失效机制，存在旧草稿和历史混入新数据的风险，未做浏览器复现。

修复验收：上传用内容哈希；分析、邮件和聊天关联数据版本；下载读取当前编辑值。修改字段或切换上传数据应更新对应材料。

### F10 / P2：AI 推演和评估的证据范围超过已实现约束

位置：[chat_assistant.py:175](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/chat_assistant.py:175)、[evaluation.py:157](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/evaluation.py:157)、[risk_model.py:8](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/risk_model.py:8)。

假设把报关件数改为 480，给 transport 一个“不调用工具、风险0”的响应，函数仍接受为 live；真实重算是 95。已有脚本化假 LLM 测试只证明配合响应能驱动工具，没有证明必然重算或答案数值与工具一致。这里未调用真实供应商，也不推断真实误答频率。

19 个场景与规则/权重校准同源，评估 main 每次生成并覆盖样本，并非读取冻结留出集。标注规则称“0 FAIL 为低风险”，但同时出现语义存疑与路线 WARNING 时是 0 FAIL、25分、中风险；再缺产地证是 1 FAIL、55分、高风险，与标注口径矛盾。19/19 可以保留为模拟回归结果，不能表达真实单证或 OCR 的准确率。

修复验收：数值推演由程序强制重算或独立呈现计算结果；拒绝未经验证的计算结论。统一风险标签定义，拆分校准集与冻结留出集，报告字段识别率、漏报/误报率、人工修正率和处理失败率。以业务人员标注的真实脱敏单证验证，覆盖不同模板、语言、格式、扫描质量与组合异常。

## 生产使用的额外门槛

当前 API 没有鉴权和访问隔离，手机允许明文 HTTP，release 使用 debug 签名；这些可以作为受控演示限制，但不满足真实企业单证交付条件。先明确单用户本地、私有部署或多客户服务的部署范围，再落实相应鉴权、TLS、访问隔离、上传资源控制与日志脱敏。

还需有原始文件/人工修改/规则版本/核验结果的审计关系、规则负责人及知识库来源和更新流程、正式 APK 签名与源版本/构建记录/产物哈希。当前目录没有 Git 元数据，现有 APK 不能证明与审查源码一致。

AI 上下文把单证原始字段拼进 system 消息，存在不可信数据与指令角色混用；此为源码/打桩边界证据，不代表已成功攻击。应限制工具名、输入与输出 schema，隔离单证数据，保留供应商失败与降级原因，明确企业数据发送到外部模型的范围。

## 下一步交接与准入

| 顺序 | 责任角色 | 交付与验收 |
|---|---|---|
| 1 | 后端/核验负责人 | 修 F01–F04、F07、F08；反例不能再输出无依据的 PASS，错误输入可解释 |
| 2 | Web/Flutter 负责人 | 修 F05、F09；缺字段能补，修改不破坏类型，导出材料对应当前数据 |
| 3 | 交付负责人 | 修 F06；干净镜像及扫码下载验收，API/OCR 的失败可见 |
| 4 | AI/评估与业务负责人 | 修 F10；计算结果约束、冻结真实样本和统一标注口径 |
| 5 | CTO/复核人 | 查看上述证据后判定竞赛完整演示 PASS；真实业务另走生产门槛验收 |

竞赛完整演示准入：关闭 F01–F07，补关键反例与跨端闭环验证，明确展示边界；真实 OCR 若未验收，必须限定演示输入。真实业务准入：进一步关闭 F08–F10，并满足部署安全、业务规则确认、真实样本验证、审计与产物溯源门槛。

当前建议暂停增加功能，先完成“提取失败可见→人工补齐→类型正确→重算可信→导出当前结果”这条链路。CTO 复核所需材料是固定源版本、修改清单、反例前后结果、测试记录和干净环境运行证据。

## 审计记录与复跑

- branch / commit：不可取得；当前目录不是 Git 仓库。
- 产品修改：无。新增文件仅在本审查目录；未覆盖原 evaluation_set 或 evaluation_report.md。
- 风险：已复现多个全绿漏报，交付入口有确定缺陷；不能从现有测试绿灯推断实际核验可靠。
- 源文件快照：[source_sha256.json](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/source_sha256.json)。
- 证据：[evidence.json](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/evidence.json)、[反例脚本](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/probes.py)、[运行输出](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/probes.log)。
- 现有验证：[pytest.log](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/pytest.log)、[selftest.log](C:/Users/Jeff0/.zcode/workspace/default/ceb_doc_verifier/_cto_review/2026-09-17/selftest.log)。

在项目根目录用 PowerShell 顺序复跑；应关闭真实 AI 凭据和可选语义模型下载路径，仅评估离线路径：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
foreach ($reviewKey in @('ARK_API_KEY','GLM_API_KEY','BIGMODEL_API_KEY','OPENAI_API_KEY','SEMANTIC_BACKEND')) {
    Remove-Item -LiteralPath ('Env:\' + $reviewKey) -ErrorAction SilentlyContinue
}
.\.venv\Scripts\python.exe -m pytest -v -p no:cacheprovider test_verification_engine.py test_risk_model.py test_pdf_ingest.py test_native_ai.py
.\.venv\Scripts\python.exe selftest.py
.\.venv\Scripts\python.exe _cto_review\2026-09-17\probes.py
```

反例脚本运行的是实际产品函数，网络 LLM 和故障 OCR 部分明确打桩。重跑会更新本审查目录的 evidence 与源码哈希，因此当前证据对应当前文件快照，而非不可变源提交。
