# DDL Agent 架构设计

> 状态：阶段五（手机优先 + 定期监控）。对应需求文档 `main(1).md`：按自然语言查询，从多个来源采集 DDL，
> 规划/选工具、字段抽取与时间归一化、跨来源去重、按可信度与发布时间解决冲突，写入共享结构化文档，
> 并配套合成数据集、单次直出 baseline 与评测。
>
> 本文同时记录设计与当前实现边界；真机与真实凭据相关路径均标注“未经真实数据验证”。

## 0. 操作模型（先读这一段）

这里最容易产生误解，所以先讲清楚：

- **操作对象是手机**。微信（群聊/私聊）、QQ（群聊）、Outlook App 收件箱的界面，全部在**手机上**被读取和点击。
  agent 不会在电脑上打开微信/QQ/Outlook 桌面客户端，也不依赖电脑本地邮件文件
  （本地 `.eml` / 聊天归档只是离线测试替身）。
- **运行环境是电脑**。本项目在电脑上运行，通过 **ADB（USB 线或 WiFi 调试）** 驱动手机。
  手机端**不安装任何本项目组件**，也**不需要 root**；首次使用时 uiautomator2 可能自行安装一个自动化辅助组件，
  那是 uiautomator2 的既有机制，不是本项目的部署。
- 一句话：**电脑上的 agent 通过 ADB 操作手机 GUI**，把手机里的群聊和邮件 DDL 汇总成日历。

Windows 用户双击仓库根目录的 `run_ddl_monitor.bat` 即可：脚本先检查 adb、设备在线与授权、设备锁，
再让用户选择「单轮扫描 / 持续监控」，跑完刷新并打开 `output/calendar.html`。
没插手机、未授权 USB 调试、找不到 adb、手机被其他窗口占用时，会给出对应的人话提示与下一步动作。

## 1. 目标与边界

### 1.1 目标

- 用户用自然语言描述要跟踪的数据源（`source_config`），再用自然语言提问（`query`）。
- Agent 自主完成：解析查询 → 选择数据源/工具 → 检索 → 抽取 → 归一化 → 去重 → 冲突核查（最多两轮追加检索）
  → 写入共享文档（插行/改行 + 审计日志）。
- 全流程可在**本地模拟数据**上离线跑通并评测，不依赖真机、不依赖真实 Microsoft Graph 权限。

### 1.2 明确的边界

| 事项 | 本阶段处理方式 |
|---|---|
| 三个真实数据源 | 微信（群聊/私聊）、QQ（群聊）、Outlook App 收件箱，**全部通过手机 GUI 屏幕内容读取**。真机路径**未经真实数据验证**。 |
| Outlook 接入方式 | **只走手机 Outlook App GUI 读屏**，与微信、QQ 共用同一套手机层。Microsoft Graph 不在当前实现路径上，代码骨架仅作为“将来可选实现”保留，当前不启用。 |
| 本地 `.eml` / 聊天归档 | 降级为**离线测试替身**，保留用于无手机、无账号时跑通全链路与测试。 |
| LLM 依赖 | 规划/抽取/裁决走 DeepSeek；断网或无 key 自动降级规则实现。 |
| 现有手机层 | **不改动任何公开行为**，只作为读取工具被调用。 |
| main 分支 / 现有 PR | 不触碰、不 force push、不合并。 |

## 2. 总体架构

```
                      ┌──────────────────────────────┐
  自然语言 source_config│  SourceConfigParser           │
  自然语言 query        │  QueryPlanner                 │
                      └───────────────┬──────────────┘
                                      │ Plan(sources, filters, time_range, keywords)
                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │  RetrievalOrchestrator                               │
        │  逐个 MessageSource.fetch(query) → RawMessage[]       │
        └───────┬───────────────────┬──────────────────┬───────┘
                │                   │                  │
      LocalEmlConnector     LocalChatConnector   MobileChatConnector
      (默认，离线)           (默认，离线)          (复用手机层，未验证)
                │                   │                  │
                └───────────────────┴──────────────────┘
                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │  ExtractionPipeline                                  │
        │  RuleExtractor (默认) / LLMExtractor (可注入)         │
        │  → DeadlineCandidate[]（含 evidence、源、发布时间）    │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  TimeNormalizer → ISO-8601；不可解析标 unresolved      │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  Deduplicator（关键词 + 语义相似度，任务名归一化）      │
        │  → TaskCluster[]（同一任务的证据集合）                 │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  ConflictResolver（可信度优先 → 发布时间新者覆盖）     │
        │  最多两轮追加检索（回调 RetrievalOrchestrator）        │
        │  仍不一致 → status=uncertain（交人工）                 │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  DeadlineWriter（可替换）                             │
        │  XlsxWriter(默认) / CsvWriter / MarkdownWriter        │
        │  插行/改行 + 审计日志 + 失败重试一次 + 保留上一版      │
        └──────────────────────────────────────────────────────┘
```

`ddl_agent.pipeline.DdlAgent.run(query, source_config)` 是唯一对外入口，评测与 baseline 都通过它或
其内部组件调用。

## 3. 一次查询的数据流

1. **解析配置**：`SourceConfigParser.parse(source_config)` → `[SourceSpec(kind=email|chat, provider, locator, credibility)]`。
2. **规划**：`QueryPlanner.plan(query, specs)` 产出 `RetrievalPlan`：选中的数据源、课程/项目过滤词、时间窗、检索关键词。
   规则版按关键词命中与时间词解析；LLM 版可替换（接口 `Planner`），离线默认规则。
3. **检索**：`RetrievalOrchestrator.fetch(plan)` 并行调用各 `MessageSource.fetch()`，统一转为 `RawMessage`。
   单个源失败只记录告警，不阻断其他源。消息按课程做软过滤（完全不提课程的保留）。
   用户说「下周/本月」时，时间窗作用于**最终截止时间**，在冲突解决后筛记录；不按发布时间过滤，避免丢掉更早的原始公告。
4. **抽取**：`RuleExtractor.extract(messages)` 输出 `DeadlineCandidate`（任务名、课程、截止时间原文、状态、证据）。
5. **归一化**：`TimeNormalizer` 把绝对/相对时间转成 ISO-8601（带 `now` 注入，保证可复现）；无法解析置 `unresolved`。
6. **去重**：`Deduplicator.cluster(candidates)` 按任务名归一化 + 相似度聚类，跨来源合并同一任务，保留全部 evidence。
7. **冲突解决**：`ConflictResolver.resolve(clusters, retrieval)` 比较候选时间；一致直接定稿；
   不一致按「源可信度 → 发布时间」决策；若最高可信度组内仍冲突，发起追加检索（最多两轮，`supplementary_query`），
   仍不一致或无法判定 → `status=uncertain`。
8. **写入**：`DeadlineWriter.upsert(records)` 按 `(course, normalized_task_name)` 匹配已有行：命中改行，否则插行；
   每次变更写审计条；写失败重试一次，失败则回滚到上一版并返回失败原因。
9. **回报**：`AgentRunResult` 含 `records`、`uncertain`、`warnings`、`trace`（各阶段耗时、检索轮数、工具调用次数）。

## 4. 模块清单（输入 / 输出 / 失败处理）

| 模块 | 输入 | 输出 | 失败处理 |
|---|---|---|---|
| `SourceConfigParser` | 自然语言配置文本 | `[SourceSpec]` | 无法识别来源时回落到默认配置并告警；不猜测 provider |
| `QueryPlanner` | query + `[SourceSpec]` | `RetrievalPlan` | 无关键词时退化为“全部已配置源 + 全时间窗” |
| `MessageSource.fetch` | `RetrievalPlan` | `[RawMessage]` | 单源异常捕获为 `SourceError`，继续其余源；超时同理 |
| `RuleExtractor` | `[RawMessage]` | `[DeadlineCandidate]` | 无匹配返回空；缺字段置 `None` 并标 `missing_fields` |
| `TimeNormalizer` | 时间原文 + `now` + 时区 | ISO-8601 字符串 / `None` | 无法解析 → `unresolved=True`，不猜日期 |
| `Deduplicator` | `[DeadlineCandidate]` | `[TaskCluster]` | 相似度并列时按来源优先级拆分，避免误合并 |
| `ConflictResolver` | `[TaskCluster]` + 检索回调 | `[ResolvedRecord]` | 超两轮仍冲突 → `uncertain`，保留全部候选供人工审阅 |
| `DeadlineWriter` | `[ResolvedRecord]` | `WriteResult`（新增/更新/跳过条数） | 首次写失败重试一次；仍失败回滚上一版并报错 |
| `AuditLog` | 变更事件 | 审计行（时间、动作、旧值、新值、证据 ID） | 审计写失败视为整体写失败，触发回滚 |

## 5. 与现有手机自动化层的关系

现有仓库是“手机 GUI 自动化 + 截图/OCR/控件树 + 设备锁 + 留证”的实现，本项目把它降级为**一个数据源连接器**：

| 现有模块 | 复用方式 | 边界 |
|---|---|---|
| `screen_agent.ScreenAgent` | `MobileChatConnector` 以自然语言任务驱动其执行“打开微信 → 进入指定群/会话 → 向上翻页读取消息” | 复用公开接口；滚动观察新增逐条消息提示与 OCR 叠加 |
| `ocr_locator.OCRLocator` / `detect_tree` / `snap_action_coords` | 由 ScreenAgent 内部使用；滚动观察时 `detect_ocr` 的文本会并入 observation | 不改 OCR 接口 |
| `phone_controller.PhoneController` | 由 ScreenAgent 内部使用（截图、tap、type_text） | 不改接口 |
| `device_lock.DeviceLock` | 连接器在整段读取周期外层加锁，防止与其他实验并发操作手机 | 只读复用 |
| `runs/` 留证目录 | 连接器把每步截图与 `step_NNN.json` 作为 `RawMessage.evidence_path` | 只读复用 |

真机只读验证（2026-11-18，微信「文件传输助手」）：ScreenAgent 能进入目标会话并滚动留证；
滚动观察叠加 OCR 后，10 步产生 10 条非零消息文本（修复前为 0 条）。到达步数上限时
`MobileChatConnector` 返回已采集观察，并带 `partial=true` / `last_status=timeout`，不再丢弃证据。

其余部分（规划、抽取、去重、冲突、写入、评测、数据集）**全部新建**于 `ddl_agent/` 包内。

## 6. 两个必须可替换的接口

### 6.1 数据源连接器 `MessageSource`

```python
class MessageSource(Protocol):
    name: str                 # 例如 "outlook" / "wechat:群名"
    source_type: SourceType   # OFFICIAL_EMAIL / COURSE_ANNOUNCEMENT / INSTRUCTOR_CHAT / STUDENT_CHAT
    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]: ...
```

实现：

| 实现 | 状态 | 说明 |
|---|---|---|
| `MobileAppSource`（微信 / QQ / Outlook） | **主路径；仅微信自聊会话做过一次真机只读验证** | 通过 ScreenAgent 在 App 内搜索/滚动定位目标会话，读取屏幕消息。微信已验证一次；QQ 与 Outlook 目前只有包名映射和代码路径，**未做真机读屏验证**；三者都未覆盖真实课程群/第三方会话 |
| `LocalEmlConnector` | 离线测试替身 | 读取 `*.eml` 目录；无手机时跑通全链路 |
| `LocalChatConnector` | 离线测试替身 | 读取聊天归档 JSON；无手机时跑通全链路 |
| `GraphMailConnector` | 不在主路径 | 将来可选实现的代码骨架；当前不启用、不测试、不作为交付能力 |

同一 `fetch(plan)` 契约，评测可任意替换而不改上层。

### 6.2 输出文档写入器 `DeadlineWriter`

```python
class DeadlineWriter(Protocol):
    def upsert(self, records: list[ResolvedRecord]) -> WriteResult: ...
    def read(self) -> list[ResolvedRecord]: ...
```

实现：`XlsxWriter`（默认，openpyxl）、`CsvWriter`、`MarkdownWriter`。三者共享 `TableSchema`（列定义）与
`AuditLog`，保证评测中替换写入器不改变记录语义。

## 7. 数据模型

```python
SourceType = Literal["official_email", "course_announcement", "instructor_chat", "student_chat"]

@dataclass
class RawMessage:
    message_id: str
    source_name: str
    source_type: SourceType
    sender: str
    published_at: datetime
    subject: str
    body: str
    evidence_path: str | None      # 真机路径下的截图/step json；离线为 None
    thread_id: str | None

@dataclass
class DeadlineCandidate:
    candidate_id: str
    task_name_raw: str
    course: str | None
    deadline_raw: str | None
    deadline_iso: str | None       # ISO-8601
    missing_fields: list[str]
    unresolved_time: bool
    source_type: SourceType
    source_name: str
    published_at: datetime
    message_id: str

@dataclass
class TaskCluster:
    cluster_id: str
    task_name: str                 # 规范化后的名称
    course: str | None
    candidates: list[DeadlineCandidate]

@dataclass
class ResolvedRecord:
    task_name: str
    course: str | None
    deadline_iso: str | None
    status: Literal["confirmed", "uncertain", "missing_deadline"]
    source: str                    # 获胜证据的来源描述
    sources: list[str]             # 所有原始来源（去重后）
    evidence_ids: list[str]
    confidence: float
    resolution_note: str           # 冲突解决说明（为什么选它）
```

文档列：`Task Name | Course | Deadline (ISO-8601) | Status | Source | Confidence | Evidence | Last Updated`。
审计表列：`Timestamp | Action(insert/update/rollback) | Task Name | Old Value | New Value | Reason`。

## 8. 关键算法

### 8.1 时间归一化

- 支持：`YYYY-MM-DD`、`MM/DD`、`Nov 20`、`11月20日`、`2026-11-20 23:59`；
  相对：`今天/明天/后天/本周五/下周五/3天后/两天后`、`today/tomorrow/next Friday/in 3 days/EOD`。
- 所有相对时间基于注入的 `now` + `Asia/Shanghai`，保证离线结果可复现。
- 只有日期没有时间 → 默认 `23:59`，并在 `resolution_note` 标注“默认当日截止”。
- 解析失败 → `deadline_iso=None`、`unresolved_time=True`，最终状态 `uncertain`，绝不猜测。

### 8.2 去重

1. 任务名归一化：小写、去标点、`HW1/Homework 1/Assignment 1/作业一` → 统一 `assignment 1`；去课程前缀。
2. 相似度：`score = 0.6 * token_jaccard + 0.4 * difflib_ratio`，对归一化名称计算。
3. 阈值 `>= 0.78`（可配置）且课程相同 → 同簇；否则分簇。并列临界值取证据更多者，避免误合并。
4. 语义匹配是接口：默认词法+字符级相似度（离线、确定性）；可注入 embedding 实现。

### 8.3 冲突解决

可信度排序（高 → 低）：`official_email(4) > course_announcement(3) > instructor_chat(2) > student_chat(1)`。

注入 LLM 裁决器时（默认 Agent-LLM 配置）：冲突先交给模型，在已有候选时间中选一个并给出理由与置信度；
置信度 < 0.6、拒答或编造候选外时间 → 不采纳，继续追加检索，最终标 `uncertain`。
未注入裁决器（规则版/无 key）时按下面的确定性规则：

```
candidates → 取 deadline_iso 去重
  一致 → confirmed
  不一致:
    最高可信度组内一致 → confirmed（note: 高可信源覆盖低可信源）
    最高可信度组内不一致 → 比较 published_at，最新者胜（note: 同源新公告取代旧公告）
    仍并列 / 无法判定 → 追加检索（第 1、2 轮，向同一任务相关源追加关键词）
        结果一致 → confirmed
        两轮后仍不一致 → uncertain（保留所有候选，交人工）
```

`confidence`：一致=0.95；可信度压制=0.85；时间新者覆盖=0.75；人工复核=0.4；LLM 裁决取模型自报置信度。

### 8.4 文档写入

- 匹配键：`(course or "", normalize(task_name))`；命中则改行，否则追加。
- 事务式写入：写前把当前文件复制为 `*.bak`；写入成功再删除备份。
- 写失败 → 重试一次；再失败 → 用 `.bak` 恢复，`WriteResult.ok=False` 并带异常信息。
- 每次插入/更新/回滚都追加审计行；审计写失败视为整体失败。

## 9. 目录结构与落盘

```
ddl_agent/
  __init__.py
  models.py
  settings.py                 # 阈值、可信度、时区、数据集路径
  planner.py
  pipeline.py
  connectors/{base,local_eml,local_chat,graph_mail,mobile_chat}.py
  semantic.py                 # 词法 + 本地 embedding 相似度（离线可降级）
  extract/{rules,normalizer,llm_extractor}.py
  llm/{client,planner,arbiter}.py
  factory.py                  # LLM_ENABLED 开关与规则降级
  dedupe.py
  conflict.py
  writer/{base,xlsx_writer,csv_writer,markdown_writer,audit}.py
  output.py                   # data.json / .ics / 手机日历可选接口
  monitor/{config,cursor,sources,runner,cli}.py
  dataset/generator.py        # 三档难度 × 20–30 条，train/val/test
  evaluation/{metrics,baseline,run_eval}.py
tests/test_ddl_*.py
experiments/ddl_agent/dataset/   # 合成数据集与 ground truth（已提交，便于复现）
experiments/ddl_agent/results/   # 评测输出（.gitignore 已忽略）
```

## 10. 合成数据集与评测协议

### 10.1 数据集

- 三档难度，每档 20–30 个样本（`easy`：直接抽取；`medium`：重复条目 + 字段缺失；
  `hard`：冲突时间 + 旧 DDL 被新值取代 + 相对时间/时区/歧义 + 多任务干扰）。
- 事实驱动：场景先定义任务、课程、真实截止时间与冲突胜者，ground truth 直接由事实导出，
  文本只负责渲染，不参与 ground truth 推导。
- 噪声：引用链（`> `）、`---------- Forwarded message ---------`、签名档、免责声明、无关闲聊、
  已过期旧 DDL、他人课程任务、考试/上课时间、`27 Nov 2026` 与中文日期等规则不覆盖的写法、
  `the Friday after next` / `月底前` / `考完试那周` / `PST` 时区等。
- 每个样本：`sample_id`、`tier`、`query`、`source_config`、`messages[]`（email/chat 混合）、`ground_truth[]`。
- 干扰来源：每个样本额外挂 4 个与查询无关的来源（实习内推群、家人群、社团活动群、其他课程群），
  内含“网申截止 / 例会报名截止 / 其他课程作业截止”等像 DDL 的诱饵。它们**不进入 ground truth**，
  ground truth 仍只由事实导出；用途是检验动态选源是否排除无关来源，以及固定源时是否多花抽取成本。
- `ground_truth` 字段：`task_name`、`course`、`deadline_iso`、`status`、`expected_sources[]`、`conflict_expected`（bool）、`winner_reason`。
- 划分：按 tier 分层抽样 `train/val/test = 60%/20%/20%`，固定随机种子，生成时写 `split` 字段，避免 prompt 调参泄漏。
- 生成器确定性（seed 固定），可重建；不手写虚构“实测指标”。

### 10.2 指标

| 指标 | 定义 |
|---|---|
| Exact match | 任务名（归一化）与截止时间同时对上的比例 |
| Fuzzy match | 任务名对上且时间差 ≤ 1 天 |
| Recall | 命中 ground truth 条数 / ground truth 总条数 |
| Conflict accuracy | 在 `conflict_expected=true` 的样本中，最终时间等于期望胜者的比例 |
| Duplicate rate | 输出行中同一 ground-truth 任务多出的行数 / 输出总行数 |
| Doc validity | 列齐全、必填非空、ISO 格式合法、审计表存在且条数匹配 |
| Efficiency | 墙钟时间、LLM 调用次数、token 估算、按单价折算成本 |

### 10.3 Baseline

`LLMSinglePassBaseline`：把同一批 `RawMessage` 全部拼进**一条** prompt，一次调用直接产出记录，
不做规划、不做去重、不做冲突核查、不写审计。`SinglePassBaseline` 是同协议的规则兜底，
在无 key 时使用。token/成本按同一计价表统计。

### 10.4 语义相似度：词法 / embedding / 组合

- 接口：`SemanticMatcher(strategy, threshold)` 返回 0~1 相似度；去重与指标匹配共用同一实现。
- 三种策略：
  - `lexical`：任务名归一化后，token Jaccard 0.6 + 字符序列相似度 0.4；阈值 0.78。
  - `embedding`：本地 `sentence-transformers`（默认 `paraphrase-multilingual-MiniLM-L12-v2`）余弦；阈值 0.6。
  - `combined`：取 `max(lexical, embedding)`，任一通道过阈值即可合并；不是加权平均。
- 降级：`sentence_transformers` 未安装、模型未下载、加载异常或离线不可用时，`similarity()` 返回词法分数，
  报告的 `similarity.available=false` 并写入 `fallback_reason`；评测与离线测试继续跑完，不中断。
- 依赖与缓存：`sentence-transformers` 放在 `requirements-embedding.txt`（可选），不进入主依赖与 CI；
  模型与框架缓存默认 `~/.cache/ddl-agent/sentence-transformers`（可用 `DDL_EMBEDDING_CACHE` 覆盖），代码在加载前设置 `HF_HOME`、
  `SENTENCE_TRANSFORMERS_HOME`、`TORCH_HOME`，不写死某台机器盘符。
- 离线环境：把缓存目录整体拷贝到目标机器，或设置 `DDL_EMBEDDING_CACHE` 指向已有目录，并保持模型名一致；
  可用 `--embedding-model` / `--embedding-cache` / `--dedupe-threshold` 在评测命令行覆盖。
- 真实模型用例默认跳过，设置 `RUN_EMBEDDING_TESTS=1` 才执行。

## 11. 验证状态标注

| 能力 | 状态 |
|---|---|
| 本地 `.eml` / 聊天归档连接器、抽取、归一化、去重、冲突、写入、评测 | 已实现，离线测试真跑 |
| DeepSeek 规划 / 抽取 / 冲突裁决 | 已真实调用（合成数据），用量与成本写入报告 |
| Graph 邮件连接器 | 不在主路径；仅保留“将来可选”骨架，未验证 |
| 语义匹配（本地 embedding） | 已实现并离线跑通；模型缺失时退回词法 |
| 微信/QQ 真机读取 | 微信单机自聊会话已做只读验证（`docs/real-device-test-report.md`）；QQ 与真实群聊未验证 |
| 真实邮件/聊天上的整体效果 | **未验证** |

### 11.1 阶段四三方实测（2026-11-18，合成数据）

命令：`python -m ddl_agent.evaluation.run_eval --split test`
（test = 15 个样本 / 16 条 ground truth，未参与任何调参）。
三个系统使用完全相同的原始消息。

| 指标 | Agent-LLM | Agent-规则 | baseline 单次直出 |
| --- | --- | --- | --- |
| 精确匹配 | **0.8125** | 0.5625 | 0.5000 |
| ±1 天模糊匹配 | **0.8125** | 0.6250 | 0.5000 |
| 召回 | **1.0000** | 0.8125 | 0.5625 |
| 重复率 | **0.0000** | 0.0000 | 0.1923 |
| 冲突解决正确率 | **1.0000**（3 条冲突） | 0.3333（3 条） | 0.6667（3 条） |
| 文档结构校验通过率 | **1.0000** | 1.0000 | 不适用 |
| 输出行数 | 16 | 16 | 26 |
| LLM 调用次数 | 38 | 0 | 15 |
| token 估算 | 15956 | 831（字符估算） | 5346 |
| 成本估算（USD） | 0.007385 | 0 | 0.002623 |
| 平均耗时（秒/样本） | 2.67 | 0.015 | 0.86 |

全量 72 样本（train+val+test，仅作稳定性参考，未据此调参）：
Agent-LLM 精确 0.7733 / 召回 1.0000 / 重复率 0 / 冲突正确率 1.0000；
Agent-规则 0.5200 / 0.8533 / 0 / 0.3636；
baseline 0.5200 / 0.6533 / 0.2051 / 0.7273。
Agent-LLM 共 179 次调用、75108 token、约 $0.0349。

说明：
- 该表是**加入干扰来源之前**的三方对比；加入干扰来源后，Agent-LLM 的精确/召回/冲突正确率不变，
  只是来源配置变长、token 略增（full 从 15968 到 16647）。当前 RQ4 与相似度对比数字见
  `docs/proposal-compliance.md` 第 8 节。
- 精确/模糊匹配达不到 1.0，是因为部分 ground truth 本身没有具体时间
  （missing / uncertain），不计入时间命中，但计入召回。
- 以上数字全部来自**合成数据**；真实邮件与真实微信/QQ 记录尚未验证，
  结论只在该生成器的场景分布上成立。
- 成本按 DeepSeek 公开单价近似换算，不是账单金额。

## 12. LLM 入链与降级

- 客户端：`ddl_agent/llm/client.py` 只读 `config.PROVIDERS["deepseek"]`（key 来自 `.env`），
  失败重试一次，累计每次调用的 token、耗时、成本。
- 规划：`LLMQueryPlanner` 输出要查询的源、课程过滤词、时间窗、是否允许追加检索；
  非法输出时回退 `QueryPlanner`。
- 抽取：`LLMDeadlineExtractor` 输出任务名/课程/时间短语/ISO/置信度/是否复核；
  ISO 非法时用本地归一化兜底；JSON 非法或断网时整条消息回退 `RuleExtractor`。
- 裁决：`LLMConflictArbiter` 只能在已有候选中选一个；低置信度/拒答/编造时间 → uncertain。
- 开关：`LLM_ENABLED`（默认开，设为 `0` 强制离线），无 key 时 `factory.build_agent` 自动返回规则版；
  所有单元测试与 `--no-llm` 评测均不触网。

## 13. 里程碑

1. 设计评审（本文）。
2. 包骨架 + 数据模型 + 归一化 + 抽取 + 去重 + 冲突 + 写入器，配单测。
3. 数据集生成器 + 划分 + ground truth。
4. Baseline + 评测脚本，产出离线评测报告。
5. LLM 规划/抽取/裁决入链、数据集去模板化、三方对比（已完成，见 §11.1）。
6. （后续）真机聊天读取、真实数据上的复测与消融实验；Graph 接入仅作为**非主路径的可选实现**，不排入当前里程碑。

## 14. 手机优先架构（阶段五，当前方向）

### 14.1 数据源与定位

- 主路径只有三类手机来源：`wechat`（群聊/私聊）、`qq`（群聊）、`outlook`（App 收件箱）。
  统一实现 `MobileAppSource`，复用现有 `ScreenAgent`：截图/OCR/控件树/tap/设备锁/留证全部由手机层完成。
- 按目标名称定位，不写死坐标：任务里明确要求「在 App 内搜索目标名称 → 打开会话 → 向上翻阅 → 逐条读取」，
  agent 自行决定搜索、置顶、列表滚动等操作。
- 找不到目标会话时抛 `TargetNotFound`，运行结果里记录 `source_errors`，**不静默跳过**、不拿别的会话顶替。
- 离线测试替身：`LocalEmlConnector` / `LocalChatConnector` 保留，仅在无手机时使用；
  Graph 不在主路径，仅保留未启用的代码骨架作为“将来可选实现”。

### 14.2 自然语言监控配置

- 新增 `monitor/config.py`，配置持久化为 `sources.yaml`，每条来源保存：
  `id / app / kind / target / source_type / enabled / lookback_days / nl_text / parse_note`。
- `nl_text` 保存原始自然语言，`parse_note` 记录解析出的关键字段，满足“这条配置从哪句话来”的可追溯要求。
- 支持 `add / list / edit / remove / enable / disable`；`add` 解析失败时显式拒绝并给出原因
  （例如未指明 App、未给出目标名称、同时出现多个互相冲突的目标），**不猜测**。
- 调度周期也由自然语言解析（如“每 6 小时”“每天一次”），解析失败直接报错，不套默认值。

### 14.3 增量、去重与调度

- 状态目录（默认 `state/`）：
  - `cursors.json`：每个来源的 `last_timestamp`、`last_message_id`、`baseline_done`；
  - `messages.jsonl`：已抓到的原始消息（跨轮保留，供冲突解决复用）；
  - `candidates.jsonl`：已抽取的候选（LLM 只处理新消息，旧候选直接复用）。
- 每轮只 **抓取** 游标之后的新消息；首次运行做一次基线扫描（按 `lookback_days`）。
  游标在来源抓取成功后推进；失败不推进，下一轮重试。游标可手动重置（`monitor reset-cursor`）。
- 跨轮去重由写入器统一处理：同一 `(课程, 归一化任务名)` 命中则改行；内容和状态都没变则跳过。
  每轮审计记录 `inserted / updated / skipped`。
- 调度：`monitor/runner.py` 提供 `run_cycle()`；`monitor/cli.py` 提供常驻循环
  `python -m ddl_agent.monitor.cli run --config sources.yaml --out output --state state`，
  以及 `--once` 单轮模式。不依赖任何外部云服务。

### 14.4 输出

| 产物 | 说明 |
|---|---|
| `deadlines.xlsx` | 默认结构化文档，含 Deadlines 与 Audit Log 两个 sheet |
| `data.json` | 供网页使用的扁平结构，每轮全量重写 |
| `deadlines.ics` | 可导入系统日历，每个 confirmed 记录一个 VEVENT + 提前 1 天 VALARM |
| `calendar.html` | 自包含日历页（数据内嵌，双击可离线打开），每轮重写，打开后每 2 分钟自动重载 |
| 手机系统日历 | `PhoneCalendarWriter` 接口预留，**默认关闭**，需真机验证后再启用 |

### 14.5 模块差异

| 模块 | 变化 |
|---|---|
| `connectors/local_eml.py`、`local_chat.py` | 降级为离线测试替身，接口不变 |
| `connectors/mobile_chat.py` → `monitor/sources.py` | 升级为主路径：微信/QQ/Outlook App，按名称定位，未找到显式报错 |
| `connectors/graph_mail.py` | 不在主路径；仅保留“将来可选”骨架 |
| `monitor/config.py` | 新增：自然语言配置、持久化、增删改查、显式拒绝 |
| `monitor/cursor.py` | 新增：游标、消息库、候选库 |
| `monitor/runner.py`、`monitor/cli.py` | 新增：单轮/常驻调度与结果汇总 |
| `monitor/launcher.py`、`run_ddl_monitor.bat` | 新增：双击启动，环境检查、人话报错、单轮/常驻、打开日历页 |
| `output.py` | 新增：`data.json`、`.ics`、`calendar.html`、手机日历可选接口 |
| `pipeline.py` / `writer/` | 复用；写入器已支持改行/跳过与审计 |

### 14.6 验证边界

- 手机 GUI 读取：微信单机、单账号、自聊会话已做只读验证（`docs/real-device-test-report.md`）；QQ、真实群聊、Outlook App 收件箱、手机系统日历写入仍未验证。
- 增量、跨轮更新、调度、配置解析、`data.json` / `.ics` 导出：离线测试可真跑。
- Graph 不在主路径，仅保留未启用的代码骨架；本地连接器只作为离线测试替身，都不代表真实来源行为。

### 14.7 每步模型请求与耗时口径

| 模式 | 每步视觉请求 | 说明 |
|---|---|---|
| 旧流程（4 次） | 决策 + 语义 + 观察 + 完成检测 | 语义默认关，完成检测默认并入决策 |
| 两段式（`MERGED_OBSERVE=0`） | 2 次 | 决策 + 观察；完成判断走决策响应 |
| 合并观察（`MERGED_OBSERVE=1`，默认） | **1 次** | 一次响应同时返回 `observation` 与 `action` |
| 合并响应不合法 | 3 次 | 1 次失败合并 + observe + decide；动作仍只执行一次 |

耗时口径：
- **实测**：RapidOCR 在 1080×2374 截图上 1.2–1.5s/张；DeepSeek 决策带图约 3.5–5s；固定 settle 默认 1.0s（来自真机报告与阶段测量）。
- **估算**：合并观察后整步约 5–7s（旧两段式约 8–10s），未做真机 A/B；OCR 预热省首次 6–7s，自适应 settle 每步最多省约 1s。
- `COMPLETION_CHECK=every_step` 时不启用合并观察，避免额外 task_done 请求与退化。

### 14.8 token 与耗时数据怎么读

- 每步归档 `step_NNN.json`：`usage` 为该步视觉调用汇总（`calls[].kind` = decision/observe/completion），无 usage 时为 `null`；`timings` 为各阶段秒数。
- 任务结束/达到最大步数时写 `usage_summary.json`：总 token、每步均值、按 provider 分组；`calls_detail` 保留每次调用的 kind、model、usage。
- 阶段统计脚本：`experiments/summarize_timings.py runs/<run>`；多个目录会输出 mean/P90 对比表，用于 `MERGED_OBSERVE` A/B。
- 证据导出：`experiments/export_evidence.py runs/<run>` 只保留可公开的数值与哈希，删除截图和原始文本，产物放 `docs/evidence/`。

## 15. 与 proposal 的明确偏差

完整逐条对照见 [`docs/proposal-compliance.md`](proposal-compliance.md)。这里只记录三处容易含糊、必须明确表态的差异：

1. **Outlook 接入方式**：proposal 写 Graph API；当前**唯一主路径是手机 Outlook App GUI 读屏**，
   Graph 不在配置、规划、检索、评测路径上，只保留未启用的代码骨架作为“将来可选实现”。
   这是有意的设计决策，**不是“两种都支持、随便切换”**。
   理由：统一所有来源到手机层、复用定位/留证/设备锁，并避开课程环境无法稳定提供的 Graph 租户凭据。
   代价：读取速度、结构化程度与附件能力弱于 Graph。
2. **ground truth 来源**：proposal 写“两名组员独立标注、分歧讨论”；当前由生成器先构造事实、再由事实导出
   ground truth，等价于“可控、可复现的标注”，但没有独立人工标注。局限：无法发现生成器事实定义错误，
   且文本与规则/提示词可能共享模式，存在自我应验风险。
3. **RQ5 用户研究**：属于人工环节，当前**未做**，需要组内安排问卷/访谈与对照展示；不要在报告中写成已完成。
