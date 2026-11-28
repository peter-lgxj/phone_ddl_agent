# PR #1 Review Summary: `fix/p0-run-blockers` → `main`

Audience: peter-lgxj, calmmmiu
Scope: PR #1 (the 1 commit on main since `967c58a` after the squash merge), covering P0 blockers, P1 correctness, P2 experiment support and engineering.

---

## 1. What changed this time (grouped by priority)

### P0: cannot run without a fix / sends duplicate messages

| Issue | Handling |
|---|---|
| `phone_controller.py` defines the same methods twice (`type_text`, `tap_input_box` one copy each), and the later definition overrides the earlier one, so "input immediately sends" and Enter is pressed again | Merged into a single implementation; input is separated from sending; `send` no longer presses Enter again |
| The input path crashes when `models/input_box.pt` is missing | `InputDetector` initialization failure falls back to the uiautomator2 view tree (`EditText`) and only prints a hint |
| No preflight validation at startup; errors point at low-level exceptions | Added API key / adb / device checks with actionable hints |
| Streamlit state never updates (`ui_state` is never called), so the page stays running forever | The main loop writes running/finished/error; completion, step limit and exceptions all have explicit states |
| Multi-app mode constructs the executor repeatedly (connects u2 twice, loads OCR twice) | Reuse the same executor |

### P1: correctness and reproducibility

- **Cross-app information passing implemented**: `context_needed` used to be written only into the prompt and discarded during parsing; it is now persisted to `SubTask`, updated on replanning, and injected into the subtask goal before execution (`build_exec_goal`)
- **Failures are no longer treated as completion**: single-step retry wraps only the "decision", and the action executes only once (WeChat will not send a message twice); a failed observation invalidates the old screenshot, and the old frame is no longer used to decide completion
- **Multi-app overall verdict**: subtask failure/timeout/pending stuck on a dependency all propagate to an overall failure, and `results.jsonl` records `success=false`; `depends_on` now filters invalid indices, breaks circular dependencies, and prints a correction log
- **Fixed sampling**: decision/observation/completion check uniformly use `temperature=0`; retry count and backoff are configurable
- **Remove hardcoding**: WeChat resource-id is configurable; hardcoded coordinates are deleted; swipes are computed from the resolution
- **Evidence**: per-step `step_NNN.json` (with the full action and coordinates) plus before/after screenshots; `task_log.json` saves the full action
- **Device mutual exclusion**: `runs/.device.lock` covers reset plus the whole task, with token/pid ownership validation; the Web, CLI and experiment runner share it; a task that cannot acquire the lock fails immediately instead of the processes force-stopping each other
- **Truthful reset reporting**: counted as stopped only when adb returns 0 and the app is installed; offline devices and uninstalled apps are recorded separately

### P2: experiment support and engineering

- `experiments/tasks.json`: 20 tasks (L1/L2/L3, including theoretical step counts) for TSR/SE/AT/ST computation
- `experiments/run_experiment.py`: repeated execution, single/multi-app and baseline modes, per-task reset, manifest (git SHA/dirty, full model name, temperature, device information)
- `experiments/metrics.py`: TSR, multi-app TSR, SE, AT, ST, CS; CS refuses to compute when the judgement granularity is mixed and reports coverage
- `experiments/baselines/`: stdin/stdout JSON protocol + generic subprocess adapter + three wrapper skeletons (**not installed, not integrated**)
- `tests/`: 650 passed, 2 skipped (652 collected; the skipped ones are optional cases that need the local embedding model); the other cases do not depend on a real device/model; CI runs compileall, tasks validation, dry-run, ruff, pytest
- Dependencies: `requirements.txt` (version lower bounds) + `requirements-lock.txt` (resolved on this machine with 3.12) + `requirements-dev.txt`
- Docs: README adds run prerequisites, configuration table, project structure, experiment usage, weight sources, privacy notes; the 30-step criterion and leftover T19 text in `test.md` have been cleaned up

---

## 2. Why (corresponding real defects)

Mainly from the `现状审查清单` (current-state review checklist) and five rounds of re-review; all are reproducible defects, not style preferences:

1. Duplicate method definitions are a **functional bug**: `type_text` presses Enter internally and `send` presses Enter again, so WeChat sends two messages in a row
2. Missing weights make the `InputDetector` constructor throw, so the task never reaches the input stage
3. `ui_state` not updating makes the Web side unable to tell whether a task has finished
4. Retry wrapping the action, `finish` returning directly, and pending not counted as failure can **record a failure as success**, directly polluting experiment conclusions
5. Mixing `judgement` granularity inflates CS into TSR, which is **metric distortion**
6. reset wrapping only the lock / not the task lets two processes operate the phone at the same time; all of these were reproduced during re-review

---

## 3. How far verification goes

### Actually run (local machine)

- Python 3.12.14 venv: `pytest` **650 passed, 2 skipped** (652 collected), `ruff` all green, `run_experiment.py --dry-run` passed
- Real device: OPPO PLJ110 / Android 17 / 1080×2374 (because Android 17 is incompatible with uiautomator2 3.7.1, the device uses `SCREEN_DRIVER=adb_dump`), WeChat 「文件传输助手」 self-chat conversation, single device, single account, read-only; including scroll OCR observation, timeout partial results, and end-to-end extraction; see `docs/real-device-test-report.md`
- GitHub Actions: based on the latest run of this PR

### Static review / limited real device

- Retry boundaries, observation validity, context injection, dependency correction, lock mutual exclusion, reset results — all have unit tests covering the logic; on the real device only read-only reading and extraction were verified, **actual tapping/input/send behavior was not verified**
- Action execution, OCR/YOLO localization, and keyboard-popup detection only received code-level review
- Real course groups, QQ / Outlook App, and image-type DDLs are not verified; token/cost of the screen-reading vision model is not counted

### Must be confirmed on a real device / real environment

- Cross-resolution tapping and swiping, actual WeChat input-box localization, send behavior
- Actual detection performance of the YOLO weights and the threshold (0.3/0.5) trade-off
- Real stability of the model API, whether retries are sufficient, actual behavior and cost of temperature=0
- 20 tasks × (3/3/2) real experiment data and manual judging to obtain TSR/CS/SE/AT/ST numbers
- Installation and integration of the three baselines

---

## 4. What is still missing

| Item | Status |
|---|---|
| Real-device interaction verification | Partially executed: read-only reading of a single-device self-chat conversation and one end-to-end extraction; real group chats/QQ/Outlook not executed |
| Real API calls | Executed: DeepSeek screen reading and extraction; stability and cost statistics not done |
| Real experiment data | Only the task set and runner, no results/manual judging |
| Baseline implementation | Only the protocol and wrapper skeletons; none of the three systems installed |
| Weights | The repository does not include binaries; fetch `best.pt` from `peter-lgxj/gui_yolo_model` and place it at `models/input_box.pt` (README has a direct link and sha check) |
| LICENSE | `gui_yolo_model` has no LICENSE; whether the weights can be distributed with the project needs the team lead's confirmation |
| Branch protection | Needs admin configuration |

---

## 5. Suggested merge method and post-merge notes

**Suggestion**: merge this PR with **Squash merge**, keeping the PR body and this document as the change record; do not push directly to main.

**After merge, team members**:

```bash
git checkout main
git pull --ff-only
git checkout -b <自己的分支>
```

Notes:

1. `models/input_box.pt` is not in the repository; after pulling the code the weights still need to be downloaded separately (README section "Input box detection weights")
2. Install dependencies from `requirements.txt`; to fully reproduce the experiment environment use `requirements-lock.txt` (resolved on Windows + Python 3.12; regenerate on another platform)
3. Before running, confirm `adb devices` shows a device and `.env` has the corresponding `*_API_KEY`; without a key, startup validation fails directly
4. Do not run the Web UI and the experiment runner on the same phone at the same time: the device lock prevents concurrency, and a task that cannot acquire the lock fails directly
5. Before experiments, read `experiments/README` (if present) and `test.md`: CS requires **per-attempt judgement** (`{"T01": [true,false,true]}`); task-level booleans make CS refuse to compute
6. `runs/` per-step screenshots + JSON keep growing; automatic run directories are limited to keeping 20 by default (`RUNS_KEEP` adjustable), manual evidence directories are not subject to automatic cleanup

---

## 6. 2026-11-22 re-verification addendum: reading labels and judging criteria

This section only registers **source-labelled** conclusions and does not change the historical review records above.

### 6.1 Label definitions

| Label | Meaning |
| --- | --- |
| Measured on device | Directly produced from device screenshots, `step_*.json`, `usage_summary.json` |
| Offline replay | Recompute with the current code from frozen real-device extraction candidates or archived view trees; no network, no phone; cannot be written as measured |
| Not executed | Device disconnected or corpus missing; no quotable numbers at present |

### 6.2 Measured on device (2026-11-22, `adb_dump`, same task, same group)

| Metric | `MERGED_OBSERVE=0` | `MERGED_OBSERVE=1` |
| --- | --- | --- |
| Total task time | 21.03 s | 11.45 s |
| Request count | 5 | 1 |
| dump count | 2 | 3 |
| Action sequence | `wait→tap→finish` | `open_group→finish` |

The two sequences differ (model navigation vs rule navigation), so this cannot be treated as a pure speed-up from merged observation. With rules turned off, 0/1 ran 3 times each with identical sequences: 21.27 s vs 17.81 s (the first two; the third timed out at 56.3 s), and merged observation is about 16% faster. Separate comparison of the rule switch: forced rules 11.44 s / 1 request vs off 17.81 s / 3 requests; a rule step of 5.5 s ≈ a model step of 5.3 s, but with one fewer request, so it is enabled by default under adb_dump.

Baseline 235deea's 20.63 s vs 71.07 s was a strategy difference caused by 6 fixed scrolls, since fixed by "scroll limit 2 + stop on answer hit + light observation"; at the time it could not be attributed to merged observation.

### 6.3 Offline replay

- Judging rerun: **21/21 = 100%** (hit 21, partial 0, fail 0, violation 0); D02/D03 data was not collected, marked "not evaluable" and excluded from the denominator.
- Basis: 21/21 is an offline rerun on 2026-11-24 at `main@411c48e`, byte-identical to the 2026-11-22 original artifact `judge_replay_result.json` (SHA256 `F4894F17E65154BD43CE861A40A74D38C5924C6BDAA62F172C94EAA053F8E672`). The earlier `20/21 = 95.24%` was an old snapshot from before D06 applied pinned filtering, and is deprecated.
- D09 criterion fixed: 「月底前」 does not guess a date; `status=uncertain` and `deadline_iso=null` count as hit.
- D15: after offline fixed planner source selection (COMP7503课程群), replay downstream is judged hit; **source-selection ability itself can only be evaluated on a real device**.
- D18: when the explicit task has no match, output an empty result and mark `no_match`, no longer output unrelated `llm` records.
- Rule stop conditions: 0 scrolls when the archived chat page already has the answer keywords; opening a group from the group list and rejection in the send area both behave as expected.

### 6.4 Other real-device smoke tests

- Search vs list: same group 3 runs each, search 39.01 s / 7 dumps / 4 steps, list 11.48 s / 3 dumps / 2 steps; under adb_dump the search page dumps more slowly, so search is not the default (`RULE_SEARCH_FORCE=1` can force it).
- monitor single-process `run_cycle`: 7 steps / 51.2 s / 19,591 tokens, outputs `data.json`, `deadlines.ics`, `calendar.html`; the phone-calendar test event was written and then deleted, and .ics generates VEVENT + VALARM.
- Read-only protection: after opening the search input, send / press_enter / typing in the message input box are still rejected; the rule asserting the send-area tap is retained.

### 6.5 Not executed

- Real-device judging rerun after the fix (the current judging numbers still come from offline replay).

Reusing the same screen and connection warm-up are not counted as gains: in 6 archived A/B runs `reused_frame` was always 0; reuse saves at most `preprocess` 0.11–0.15 s/step; connection warm-up 0.22–0.43 s happens only once at startup, so the per-step gain is 0.00 s.
