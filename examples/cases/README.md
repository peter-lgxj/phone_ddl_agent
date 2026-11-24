# 三类样例输入（普通 / 变更 / 失败）

课程要求提交“普通任务、变更请求、失败案例”三类样例输入。这里的 D01、D08、D18 来自
外部任务集 `测试数据集/微Q邮/ddl_tasks.json`，只抽取了这三条，不引入整库。

| 类型 | 任务 ID | 输入要点 | 期望结果 |
|---|---|---|---|
| 普通任务 | D01 | 微信「COMP7503 课程群」，找最近一条作业通知 | 机器学习作业三 / COMP7503 / 2026-11-29 23:59 |
| 变更条件 | D08 | 同群同时有作业二旧时间和作业三新时间 | 只报作业三 11-29；作业二 11-15 不得出现在结果里 |
| 失败案例 | D18 | 只有闲聊的群里找不存在的「数据库作业」 | 明确未找到（空结果 / `no_match`），不得编造 DDL |

## 真机运行（需要手机、API key 和测试群）

```bash
python experiments/run_experiment.py \
  --tasks examples/cases/ddl_cases.json \
  --task-ids D01,D08,D18 \
  --repeat 1
```

- 需要手机已连接并授权，`.env` 里有 `DEEPSEEK_API_KEY`。
- D01/D08 依赖测试手机上的「COMP7503 课程群」，D18 依赖只有闲聊的「闲聊群」；
  建群与消息内容见仓库外的 `测试前准备清单.md`。没有这些群时，用下面的离线案例。
- 脚本只采集 `results.jsonl` 和运行目录，不自动判分；按 `ddl_cases.json` 里的
  `judge` 字段人工判定，并把 `success` 写回结果。

## 离线运行（不需要手机和 key）

普通 + 变更条件（`examples/demo` 里已包含 11.29 → 12.06 的延期更正和 11.31 的干扰消息）：

```bash
python scripts/demo.py --offline
```

期望：3 条 confirmed；微信「作业三」生效值为 2026-12-06 23:59，
旧值 2026-11-29 只出现在 `data.json` 的 `superseded_by`。

失败案例（只有一个闲聊群，查询不存在的「数据库作业」）：

```bash
python scripts/demo.py --offline \
  --demo-config examples/cases/offline_failure.json \
  --query "数据库作业的截止时间"
```

期望：`共 0 条 DDL`，不输出编造记录；输出目录 `demo_output/cases_failure/`。

## 边界

- `examples/demo/*.json` 是离线替身归档，不是真机证据；不能写成“真机通过”。
- 真机样例的实际结果取决于手机上的群、消息版本和模型输出；提交报告时引用
  `results.jsonl`、运行目录截图与人工判分，不要只写期望值。
- 旧值与新值同时出现时，系统按“同源更正/延期”裁决，只输出生效值；旧值保留在
  `superseded_by` 供审计。
