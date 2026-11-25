# Comparison Experiment Plan

> This file is the **single source of truth** for the task set and statistical criteria. The repeat count, the systems participating in Level 3, and the AT aggregation criterion are marked [TBD], and after the team decides, only Section 4 needs to change; other sections, `tasks.json`, and the defaults in `metrics.py` are references or implementation defaults only and do not represent the final criteria.

## 1. Comparison Systems

| System | Positioning | How to get it |
|------|------|---------|
| **Open-AutoGLM** | The most direct competitor, Chinese Android scenario | github.com/zai-org/Open-AutoGLM |
| **Mobile-Agent-v2** | Mainstream academic baseline, multi-agent architecture | github.com/X-PLUG/MobileAgent |
| **AppAgent** | Foundational work in the single-agent line | github.com/TencentQQGYLab/AppAgent |
| **Ours (this system)** | Dual-model layering + multi-app orchestration | — |

Mobile-Agent-v3 and AppAgent-v2 have high deployment costs and are not included in this round. The four systems form a gradient of "basic → advanced → multi-agent → ours".

## 2. Evaluation Metrics

There are 6 metrics in total, all based on the manual `success` judgement in `results.jsonl`.

**① Task Success Rate (TSR)**

```
TSR = successful tasks / judged tasks × 100%
```

When a task has multiple repeats, the task counts as successful if any attempt succeeds (best of).

**② Step Efficiency (SE)**

```
SE = theoretical steps for the task / actual executed steps
```

Only successful attempts are counted; when there are multiple successful attempts, average them first and then average over tasks. Theoretical step counts are in `theory_steps` of the task set.

**③ Multi-App Task Success Rate (Multi-App TSR)**

Only tasks with `apps` length ≥ 2 are counted, computed the same way as TSR.

**④ Average Time (AT)**

Per-task time criterion: `mean elapsed time of the attempts included in the statistics for that task`.

There are two candidate formulas for cross-task aggregation (**criterion pending, see 4.2**):

```
A. total_per_task = total elapsed time of all included attempts / number of tasks
B. task_mean      = first compute each task's mean elapsed time, then average equally over tasks
```

The two are equal when every task has the same number of attempts; they diverge when attempt counts differ (timeouts, failed replays).
`--at-mode` decides which attempts are included (all / success / best), orthogonal to the aggregation criterion above.

**⑤ Average Step Time (ST)**

```
ST = total elapsed time / total steps (seconds/step)
```

**⑥ Completion Stability (CS)**

```
CS = tasks where all repeats succeeded / judged tasks × 100%
```

The repeat count follows the procedure in 4.2 (**the Level 3 repeat count is TBD**). CS requires per-attempt judgement (`{"T01": [true, false, true]}`); tasks that only provide a task-level boolean do not enter the CS denominator, and `metrics.py` lists them as `CS_pending`.

## 3. Test Task Set

There are 20 tasks in total, in three levels; the task text is defined by `experiments/tasks.json`.

### Level 1: Single-app basic tasks (8)

| ID | Task description | Target App | Theoretical steps |
|------|---------|---------|---------|
| T01 | 打开微信，向「文件传输助手」发送「测试消息123」 | 微信 | 5 |
| T02 | 打开设置，查看当前 WiFi 名称 | 系统设置 | 4 |
| T03 | 打开高德地图，搜索「天安门」，查看当前步行距离 | 高德地图 | 4 |
| T04 | 打开淘宝，搜索「机械键盘」，按销量排序，报告第一名价格 | 淘宝 | 6 |
| T05 | 打开美团，搜索「黄焖鸡」，报告附近评分最高的店名 | 美团 | 5 |
| T06 | 打开微信，进入「发现」→「朋友圈」，报告最新一条动态内容 | 微信 | 4 |
| T07 | 打开网易云音乐，搜索「稻香」，播放第一首 | 网易云音乐 | 5 |
| T08 | 打开京东，搜索「AirPods Pro」，收藏第一个商品 | 京东 | 6 |

### Level 2: Cross-app two-App tasks (8)

| ID | Task description | Apps involved | Theoretical steps |
|------|---------|---------|---------|
| T09 | 打开微信，阅读指定联系人最新消息，提取消息中的地点，用高德地图导航过去 | 微信+高德 | 9 |
| T10 | 打开微信，阅读指定联系人推荐的餐厅名，去大众点评搜索该餐厅，报告评分 | 微信+大众点评 | 9 |
| T11 | 打开备忘录，读取购物清单第一项，去淘宝搜索并报告价格 | 备忘录+淘宝 | 8 |
| T12 | 打开微信，阅读指定联系人发来的商品名，去京东搜索，报告最低价 | 微信+京东 | 9 |
| T13 | 打开高德地图，搜索附近的星巴克，记录最近一家的地址，发微信告知指定联系人 | 高德+微信 | 10 |
| T14 | 打开美团，找到「黄焖鸡米饭」评分最高的店，将店名用微信发给指定联系人 | 美团+微信 | 10 |
| T15 | 打开淘宝搜索指定商品，记录价格，发微信告知指定联系人 | 淘宝+微信 | 10 |
| T16 | 打开微信阅读联系人消息中的歌曲名，打开网易云音乐搜索并播放 | 微信+网易云 | 9 |

### Level 3: Three-App chained tasks (4)

| ID | Task description | Apps involved | Theoretical steps |
|------|---------|---------|---------|
| T17 | 读微信联系人消息提取餐厅名 → 大众点评查地址 → 高德地图导航 | 微信+大众点评+高德 | 14 |
| T18 | 淘宝搜索商品记录价格 → 京东搜索同款比价 → 微信发送比价结果 | 淘宝+京东+微信 | 14 |
| T19 | 打开微信找到与指定联系人的对话，确认对方在哪，用高德查询公共交通路线，再用微信回复路线安排 | 微信+高德+微信 | 13 |
| T20 | 美团查餐厅评分 → 网易云音乐搜索该餐厅 BGM → 微信分享给联系人 | 美团+网易云+微信 | 15 |

Which systems participate in Level 3, and how non-participating systems are scored, are covered in **4.2 pending items**; this section does not prescribe it separately.

## 4. Execution Procedure and Pending Criteria

### 4.1 Test environment

- The same phone (Honor 70 or vivo X200), with consistent network conditions (same WiFi)
- Every system uses the same base model (Qwen-VL-Max) to ensure fairness
- Before each task, the runner automatically "returns to the home screen + force-stops known apps"; in-app drafts, the chat input box, clipboard and IME state **cannot be cleared automatically**, so manual checking is required (`--no-reset` disables the automatic reset; the `reset` field records `stopped/failed/not_installed/not_mapped`)

### 4.2 Pending criteria (require a team decision)

The following three items are **not yet finalized**; the defaults in the code are implementation defaults, not experiment conclusions.

| Pending item | Candidates | Current implementation default | Where to change after the decision |
|---|---|---|---|
| **A. Level 3 repeat count** | A1: same as L1/L2, 3 times; A2: 2 times | `repeat_by_level.3 = 2` in `tasks.json` | `repeat_by_level` in `tasks.json` + this section |
| **B. Systems participating in Level 3** | B1: all four systems run, those not supporting multi-app are recorded as failures; B2: only our system + Mobile-Agent-v2 participate, the rest are marked N/A and excluded from the denominator | none (the runner does not distinguish systems) | this section + the baseline results summary table |
| **C. AT cross-task aggregation** | C1: `total_per_task` (total time ÷ number of tasks); C2: `task_mean` (average within a task first, then equal-weight across tasks) | `--at-aggregate task_mean` in `metrics.py` | the default in `metrics.py` or run arguments + this section |

Supplementary rules:

- **Execution rule for repeat counts**: when `--repeat` is not specified, follow the per-level rule (`repeat_by_level`, falling back to `default_repeat` when absent); an explicit `--repeat N` **forcibly overrides all levels** (including Level 3). When using `--repeat`, the result tag / manifest must make clear that this run did not follow the per-level procedure.
- **Timeouts/crashes** are always recorded as failures and are not included in the AT success criterion.
- While the criterion is pending, both AT numbers may be computed for comparison, but the paper/report may cite only the one confirmed by the team.

### 4.3 Manual judging criteria

```
Success criteria:
  - Information extraction: the extracted entity names are exactly correct (semantically close but textually different is not accepted)
  - Operation execution: the target interface/state is reached (screenshot as evidence)
  - Cross-app: every subtask is completed and information is passed correctly

Failure criteria:
  - Entity recognition error (「财富又一城」 misidentified as a restaurant name)
  - Step limit reached without completion (MAX_STEPS defaults to 30)
  - Crash/error midway
  - Operation completed but content wrong (wrong content sent)
```

## 5. Expected Results and Analysis Framework

| Metric | AppAgent | Open-AutoGLM | Mobile-Agent-v2 | Ours |
|------|---------|--------------|----------------|-------|
| Single-App TSR | Medium | Medium-high | High | High |
| Two-App TSR | Low | Low | Medium | **High** |
| Three-App TSR | Very low/0 | Very low/0 | Low | **Medium-high** |
| Average time | Slow | Medium | Medium | Medium (one extra text-model call) |
| Step efficiency | Low | Medium | Medium | Medium-high |
| Stability CS | Low | Medium | Medium | **High** (effect of the semantic correction layer) |

Key points of the paper analysis:

1. **Advantage on multi-app tasks**: the TSR gap on Level 3 explains the point best (the participating systems follow 4.2-B).
2. **Contribution of the semantic correction layer to stability**: analyze CS and use concrete cases to show how entity-recognition errors of the VL model are corrected by the text model.
3. **Reasonableness of the time cost**: dual-model layering adds latency; discuss it in the "accuracy-efficiency trade-off" subsection.

## 6. Presentation Structure in the Paper

```
5.1 Experiment environment and configuration
5.2 Evaluation task set design
5.3 Evaluation metric definitions
5.4 Experimental results and analysis
    5.4.1 Overall comparison (6 metrics × 4 systems)
    5.4.2 Analysis by difficulty level
    5.4.3 Dedicated analysis of multi-app tasks
    5.4.4 Typical case analysis
5.5 Ablation experiments
    - Remove the semantic analysis layer vs keep it
    - Remove multi-app planning vs keep it
5.6 Experimental conclusions
```

The ablation only needs two variants of our system and does not require deploying the other systems.

## 7. Correspondence with the Code

| Experiment setting | Code entry |
|---|---|
| 20 tasks | `experiments/tasks.json` (`apps` / `theory_steps` / `level`) |
| Per-level repeats | Reads `repeat_by_level` by default; `--repeat N` forces all levels to be uniform |
| Run only some tasks | `--levels L1,L2` / `--task-ids T01,T05`; filter conditions are written to `manifest.json` and to `filters` in each `results.jsonl` line |
| Single-app / multi-app | `--mode single` / `--mode multi` |
| Maximum 30 steps | `config.MAX_STEPS` (default 30, overridable with the `MAX_STEPS` environment variable) |
| Six metrics | `python experiments/metrics.py experiments/results/<run_dir>` |
| AT aggregation criterion | `--at-aggregate total_per_task` / `task_mean` (default task_mean, see 4.2-C) |
| AT sample criterion | `--at-mode all / success / best` |
| Ablation: remove semantic analysis layer | `--no-semantic` |
| Ablation: remove multi-app planning | `--mode single` on Level 2/3 tasks |
| Comparison systems | `experiments/baselines/` (the three wrappers are integrated at the upstream entry points, see that directory's README) |

The script only collects and aggregates; a task's "success/failure" is still judged manually against the criteria in 4.3, and must be annotated in the `success` field of `results.jsonl` or in `judgement.json`.
