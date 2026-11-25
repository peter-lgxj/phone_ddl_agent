# Three Kinds of Sample Input (normal / change / failure)

The course requires submitting three kinds of sample input: "normal task, change request, failure case". The D01, D08 and D18 here come from the
external task set `测试数据集/微Q邮/ddl_tasks.json`; only these three entries are extracted, the whole library is not imported.

| Type | Task ID | Key input | Expected result |
|---|---|---|---|
| Normal task | D01 | WeChat 「COMP7503 课程群」, find the most recent assignment notice | 机器学习作业三 / COMP7503 / 2026-11-29 23:59 |
| Change condition | D08 | The same group has both the old time of assignment 2 and the new time of assignment 3 | Report only assignment 3 at 11-29; assignment 2 at 11-15 must not appear in the result |
| Failure case | D18 | In a group with only chit-chat, look for the nonexistent 「数据库作业」 | Explicitly not found (empty result / `no_match`); must not fabricate a DDL |

## Real-device run (requires a phone, API key and test groups)

```bash
python experiments/run_experiment.py \
  --tasks examples/cases/ddl_cases.json \
  --task-ids D01,D08,D18 \
  --repeat 1
```

- The phone must be connected and authorized, and `.env` must contain `DEEPSEEK_API_KEY`.
- D01/D08 depend on the test phone having 「COMP7503 课程群」, and D18 depends on 「闲聊群」, which contains only chit-chat;
  group creation and message content are described in `测试前准备清单.md` outside the repository. If these groups are unavailable, use the offline case below.
- The script only collects `results.jsonl` and the run directory and does not judge automatically; judge manually according to the
  `judge` field in `ddl_cases.json`, and write `success` back into the results.

## Offline run (no phone or key required)

Normal + change condition (`examples/demo` already contains the 11.29 → 12.06 extension correction and the 11.31 distractor message):

```bash
python scripts/demo.py --offline
```

Expected: 3 confirmed records; the effective value of WeChat 「作业三」 is 2026-12-06 23:59,
and the old value 2026-11-29 appears only in `superseded_by` in `data.json`.

Failure case (only a chit-chat group; query the nonexistent 「数据库作业」):

```bash
python scripts/demo.py --offline \
  --demo-config examples/cases/offline_failure.json \
  --query "数据库作业的截止时间"
```

Expected: `共 0 条 DDL`, no fabricated records; output directory `demo_output/cases_failure/`.

## Boundaries

- `examples/demo/*.json` are offline stand-in archives, not real-device evidence; they must not be written as "passed on a real device".
- The actual result of the real-device samples depends on the groups, message versions and model output on the phone; when submitting the report, cite
  `results.jsonl`, run-directory screenshots and manual judging, not just expected values.
- When an old value and a new value appear at the same time, the system resolves them as a "same-source correction/extension" and outputs only the effective value; the old value is kept in
  `superseded_by` for auditing.
