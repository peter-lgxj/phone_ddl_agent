# DDL Agent 架构设计

> 状态：设计稿（阶段二）。对应需求文档 `main(1).md`：按自然语言查询，从多个来源采集 DDL，
> 规划/选工具、字段抽取与时间归一化、跨来源去重、按可信度与发布时间解决冲突，写入共享结构化文档，
> 并配套合成数据集、单次直出 baseline 与评测。
>
> 本文只描述设计，不含未实现内容；实现进度在阶段三单独回报。

## 1. 目标与边界

### 1.1 目标

- 用户用自然语言描述要跟踪的数据源（`source_config`），再用自然语言提问（`query`）。
- Agent 自主完成：解析查询 → 选择数据源/工具 → 检索 → 抽取 → 归一化 → 去重 → 冲突核查（最多两轮追加检索）
  → 写入共享文档（插行/改行 + 审计日志）。
- 全流程可在**本地模拟数据**上离线跑通并评测，不依赖真机、不依赖真实 Microsoft Graph 权限。

### 1.2 明确的边界

| 事项 | 本阶段处理方式 |
|---|---|
| Outlook 真实邮件 | 抽象出 `MessageSource`，提供 Graph 实现骨架；默认使用本地 `.eml` 目录实现。Graph 路径**未经真实数据验证**。 |
| 微信/QQ 真实聊天记录 | 复用现有手机 GUI 自动化层做 `MobileChatConnector`；默认使用本地聊天归档实现。真机路径**未经真实数据验证**。 |
| LLM 依赖 | 抽取/冲突环节定义 `LLMExtractor`/`ConflictArbiter` 接口；离线默认用规则实现，LLM 后端可注入。 |
| 现有手机层 | **不改动任何公开行为**，只作为库被 `MobileChatConnector` 调用。 |
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
| `screen_agent.ScreenAgent` | `MobileChatConnector` 以自然语言任务驱动其执行“打开微信 → 进入指定群/会话 → 向上翻页读取消息” | 只调用公开方法，不改行为 |
| `ocr_locator.OCRLocator` / `detect_tree` / `snap_action_coords` | 由 ScreenAgent 内部使用；连接器不直接调用 | 不改接口 |
| `phone_controller.PhoneController` | 由 ScreenAgent 内部使用（截图、tap、type_text） | 不改接口 |
| `device_lock.DeviceLock` | 连接器在整段读取周期外层加锁，防止与其他实验并发操作手机 | 只读复用 |
| `runs/` 留证目录 | 连接器把每步截图与 `step_NNN.json` 作为 `RawMessage.evidence_path` | 只读复用 |

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
| `LocalEmlConnector` | 默认，离线可跑 | 读取 `*.eml` 目录，按 `Date`/`From`/`Subject` 解析；模拟收件箱 |
| `GraphMailConnector` | 骨架，**未经真实数据验证** | 通过 Microsoft Graph `/me/messages` 拉取；无凭据时抛 `SourceNotConfigured`，不静默降级 |
| `LocalChatConnector` | 默认，离线可跑 | 读取聊天归档 JSON（`{sender, role, timestamp, text}`） |
| `MobileChatConnector` | 骨架，**未经真机验证** | 复用 ScreenAgent 读取微信/QQ；手机已拔线，未跑通 |

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
  extract/{rules,normalizer,llm_extractor}.py
  llm/{client,planner,arbiter}.py
  factory.py                  # LLM_ENABLED 开关与规则降级
  dedupe.py
  conflict.py
  writer/{base,xlsx_writer,csv_writer,markdown_writer,audit}.py
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

## 11. 验证状态标注

| 能力 | 状态 |
|---|---|
| 本地 `.eml` / 聊天归档连接器、抽取、归一化、去重、冲突、写入、评测 | 已实现，离线测试真跑 |
| DeepSeek 规划 / 抽取 / 冲突裁决 | 已真实调用（合成数据），用量与成本写入报告 |
| Graph 邮件连接器 | 骨架，**未经真实数据验证** |
| 微信/QQ 真机读取 | 复用现有手机层，**未经真机验证**（设备已拔线） |
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
6. （后续）Graph 真实接入、真机聊天读取、真实数据上的复测与消融实验。
