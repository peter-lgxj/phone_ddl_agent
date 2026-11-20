# Proposal 符合度对照

> 对照对象：`main(1).md`（proposal）
> 对照时间：2026-11-18
> 状态定义：**✅ 已实现且可验证** / **◐ 部分实现或口径不同** / **✗ 未做**
> 验证边界：可验证性来自离线测试与合成数据；真机读屏已在单机、单账号、微信自聊会话上做过一次只读验证
> （见 `docs/real-device-test-report.md`），真实课程群、QQ / Outlook App、图片型 DDL 与真实凭据仍未验证。

## 1. 系统目标

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 用户用自然语言查询某课程/项目的 DDL | ✅ | `ddl_agent/planner.py: QueryPlanner`、`ddl_agent/pipeline.py: DdlAgent.run` | 解析课程代码、关键词与时间窗；评测中每个样本都有 query。 |
| 运行前用自然语言配置要监控的数据源 | ✅ | `ddl_agent/monitor/config.py`、`ddl_agent/monitor/cli.py` | 解析成 `sources.yaml`，支持增删改查、启停与来源原句留存；无法解析时显式拒绝。 |
| Agent 自主完成任务规划、工具选择、检索、抽取、去重、冲突验证 | ◐ | `pipeline.py`、`llm/planner.py`、`extract/*`、`dedupe.py`、`conflict.py` | 抽取/去重/冲突验证已实现；**工具选择只做到“在已配置来源中选择子集”**，没有通用工具注册表，也没有“规划任意工具序列”的循环。检索基本是一次拉取，只有冲突时追加两轮。 |
| 把结构化 DDL 写入指定输出文档 | ✅ | `writer/xlsx_writer.py`、`writer/base.py`、`output.py` | 写 xlsx；另导出 `data.json`、`deadlines.ics`、`calendar.html`。 |
| 减少跨 App 手工查找，提高正确性与完整性 | ◐ | `evaluation/run_eval.py` | 合成数据上相对单次直出有提升（见第 7 节）；真实数据尚未验证。 |

## 2. 四个挑战

| Proposal 挑战 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 异构、分散、非结构化的邮件/聊天中抽取结构化字段 | ✅ | `extract/rules.py`、`extract/llm_extractor.py`、`extract/normalizer.py` | 规则与 LLM 双实现；相对时间按注入的 `now` 归一化，无法解析标 `unresolved`。 |
| 处理重复记录与跨来源冲突 | ✅ | `dedupe.py`、`conflict.py` | 任务名归一化 + 相似度聚类；按可信度、发布时间裁决；最多两轮追加检索；仍冲突标 `uncertain`。 |
| 动态规划与工具选择，用户不会总说清要查哪个 App | ◐ | `llm/planner.py`、`planner.py: SourceConfigParser` | LLM 规划器会在已配置来源中选子集；但“已配置来源”仍由用户事先给出，且没有通用工具调用循环。加入干扰源后，动态选源仍未改变任务质量，但显著减少了无关消息的抽取调用（见第 8 节）。 |
| 可靠地修改结构化文档，保留已有有效内容 | ✅ | `writer/base.py`、`writer/xlsx_writer.py`、`writer/audit.py` | 按 `(课程, 归一化任务名)` 插行/改行；写前备份，失败重试一次后回滚；审计 CSV + xlsx sheet。 |

## 3. 解决方案逐节

### 3.1 System Overview

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 围绕单一共享 DDL 文档工作 | ✅ | `writer/xlsx_writer.py` | 默认 `deadlines.xlsx`，含 Deadlines 与 Audit Log。 |
| 从用户定义的来源配置出发 | ✅ | `monitor/config.py` | 手机路径用 `sources.yaml`；评测路径用样本里的 `source_config`。 |
| 自然语言 → 规划 → 取数 → 解析 → 去重/冲突 → 写回 | ✅ | `pipeline.py: DdlAgent.run` | 阶段清晰，trace 记录各阶段耗时、轮数、token/成本。 |
| Outlook 走 Microsoft Graph API | ◐ | `monitor/sources.py: MobileAppSource` | **设计决策**：Outlook 只走手机 App GUI 读屏，这是唯一主路径。Microsoft Graph **不在当前实现路径上**，代码骨架仅作为“将来可选实现”保留，不承诺支持，也不与手机读屏并列。原因见 3.1.1。 |
| 微信/QQ 无公开接口，走手机 GUI 自动化 | ✅（代码）/ 未验证（真机） | `monitor/sources.py: MobileAppSource`、`screen_agent` | 按名称搜索/滚动定位，找不到显式报“未找到”；真机路径未经真实数据验证。 |
| 早期用人工构造消息集合开发与单测 | ✅ | `dataset/generator.py`、`connectors/local_*.py` | 评测仍然跑在本地 `.eml`/聊天归档上，不依赖手机。 |
| 核心稳定后用真实聊天记录做有限测试 | ✗ | — | 未做，需要真机、真实账号与参与用户同意，需组内安排。 |

#### 3.1.1 Outlook 的设计决策（明确偏差）

- Proposal 写的是 “connect to Outlook through the official Microsoft Graph API”。
- **当前唯一主路径是手机 Outlook App GUI 读屏**，与微信、QQ 走同一套手机层。
- Microsoft Graph **已从主路径移除**：配置、规划、检索、评测都不使用 Graph，也不存在“Graph / 手机读屏二选一”的开关。
  `connectors/graph_mail.py` 只是保留的代码骨架，标注为“将来可选实现”，当前不启用、不测试、不作为交付能力。
- 理由：
  1. 课题组长明确要求所有来源统一走手机 GUI，复用手机智能体；
  2. Graph 需要租户/应用注册与用户授权，真实凭据无法在课程环境中稳定提供；
  3. 微信、QQ 只能走 GUI，把 Outlook 也放在同一层可以复用定位、截图、留证与设备锁，减少两套读取路径的维护成本。
- 代价：Outlook 的读取速度、结构化程度和稳定性不如 Graph API；邮件正文/附件解析能力受屏幕可见内容限制。这一点在文档与代码中均标注“未经真实数据验证”。
- 口径：论文/报告中应写“Outlook 使用手机 App 屏幕读取”，不要写“支持 Graph 与手机两种接入”。

### 3.2 Task Planning and Tool Selection

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 理解 query 中的课程/项目与时间范围 | ✅ | `planner.py: QueryPlanner`、`_parse_date_range` | 课程代码、关键词、本周/下周/本月/今天等。 |
| 判断应查询哪些已配置来源 | ✅ | `llm/planner.py: LLMQueryPlanner`、`planner.py: SourceConfigParser` | LLM 返回来源子集；规则版按名称/类型命中，未命中回落到全部来源。 |
| 工具分“取数”和“大模型推理”，由 LLM 运行时决定 | ◐ | `llm/planner.py`、`conflict.py` | 规划器选来源、冲突时决定是否继续核查；但不存在通用工具注册表，也没有任意工具序列的运行时编排。 |
| 不按固定写死的搜索序列执行 | ◐ | `pipeline.py` | 主流程仍是固定阶段顺序；动态性只体现在来源子集与冲突追加检索。 |

### 3.3 DDL Extraction and Integration

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 抽取任务名、课程/项目、截止时间、来源 | ✅ | `extract/rules.py`、`extract/llm_extractor.py`、`conflict.py` | 候选与最终记录都保留来源；输出表新增 `All Sources` 保留全部原始来源。 |
| 时间统一转 ISO-8601 | ✅ | `extract/normalizer.py` | 固定 `+08:00`；相对时间基于注入的 `now`，保证可复现。 |
| 相对时间（next Friday 等）换算成绝对时间 | ✅ | `extract/normalizer.py` | 中英文相对日期、周几、`N 天后`、`EOD` 等。 |
| 无法可靠解析时标记供人工检查 | ✅ | `models.py`、`conflict.py` | `unresolved_time` / `needs_review` → `uncertain`，不猜日期。 |
| 关键词 + 语义匹配 `HW1` vs `Assignment 1` | ✅ | `dedupe.py`、`semantic.py` | 词法归一化（Jaccard + 字符序列）与本地 embedding 余弦可组合使用；`combined` 取两者较高分，阈值可配。模型缺失/离线自动退回词法。三策略同数据对比见 8.1。 |
| 合并同一任务并保留每个原始来源 | ✅ | `dedupe.py`、`conflict.py`、`writer/xlsx_writer.py` | 本轮新增 `ResolvedRecord.sources` 与 xlsx `All Sources` 列。 |

### 3.4 Verification and Conflict Resolution

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 四级可信度：官方邮件 > 课程公告 > 教师群聊 > 学生消息 | ➖ 未按固定四级实现 | `conflict.py: _resolve_cluster` | 裁决不使用 `SOURCE_CREDIBILITY` 这类来源等级先验；同一冲突簇内所有候选等权。实际顺序是：候选自带的更正/作废标记 → `published_at` 最新者 → 多数一致；仍不一致才交 LLM 仲裁或标 `uncertain`。 |
| 更可信来源优先 | ⚠️ 仅限候选自带标记 | `conflict.py`、`llm/arbiter.py` | 不因为“来自邮件/教师群”就固定加权；只有候选文本明确写了“以此为准/作废/更正”时才按标记决定优先级，仲裁提示词也要求同等看待来源。 |
| 同权候选下新公告覆盖旧公告 | ✅ | `conflict.py` | 等权候选比较 `published_at`，最新者胜。 |
| 停止条件 1：所有相关来源查完且一致 | ✅ | `pipeline.py` + `conflict.py` | 检索器先拉取全部选中来源；候选时间一致直接 confirmed。 |
| 停止条件 2：追加两轮检索仍无法解决 | ✅ | `conflict.py: _resolve_cluster`、`settings.py: MAX_SUPPLEMENTARY_ROUNDS=2` | 规则与 LLM 两条路径都有 `while ... rounds_used < max_rounds`；测试断言恰好两轮后标 `uncertain`。 |
| 停止条件 3：模型置信度足够高 | ✅ | `llm/arbiter.py`、`settings.py: LLM_MIN_CONFIDENCE=0.6` | 低于阈值、拒答或编造候选外时间都不采纳，继续追加检索。 |
| 仍冲突 → uncertain，交人工 | ✅ | `conflict.py` | 保留全部候选与说明，不猜。 |

**关于“两轮”的核实结论**：不是只做一轮，也不是没做追加检索。`ConflictResolver` 默认 `max_rounds=2`，规则路径与 LLM 裁决路径都会在第 1、2 轮调用 `_supplement`；已有测试覆盖“第 1 轮解决”和“两轮后仍不确定”。

**仍需如实说明的差异**：离线评测的“追加检索”是对同一批静态本地消息再拉一遍，通常不会出现新证据；手机路径会重新读屏，但未做真机验证。因此“机制存在且可测试”是 ✅，“真实环境中追加检索能改变结论”尚未验证。

### 3.5 Document Updating

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 结构化表包含任务名、课程/项目、截止时间、任务状态、原始来源 | ✅ | `settings.py: DEADLINE_COLUMNS`、`writer/xlsx_writer.py` | 本轮补齐 `All Sources`。当前列：Task Name / Course / Deadline / Status / Source / All Sources / Confidence / Evidence / Last Updated。 |
| 不整表重写，插行或改行 | ✅ | `writer/base.py: merge_records` | 按 `(course, normalize(task_name))` 匹配；时间变 update，内容同 skip。 |
| 写失败重试一次，保留上一版 | ✅ | `writer/base.py` | `.bak` 备份 → 写入 → 失败重试一次 → 再失败回滚。 |
| 每次修改写审计日志 | ✅ | `writer/audit.py`、`xlsx_writer.py` | `deadlines.xlsx.audit.csv` + Audit Log sheet；每轮 `cycles/<id>.json` 记录 insert/update/skip。 |
| 支持下游日历提醒 | ✅ | `output.py` | `.ics` 带 `VALARM`；`calendar.html` 可双击打开；手机系统日历接口预留且默认关闭。 |

## 4. 数据集

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 合成模拟邮件与聊天，含真实风格 DDL | ✅ | `dataset/generator.py` | 事实驱动生成，文本只负责渲染噪声。 |
| 每个样本配人工准备 ground truth | ◐ | `dataset/generator.py` | **偏差**：ground truth 由生成器按事实直接导出，不是两名组员独立标注。等效性见 4.1。 |
| 每档 20–30 条 | ✅ | `dataset/generator.py: generate_samples(per_tier=24)` | 3 档 × 24 = 72 条。 |
| 覆盖直接抽取、重复、字段缺失、冲突、旧值被新值取代 | ✅ | `dataset/generator.py` | 另有转发链、签名/闲聊噪声、跨年/时区、姓名变体、旧 DDL 干扰等。 |
| 与查询无关的干扰来源（用于检验动态选源） | ✅ | `dataset/generator.py: _distractors` | 每个样本额外挂 4 个无关来源：实习内推群、家人群、社团活动群、其他课程群。消息带“看起来像 DDL”的诱饵（网申截止、例会报名截止、其他课程作业截止），但**不进入 ground truth，也不改写任何事实**，truth 仍只由事实导出。 |
| 两名组员独立标注并讨论分歧 | ✗ | — | 未做；用生成器事实替代。 |
| 真实聊天记录上的有限测试（需同意） | ✗ | — | 未做，需组内安排与参与用户同意。 |
| train / val / test 划分，防止泄漏 | ✅ | `dataset/generator.py`、`write_dataset` | 60/20/20 确定性划分；test 15 条。 |

### 4.1 ground truth 口径差异与等效性

- Proposal：两名组员独立标注，分歧讨论解决。
- 当前：生成器先构造事实（任务、课程、真实截止时间、来源、是否冲突），再由事实导出 ground truth，文本层只负责把事实渲染成带噪声的消息。
- 等效性：在“事实 → 文本 → ground truth”这条链上，标注不再受人工阅读误差影响，且可复现；覆盖场景由生成器显式控制。
- 局限：
  1. 没有独立标注，无法发现“生成器事实本身定义错了”的问题；
  2. 评测文本与规则/提示词可能共享部分模式，存在自我应验风险；
  3. 真实邮件/聊天的长尾格式、图片、附件、引用链远多于生成器覆盖范围。
- 因此所有指标都只应表述为“合成数据上的结果”，不能外推为真实场景性能。

## 5. Baseline

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 同一 LLM | ✅ | `evaluation/baseline.py: LLMSinglePassBaseline` | 与 Agent 共用 `LLMClient` 配置与模型。 |
| 所有原始内容 + 原 query 一次性输入 | ✅ | `baseline.py` | 单 prompt、单次调用。 |
| 无任务规划、重复工具调用、验证 | ✅ | `baseline.py` | 不去重、不核查冲突、不追加检索。 |
| 与 Agent 拿到完全相同的原始材料 | ✅ | `evaluation/run_eval.py: run_one` | 同一 `materialize_sources` 产物。 |
| 比较性能、LLM 调用次数、token、成本 | ✅ | `run_eval.py: _efficiency` | 报告含 calls / tokens / USD / 平均耗时。 |

## 6. 六项指标

| Proposal 指标 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 精确匹配（任务名 + 截止时间完全一致） | ✅ | `evaluation/metrics.py: evaluate_sample` | 任务名做归一化后比较。 |
| 模糊匹配（±1 天） | ✅ | `metrics.py` | 时区/非正式日期容忍。 |
| 召回 / 完整性 | ✅ | `metrics.py: evaluate_dataset` | matched / ground-truth 总数。 |
| 冲突解决正确率 | ✅ | `metrics.py` | 只在含冲突 ground truth 的样本上统计。 |
| 重复率 | ✅ | `metrics.py` | 多余重复行 / 全部预测行。 |
| 文档更新质量 | ◐ | `metrics.py: validate_document` | 目前只有自动结构检查（列、空值、审计表、状态）；**没有人工抽查**。 |
| 运行时间 / LLM 调用 / token / 成本 | ✅ | `run_eval.py: _efficiency` | 报告逐项列出。 |

## 7. 五个研究问题

| RQ | 状态 | 证据 | 说明 / 差异 |
| --- | --- | --- | --- |
| RQ1：Agent 是否优于单次直出 | ✅ | `experiments/ddl_agent/results/report.md`（test 集）与 `ablation_test/ablation.md` | test 集 Agent-LLM 精确 0.8125 / 召回 1.0；baseline 精确 0.500 / 召回 0.562。均为合成数据。 |
| RQ2：迭代验证是否改善重复/缺失/冲突 | ◐ | `conflict.py`、`tests/test_ddl_llm.py`、`ablation_test` | 两轮机制与测试存在；离线追加检索不产生新证据，真实收益未验证。消融显示关闭验证后冲突正确率 1.0 → 0.667。 |
| RQ3：性能收益与多步开销的权衡 | ✅ | `run_eval.py` 报告 | 同时给出质量、耗时、调用数、token、成本。 |
| RQ4：各模块贡献（消融） | ✅ | 本轮新增 `--ablation` / `--no-dedup` / `--no-verify` / `--fixed-sources`，结果见第 8 节 | 去重主要降低重复率；验证主要提升精确匹配与冲突正确率；动态来源选择在加干扰源后仍不改变任务质量，但关闭后 LLM 调用从 37 升到 67——即动态选源省的是无关消息的抽取成本。 |
| RQ5：小规模用户研究 | ✗ | — | 人工环节，未做，需组内安排问卷/访谈与对照展示。 |

## 8. RQ4 消融结果

命令：

```bash
python -m ddl_agent.evaluation.run_eval --ablation --split test \
  --out experiments/ddl_agent/results/ablation_distractors2
```

同一批 test 样本（15 条、16 条 ground truth），同一套原始消息，实际引擎为 `llm`。
本轮数据集已为每个样本加入 4 个无关来源（实习内推群、家人群、社团活动群、其他课程群），
干扰消息含“网申截止 / 例会报名截止 / 其他课程作业截止”等诱饵，但**不进入 ground truth**。
完整结果见 `experiments/ddl_agent/results/ablation_distractors2/ablation.md` 与 `ablation.json`。

| 变体 | 精确 | ±1天模糊 | 召回 | 精度 | 无关行 | 重复率 | 冲突正确率 | 文档通过率 | LLM 调用 | token | 成本 USD | 平均耗时 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full（全开） | 0.812 | 0.812 | 1.000 | 1.000 | 0 | 0.000 | 1.000 | 1.000 | 37 | 16647 | 0.007454 | 2.604 |
| no_dedup（关去重） | 0.812 | 0.812 | 1.000 | **0.889** | 0 | **0.111** | 1.000 | 1.000 | 36 | 16291 | 0.007324 | 2.500 |
| no_verify（关冲突验证） | **0.750** | **0.750** | 1.000 | 1.000 | 0 | 0.000 | **0.667** | 1.000 | 36 | 16313 | 0.007349 | 2.417 |
| fixed_sources（固定来源） | 0.812 | 0.812 | 1.000 | 1.000 | 0 | 0.000 | 1.000 | 1.000 | **67** | **27843** | **0.009177** | **3.635** |

**加入干扰源前后**

| 变体 | 加入前 LLM 调用 | 加入后 LLM 调用 | 加入前 token | 加入后 token | 质量变化 |
| --- | --- | --- | --- | --- | --- |
| full | 38 | 37 | 15968 | 16647 | 无 |
| fixed_sources | 23 | 67 | 11220 | 27843 | 无 |

**如实解读**

- 任务质量贡献最大的仍是**冲突验证/裁决**：关闭后精确匹配 −0.062，冲突正确率 −0.333。
- **去重**的可测贡献集中在重复率：关闭后 0 → 0.111，精度 1.000 → 0.889；精确匹配不变，因为精确指标按 ground-truth 匹配计，额外重复行由重复率/精度单独惩罚。
- **动态来源选择**：加入干扰源后，全开 37 次调用，`fixed_sources` 升到 67 次（+30，+81%），token 16647 → 27843（+67%），成本约 +23.1%，平均耗时 2.60s → 3.64s。质量指标完全不变。
  - 原因 1：`fixed_sources` 会跳过 LLM 规划（省下约 15 次规划调用），但把 4 个干扰来源的消息全部送进抽取器；多出的抽取/裁决调用超过省下的规划调用，净增 30 次。
  - 原因 2：LLM 抽取器能按课程与“是否像课程 DDL”过滤实习内推、家人群、社团群消息，其他课程作业也被课程过滤丢弃，因此额外消息没有变成输出行。
  - 结论：**在本数据集上，动态选源的收益是成本与延迟，不是任务质量**。这不能证明“质量上无用”，但也**不能**写成“动态选源提升了准确率”。数据集没有“选源正确率”这一 ground truth，是当前评测的明确局限。
- 所有数字来自合成数据；`full` 的精确/召回/冲突正确率与历史三方对比一致（0.8125 / 1.0 / 1.0）。

### 8.1 语义匹配：词法 / embedding / 组合

命令：

```bash
python -m ddl_agent.evaluation.run_eval --similarity-ablation --split test \
  --out experiments/ddl_agent/results/similarity_test4
```

同一批 test 样本、同一套原始消息、其余模块全开，只替换去重相似度。
模型为本地 `sentence-transformers` 的 `paraphrase-multilingual-MiniLM-L12-v2`，
缓存目录默认 `~/.cache/ddl-agent/sentence-transformers`（可用 `DDL_EMBEDDING_CACHE` 覆盖）。

| 策略 | 精确 | 召回 | 重复率 | 精度 | 无关行 | 文档通过率 | embedding 可用 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 词法（lexical） | 0.812 | 1.000 | 0.000 | 1.000 | 0 | 1.000 | — |
| 纯 embedding | 0.812 | 1.000 | **0.059** | **0.941** | 0 | 1.000 | True |
| 组合（combined） | 0.812 | 1.000 | 0.000 | 1.000 | 0 | 1.000 | True |

**结论：在当前 test 集上，embedding 没有带来质量提升。**

- 精确匹配、召回、冲突正确率三条策略完全相同（0.812 / 1.000 / 1.000），词法已经达到该数据集的上限。
- 纯 embedding 反而多出 1 行重复：`Assignment 2` 与 `HW2` 的余弦只有 0.342（< 0.6），
  而词法归一化后两者完全相同。所以纯 embedding 的重复率 0 → 0.059、精度 1.000 → 0.941。
- 组合策略取 `max(词法, embedding)`，用词法的 1.0 兜住了这个错误，结果与纯词法一致。
  这说明**组合的价值是互补与保底，不是提高本轮指标**。
- 数字口径：三条策略的 LLM 调用均为 37 次；embedding 只影响去重，不影响抽取与裁决调用。

**改写能力：有，但要如实限定**

- 单元级验证：`Midterm Report` / `期中报告` 余弦 0.652（可过 0.6），`term paper` / `期中报告` 余弦 0.476（不过阈值）。
- 曾试过在 test 集加入一条“同一任务中英改写”样本，embedding 能把精确从 0.750 抬到 0.875；
  但那等于**针对方法调整测试集**，已撤销，不进入本轮数据与结论。
- 因此当前口径是：**embedding 在真正的跨语言改写上有能力，但在现有以短代号（HW1/Assignment 1）为主的
  test 集上测不出收益，单独使用甚至会因缩写而下降**。不把 embedding 写成“已解决语义匹配”。

**离线与降级**

- `sentence-transformers` 是可选依赖，放在 `requirements-embedding.txt`，不进入主 `requirements.txt`/CI，
  避免 CI 为评测去装 torch。
- 模型默认下载到 `~/.cache/ddl-agent/sentence-transformers`（可用 `DDL_EMBEDDING_CACHE` 覆盖）；代码在加载前设置 `HF_HOME`、`SENTENCE_TRANSFORMERS_HOME`、
  `TORCH_HOME`，不写死机器盘符。
- 离线环境：先把模型目录整体拷贝到目标机器，或设置 `DDL_EMBEDDING_CACHE` 指向已有缓存，并保持模型名一致；
  加载失败的异常会写入报告的 `fallback_reason`，评测继续用词法跑完，不中断。
- 真实模型用例默认跳过；设置 `RUN_EMBEDDING_TESTS=1` 才执行。CI 与离线测试不依赖模型文件。

## 9. 仍未完成 / 需要组内安排

1. **RQ5 用户研究**：问卷或小规模对照体验，比较 Agent 输出与单次直出；需要组内排期。
2. **真机验证**：手机读屏、按名称定位、Outlook App 收件箱、手机系统日历写入；目前均标注“未经真实数据验证”。
3. **真实聊天/邮件测试**：需要真实账号、参与用户同意与数据脱敏。
4. **人工文档抽查**：当前只有自动结构检查，缺少 proposal 要求的人工检查环节。
5. **独立双人标注**：当前 ground truth 由生成器事实导出，需补一次独立标注或抽样复核来验证生成器事实。
6. **语义匹配的剩余缺口**：embedding 已接入，但在当前 test 集上没有带来提升；纯 embedding 对 `Assignment 2` / `HW2` 这类缩写反而下降。跨语言改写只在单元级验证过（`Midterm Report` / `期中报告` 0.652，`term paper` / `期中报告` 0.476 仍不过阈值）。
7. **通用工具编排**：当前“工具选择”仅限来源子集与冲突追加检索，尚未实现通用工具注册表/任意工具序列。
8. **选源指标缺失**：数据集没有“应该选哪些来源”的 ground truth，因此动态选源只能报告成本/延迟收益，无法量化选源正确率。
