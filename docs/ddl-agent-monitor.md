# DDL Periodic Retrieval Guide

> The runtime environment is a PC and the operation target is a phone: the PC drives the phone over ADB, and the WeChat / QQ / Outlook App interfaces are all read on the phone.
> No component of this project is installed on the phone, and root is not required. Phone screen reading and target location are **not verified with real data**.

## 1. One-time configuration

### 1.1 Add retrieval targets in natural language

```bash
python -m ddl_agent.monitor.cli add "定期读取微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次，最近7天"
python -m ddl_agent.monitor.cli list
python -m ddl_agent.monitor.cli edit wechat-21级课程群 --target "22级课程群" --lookback-days 7
```

- `微信` / `QQ` / `Outlook` are supported; Outlook reads the inbox by default.
- Target names are best marked with 「」 or quotes; when several Apps are mentioned at once, each target must be marked separately.
- Schedules support 「每 6 小时」/「每天一次」/「每周」/「每 30 分钟」, with a minimum of 5 minutes.
- Input that cannot be parsed or is too vague (e.g. 「所有群」/「帮我盯一下」) is **rejected with the reason**, never guessed.
- Every source stores the original sentence (`nl_text`) and the parse note (`parse_note`); `list` shows “来自：……”.

### 1.2 Manual editing

The configuration is stored in `sources.yaml`; see `sources.example.yaml` for the format. Fields:

| Field | Description |
|---|---|
| `id` | Unique identifier, generated automatically by the CLI |
| `app` | `wechat` / `qq` / `outlook` |
| `kind` | `group` / `private` / `inbox` |
| `target` | Group name, contact, or inbox to locate inside the App |
| `source_type` | `official_email` / `student_chat` etc., used for credibility ordering in conflict resolution |
| `enabled` | Whether the source participates in this cycle |
| `lookback_days` | Only read messages from the last N days |

For a QQ single-group smoke test, copy `sources.qq.example.yaml` to `sources.qq.yaml` directly: it contains only one source, 「2026 计算机班级群」, with independent `output_dir` / `state_dir`, so it does not affect the default `sources.yaml`. The minimal D04 configuration and single-cycle usage are described in the comment at the top of that file.

## 2. Running

### 2.1 Double-click launch (recommended)

Double-click `run_ddl_monitor.bat` in the repository root:

1. Check whether adb is available;
2. Check whether the phone is online and authorized (USB debugging);
3. Check whether the device lock is held by another window;
4. Ask 「1 仅扫描一次 / 2 定期」;
5. After the run, refresh and open `output/calendar.html`.

It can also be run with arguments:

```bat
run_ddl_monitor.bat --mode once
run_ddl_monitor.bat --mode loop --config sources.yaml --engine rules
```

Common problems and the next steps printed by the script:

| Symptom | Message and next step |
|---|---|
| adb not found | Install Android Platform Tools and add it to PATH, or set `ADB_BINARY` |
| No device | Connect USB, unlock the screen, enable 「开发者选项 → USB 调试」 |
| `unauthorized` | Tap 「允许」 in the phone popup; if no popup appears, revoke authorization and reconnect |
| `offline` | Reconnect, or run `adb kill-server` and retry |
| Multiple devices | Keep only one phone/emulator |
| Device lock held | Close other `run.py` / `app.py` / scanning windows |

### 2.2 Long-running CLI

```bash
# Run a single cycle
python -m ddl_agent.monitor.cli run --once

# Stay resident on the schedule in sources.yaml; the config is re-read every cycle, so adding/removing sources needs no restart
python -m ddl_agent.monitor.cli run
```

## 3. Incremental retrieval and cross-cycle deduplication

The state directory defaults to `state/`:

| File | Content |
|---|---|
| `cursors.json` | Per-source `last_timestamp`, `last_message_id`, `baseline_done` |
| `messages.jsonl` | Raw messages already fetched, deduplicated by `message_id` |
| `candidates.jsonl` | Extracted candidates, kept across cycles for conflict resolution |

- The first run performs a baseline scan (bounded by `lookback_days`); later cycles only extract new messages.
- A source whose fetch fails does not advance its cursor, is retried in the next cycle, and records `error` in that cycle's result.
- Reset cursors (the next run performs a baseline scan again):

  ```bash
  python -m ddl_agent.monitor.cli reset-cursor
  python -m ddl_agent.monitor.cli reset-cursor --source wechat-21级课程群
  ```

- Records already written to the document are matched by `(course, normalized task name)`: update if the time changed, skip if nothing changed.
  Each cycle writes `output/cycles/<cycle_id>.json` with `inserted / updated / skipped`; the audit log is at
  `output/deadlines.xlsx.audit.csv`.

## 4. Output

| File | Purpose |
|---|---|
| `output/deadlines.xlsx` | Main document, containing Deadlines and Audit Log |
| `output/data.json` | Flat data for the web page / secondary development |
| `output/deadlines.ics` | Import into the system calendar; confirmed records carry a reminder 1 day in advance |
| `output/calendar.html` | Self-contained calendar page, opened by double-click; auto-reloads every 2 minutes |
| `output/cycles/*.json` | Per-cycle summary of sources, increments, writes and LLM usage |

Writing to the phone's system calendar is disabled by the read-only constraint (`PhoneCalendarWriter` always skips; even with `write_phone_calendar=true` it never touches the phone).

## 5. Verification boundaries

- Offline test coverage: natural-language configuration, explicit rejection, cursor baseline/increment/reset, cross-cycle insert→skip→update,
  error when the target is not found without advancing the cursor, `data.json` / `.ics` / `calendar.html` export, launcher device-state messages.
- **Not verified with real data**: on-device screen reading, locating by name inside an App, Outlook App inbox,
  real WeChat/QQ/Outlook message formats.
