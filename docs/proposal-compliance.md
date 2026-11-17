# Proposal 符合度对照

> 对照对象：`main(1).md`（proposal）
> 对照时间：2026-11-18
> 状态定义：**✅ 已实现且可验证** / **◐ 部分实现或口径不同** / **✗ 未做**
> 验证边界：可验证性来自离线测试与合成数据；真机读屏、真实凭据、真实聊天记录均未验证。

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
| 动态规划与工具选择，用户不会总说清要查哪个 App | ◐ | `llm/planner.py`、`planner.py: SourceConfigParser` | LLM 规划器会在已配置来源中选子集；但“已配置来源”仍由用户事先给出，且没有通用工具调用循环。离线消融中该模块没有体现出任务质量收益。 |
| 可靠地修改结构化文档，保留已有有效内容 | ✅ | `writer/base.py`、`writer/xlsx_writer.py`、`writer/audit.py` | 按 `(课程, 归一化任务名)` 插行/改行；写前备份，失败重试一次后回滚；审计 CSV + xlsx sheet。 |

## 3. 解决方案逐节

### 3.1 System Overview

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 围绕单一共享 DDL 文档工作 | ✅ | `writer/xlsx_writer.py` | 默认 `deadlines.xlsx`，含 Deadlines 与 Audit Log。 |
| 从用户定义的来源配置出发 | ✅ | `monitor/config.py` | 手机路径用 `sources.yaml`；评测路径用样本里的 `source_config`。 |
| 自然语言 → 规划 → 取数 → 解析 → 去重/冲突 → 写回 | ✅ | `pipeline.py: DdlAgent.run` | 阶段清晰，trace 记录各阶段耗时、轮数、token/成本。 |
| Outlook 走 Microsoft Graph API | ◐ | `connectors/graph_mail.py` | **设计决策**：主路径改为手机 Outlook App GUI 读屏；Graph 连接器保留为将来可选实现与离线替身。原因见 3.1.1。 |
| 微信/QQ 无公开接口，走手机 GUI 自动化 | ✅（代码）/ 未验证（真机） | `monitor/sources.py: MobileAppSource`、`screen_agent` | 按名称搜索/滚动定位，找不到显式报“未找到”；真机路径未经真实数据验证。 |
| 早期用人工构造消息集合开发与单测 | ✅ | `dataset/generator.py`、`connectors/local_*.py` | 评测仍然跑在本地 `.eml`/聊天归档上，不依赖手机。 |
| 核心稳定后用真实聊天记录做有限测试 | ✗ | — | 未做，需要真机、真实账号与参与用户同意，需组内安排。 |

#### 3.1.1 Outlook 的设计决策（明确偏差）

- Proposal 写的是 “connect to Outlook through the official Microsoft Graph API”。
- 当前实现把它改成 **手机 Outlook App GUI 读屏**，Graph 仅作为将来可选的第二实现。
- 理由：
  1. 课题组长明确要求所有来源统一走手机 GUI，复用手机智能体；
  2. Graph 需要租户/应用注册与用户授权，真实凭据无法在课程环境中稳定提供；
  3. 微信、QQ 只能走 GUI，把 Outlook 也放在同一层可以复用定位、截图、留证与设备锁，减少两套读取路径的维护成本。
- 代价：Outlook 的读取速度、结构化和稳定性不如 Graph；邮件正文/附件解析能力受屏幕可见内容限制。这一点在文档与代码中均标注“未经真实数据验证”。

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
| 关键词 + 语义匹配 `HW1` vs `Assignment 1` | ◐ | `dedupe.py` | **语义匹配目前是字符级相似度**（token Jaccard + difflib），不是 embedding。接口可替换，但当前离线口径应如实描述为“词法 + 字符相似度”。 |
| 合并同一任务并保留每个原始来源 | ✅ | `dedupe.py`、`conflict.py`、`writer/xlsx_writer.py` | 本轮新增 `ResolvedRecord.sources` 与 xlsx `All Sources` 列。 |

### 3.4 Verification and Conflict Resolution

| Proposal 要求 | 状态 | 实现位置 | 说明 / 差异 |
| --- | --- | --- | --- |
| 四级可信度：官方邮件 > 课程公告 > 教师群聊 > 学生消息 | ✅ | `settings.py: SOURCE_CREDIBILITY`、`conflict.py` | `official_email=4`、`course_announcement=3`、`instructor_chat=2`、`student_chat=1`。 |
| 更可信来源优先 | ✅ | `conflict.py: _resolve_cluster` | 最高可信度组内一致即采用。 |
| 同可信度下新公告覆盖旧公告 | ✅ | `conflict.py` | 比较 `published_at`，最新者胜。 |
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
| RQ4：各模块贡献（消融） | ✅ | 本轮新增 `--ablation` / `--no-dedup` / `--no-verify` / `--fixed-sources`，结果见第 8 节 | 去重主要降低重复率；验证主要提升精确匹配与冲突正确率；动态来源选择在当前离线 test 集上没有质量收益，但关闭后少 15 次规划调用。 |
| RQ5：小规模用户研究 | ✗ | — | 人工环节，未做，需组内安排问卷/访谈与对照展示。 |

## 8. RQ4 消融结果

命令：

```bash
python -m ddl_agent.evaluation.run_eval --ablation --split test \
  --out experiments/ddl_agent/results/ablation_test
```

同一批 test 样本（15 条），同一套原始消息，实际引擎为 `llm`。完整结果见
`experiments/ddl_agent/results/ablation_test/ablation.md` 与 `ablation.json`。

| 变体 | 精确 | ±1天模糊 | 召回 | 重复率 | 冲突正确率 | 文档通过率 | LLM 调用 | token | 成本 USD | 平均耗时 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full（全开） | 0.812 | 0.812 | 1.000 | 0.000 | 1.000 | 1.000 | 38 | 15968 | 0.007399 | 2.500 |
| no_dedup（关去重） | 0.812 | 0.812 | 1.000 | **0.158** | 1.000 | 1.000 | 36 | 15217 | 0.007097 | 2.368 |
| no_verify（关冲突验证） | **0.750** | **0.750** | 1.000 | 0.000 | **0.667** | 1.000 | 36 | 15150 | 0.007023 | 2.254 |
| fixed_sources（固定来源） | 0.812 | 0.812 | 1.000 | 0.000 | 1.000 | 1.000 | **23** | **11220** | **0.004784** | **1.454** |

**如实解读**

- 任务质量贡献最大的是**冲突验证/裁决**：关闭后精确匹配 −0.062，冲突正确率 −0.333。
- **去重**的可测贡献集中在重复率：关闭后 0 → 0.158；精确匹配不变，因为当前精确指标按 ground-truth 匹配计，额外重复行由重复率单独惩罚。
- **动态工具选择**在当前 test 集上没有质量差异；关闭后 LLM 调用从 38 降到 23、token 从 15968 降到 11220。原因是这些样本的 `source_config` 基本覆盖全部相关来源，规划器没有“排除了干扰源”的空间。这不能证明该模块无用，只说明当前数据集没有覆盖“该查 A、不该查 B”的场景。
- 所有数字来自合成数据；`full` 与历史三方对比中的 Agent-LLM 数字一致（0.8125 / 1.0 / 0 / 1.0）。

## 9. 仍未完成 / 需要组内安排

1. **RQ5 用户研究**：问卷或小规模对照体验，比较 Agent 输出与单次直出；需要组内排期。
2. **真机验证**：手机读屏、按名称定位、Outlook App 收件箱、手机系统日历写入；目前均标注“未经真实数据验证”。
3. **真实聊天/邮件测试**：需要真实账号、参与用户同意与数据脱敏。
4. **人工文档抽查**：当前只有自动结构检查，缺少 proposal 要求的人工检查环节。
5. **独立双人标注**：当前 ground truth 由生成器事实导出，需补一次独立标注或抽样复核来验证生成器事实。
6. **真正的语义匹配**：当前是词法 + 字符相似度；如需 embedding，需要新增依赖与离线缓存。
7. **通用工具编排**：当前“工具选择”仅限来源子集与冲突追加检索，尚未实现通用工具注册表/任意工具序列。
