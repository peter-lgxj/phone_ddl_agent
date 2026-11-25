# Real-Device Reading and End-to-End Extraction Verification Report

> Verification dates: 2026-11-18 (old record); 2026-11-23 (three-group rerun, see 4.4)
> Branch: `fix/p0-run-blockers` (2026-11-18); `1e38364` (2026-11-23 rerun)
> Device: OPPO PLJ110 (real device, USB + ADB)
> Criteria: content marked "measured on device" comes from device screenshots, `step_*.json`, `task_log.json` and extraction artifacts;
> content marked "inferred/estimated" does not come from logs and must be understood as explained in the text.

> **The two batches of records must not be merged**:
> - 2026-11-18 WeChat 「文件传输助手」 self-chat, 3 records = **1 confirmed / 2 uncertain** (4.2, 4.3).
> - 2026-11-23 three real groups, 3 records each from demo and launcher = **2 confirmed / 1 uncertain** (4.4).
> The messages in the two batches differ; the status distributions cannot be substituted for or added to each other.

## 1. Device and environment

| Item | Value | Source |
| --- | --- | --- |
| Model | OPPO PLJ110 | `adb devices -l` (measured on device) |
| Android version | 17 (SDK 37) | `getprop` (measured on device) |
| Screen | 1080×2374, physical density 560, override 480 | `wm size` / `wm density` (measured on device) |
| Target App | WeChat, conversation 「文件传输助手」 | screenshots and `topResumedActivity` (measured on device) |
| WeChat version | **not obtained** | after verification `adb devices` showed no device, so `dumpsys package` could not be read; no inferred value was written |
| Screen-reading model | `deepseek-flash` | ScreenAgent startup log (measured on device) |
| Extraction engine | `engine=llm` (DeepSeek) | `extraction_result.json` (measured on device) |

> Version correction: the device is actually **Android 17 (API 37)**. `uiautomator2` 3.7.1 on Android 17
> reports `ApplicationSharedMemory not initialized` on the first `deviceInfo` RPC
> (openatx/uiautomator2#1209), so real-device reading on this device uses the `SCREEN_DRIVER=adb_dump` fallback driver;
> this is an Android 17 device record, not Android 16. The repository has no test record for another Android 16 real device.

## 2. Timeline

| Stage | Time | Run directory | Result |
| --- | --- | --- | --- |
| First round (before fix) | 2026-11-18 13:19:36 | local evidence cleaned up | 10 steps all swipe, `timeout`, **0 messages**, 81.42 s |
| First run after fix | about 13:26 | local evidence cleaned up | device-side read succeeded; the test script errored while saving JSON because `datetime` was not serializable, so the result was not written to disk |
| Rerun after fix | 2026-11-18 13:28:27 | local evidence cleaned up | 10 messages, `timeout` + `partial=true`, 105.59 s |
| End-to-end extraction | 2026-11-18 13:47:38 | local evidence cleaned up | 3 steps, `done`, target message read; LLM extracted 3 records, target record time correct |

The root causes of the first round were three defects:

1. `screen_agent._observe` required only "a one-sentence description of the interface" for swipe/scroll, not the message body.
2. Scroll observation did not trigger OCR (`ocr_triggered=false`); when the model's summary lost content, there was no text fallback.
3. `MobileChatConnector.fetch` threw directly on `timeout`, discarding already-archived observations.

After the fix: scroll observation requires listing messages one by one and overlays OCR text; `timeout` returns partial observations marked `partial=true`, `last_status=timeout`.

## 3. Key Numbers Comparison

| Metric | First round (before fix) | After fix (rerun) | End-to-end extraction |
| --- | --- | --- | --- |
| Returned messages | 0 | **10** | 3 (2 observations + 1 `任务完成`) |
| Total observation characters | 216 | **1751** | 1738 |
| Total time | 81.42 s | 105.59 s | 28.91 s (reading) + 3.35 s (extraction) |
| Average per step | 8.14 s | 10.56 s | 9.64 s |
| Total OCR cost | 0 | **26.27 s** | 5.22 s |
| Extraction candidates | 0 | 0 | 3 records |
| End status | `timeout`, result discarded | `timeout`, `partial=true` | `done`, `partial=false` |

### 3.1 First-Round Per-Step Timings (measured on device, unit s)

| Step | Action | Decision | Execution | Observation | OCR |
| --- | --- | --- | --- | --- | --- |
| 1 | swipe | 3.71 | 1.96 | 2.95 | — |
| 2 | swipe | 2.99 | 1.94 | 3.03 | — |
| 3 | swipe | 2.55 | 1.93 | 2.35 | — |
| 4 | swipe | 2.59 | 1.90 | 2.63 | — |
| 5 | swipe | 3.13 | 1.97 | 2.34 | — |
| 6 | swipe | 2.23 | 1.91 | 2.61 | — |
| 7 | swipe | 3.10 | 1.97 | 2.46 | — |
| 8 | swipe | 2.53 | 1.94 | 2.67 | — |
| 9 | swipe | 2.74 | 1.96 | 2.64 | — |
| 10 | swipe | 2.54 | 1.94 | 3.05 | — |

### 3.2 Post-Fix Per-Step Timings (measured on device, unit s)

| Step | Action | Decision | Execution | Observation | OCR |
| --- | --- | --- | --- | --- | --- |
| 1 | swipe | 2.36 | 1.98 | 11.79 | 7.33 |
| 2 | swipe | 2.49 | 1.93 | 7.55 | 4.70 |
| 3 | swipe | 2.41 | 1.94 | 5.00 | 2.19 |
| 4 | swipe | 2.27 | 1.93 | 7.01 | 3.48 |
| 5 | swipe | 2.38 | 1.93 | 5.27 | 2.32 |
| 6 | swipe | 2.41 | 1.93 | 4.54 | 1.50 |
| 7 | swipe | 2.07 | 1.96 | 3.52 | 1.23 |
| 8 | swipe | 2.21 | 1.94 | 3.74 | 1.45 |
| 9 | swipe | 2.73 | 1.94 | 3.52 | 0.90 |
| 10 | swipe | 2.17 | 1.95 | 3.63 | 1.17 |

Step 1 OCR includes the model's first load, 7.33 s; afterwards it stabilizes at 0.90–4.70 s.

### 3.3 End-to-End Per-Step Timings (measured on device, unit s)

| Step | Action | Decision | Execution | Observation | OCR |
| --- | --- | --- | --- | --- | --- |
| 1 | swipe | 2.88 | 1.96 | 8.19 | 4.25 |
| 2 | swipe | 2.47 | 1.95 | 5.10 | 0.97 |
| 3 | finish | 3.38 | 0.00 | — | — |

### 3.4 LLM Calls and Cost

| Stage | Calls | prompt token | completion token | Total token | Cost USD | Time |
| --- | --- | --- | --- | --- | --- | --- |
| Extraction chain (planning + extraction) | 2 | 1043 | 379 | 1422 | 0.000699 | 3.35 s |
| Screen-reading vision model | 5 (inferred) | not counted | not counted | not counted | not counted | included in reading time |

- Extraction-chain token/cost comes from `UsageTracker` (measured on device).
- Screen-reading calls are inferred from 3 decisions + 2 observations; the vision model is not connected to token statistics, so **token and cost cannot be given**.

## 4. Original Text and Extraction Result Comparison

### 4.1 Original Text Actually Read

VLM transcription (measured on device):

> 机器学习作业三，11.29 晚上 23.59 交到 Moodle

OCR transcription (measured on device):

> 机器学习作业三，11.29晚上23.59
> 交到 Moodle

The difference is only spaces and line breaks; the text content is identical. The sender is the user themselves (green bubble), time 13:39.

The same screen also has two old messages (`llm 的 ddl 是 11.31`, `多媒体 project 的截止日期是 10 月 21`), and below at 13:43 is a course-timetable image (COMP7415 / COMP7503); the image itself contains no deadline.

### 4.2 Target Record

| Field | Value | Judgement |
| --- | --- | --- |
| Task name | 机器学习作业三 | correct |
| Course | `null` | the original text gives no course code; it was not forced from the adjacent image; leaving it empty is a reasonable choice, but manual completion is needed |
| Deadline | `2026-11-29T23:59:00+08:00` | correct |
| Status | `confirmed` | the time is explicit, reasonable |
| Confidence | 0.95 | — |
| Needs review | time no; **course field yes** | manually complete the course attribution |
| Source | `wechat-filehelper (student_chat)` | — |
| Evidence | `wechat-filehelper-step-002:llm3` | corresponds to the step 2 observation |

**Consistency checks (measured on device + inferred)**

| Check | Result |
| --- | --- |
| `11.29` → `11-29` | consistent |
| `23.59` → `23:59` | consistent |
| Year | the original text has no year; inferred as 2026 from the current date (**inference, not a fact from the original text**) |
| Time zone | `+08:00` (Asia/Shanghai), no offset |
| Whether the date is out of range/wrong day | not found |

### 4.3 Other Records on the Same Screen

| Task name | Deadline | Status | Confidence | Notes |
| --- | --- | --- | --- | --- |
| llm 的 ddl | `null` | `uncertain` | 0.4 | the original `11.31` is not a valid date; no time was fabricated, marked for manual review |
| 多媒体 project | `null` | `uncertain` | 0.4 | the original only says `10 月 21`; the model associated `COMP7503` from the timetable on the same screen, which is over-association |

These two are not the target message of this run; they are kept to show that the extractor processes adjacent messages in the same observation and to expose the over-association problem.

### 4.4 2026-11-23 Three-Group Rerun (demo and launcher)

Rerun on main commit `1e38364` (before PR #27), read-only; artifacts in `runs/_verify/live2_20261123/`. Before starting, adb was offline once and recovered after one `adb reconnect`.

| Run | Exit code | Duration | Sources and fetch counts | Records |
| --- | --- | --- | --- | --- |
| `demo --live` | 0 | 329.1 s | COMP7503 作业通知群 42 / COMP7415 课程群 137 / 2026 计算机班级群 192; candidates 18 / 0 / 14 | 3 records: **2 confirmed / 1 uncertain** |
| launcher single run | 0 | 289.4 s | 3 sources succeeded; fetched 29 / 132 / 192 | 3 records: **2 confirmed / 1 uncertain** |

The 3 records from the two artifacts are identical one by one:

| Task | Course | Deadline | Status | Confidence | Basis (`note` in `data.json`) |
| --- | --- | --- | --- | --- | --- |
| 操作系统作业一 | COMP7503 | `2026-11-26T23:59:00+08:00` | `confirmed` | 0.95 | same-source correction/extension, taking the latest effective value; the old value 11.28 is kept in `superseded_by` and not output side by side |
| 机器学习作业三 | COMP7503 | `null` | `uncertain` | 0.40 | extraction confidence insufficient, marked `uncertain` for manual review |
| 实验报告二 | `null` | `2026-11-22T23:59:00+08:00` | `confirmed` | 0.95 | all sources consistent |

- Artifacts: `runs/_verify/live2_20261123/demo_out/{data.json,deadlines.ics,calendar.html}`, `launcher_out/{data.json,deadlines.ics,calendar.html}`, `summary.json`.
- `write_phone_calendar=false`; this round did not write the phone calendar.
- Relationship with 4.2/4.3: 4.2/4.3 are the old 2026-11-18 self-chat records (1 confirmed / 2 uncertain); 4.4 is the 2026-11-23 three-group rerun (2 confirmed / 1 uncertain). The two batches of messages differ and **must not be merged or have one number overwrite the other**.

**QQ scroll-up recovery correction**: `item3_qq_navigation.scroll_up_recovery_passed` in `summary.json` is `false`. In that run, after turning upward at steps 3–6, step 7 misjudged "scrolled up to the top" and gave up; the vision model then changed to scrolling down and tapped 「展示最近不活跃的群(13)」, ultimately not returning to the target group (the single-run directory has been cleaned up; the probe evidence is retained at `runs/_verify/live_20261123/qq_scrollup_probe.json`). This failure occurred before PR #27; PR #27 changed to judging the top twice consecutively via frame hash/OCR text-set and locking the direction, but post-fix real-device rerun evidence is not in this artifact directory, so this round cannot be recorded as #27 verified.

## 5. Problems Found

1. **Over-association**: `多媒体 project` was filled with `COMP7503`. The only basis was the timetable image lower on the same screen; the original text did not establish the relation "多媒体 project = COMP7503". That record is marked `uncertain`, but the field may still mislead, and a review should clear it or require explicit evidence.
   - A later fix added a hard evidence check: the course name and deadline must be found as a string or an equivalent normalized form in that message's own body, otherwise they are cleared and marked `needs_review`. This report keeps the actual output at the time for comparison with pre-fix behavior.
2. **Invalid date handled correctly**: `11.31` has no corresponding date; the extraction result keeps `deadline_iso=null`, `status=uncertain`, and does not silently rewrite it to `11.30` or `12.01`.
3. **Serialization problem in the test script**: in the first run after the fix (13:26), the device side had already read messages, but `datetime` could not be serialized when saving JSON, so the summary file was not generated. After changing the script to `json.dumps(..., default=str)` and rerunning, it worked. This problem belongs to the verification script, not the product path; the rerun results are in 3.2.
4. **OCR noise**: OCR brings in system text such as the status-bar time, battery and title bar; the prompt currently asks to ignore it, but it is still present in the text. The extractor does not treat it as a DDL, which is acceptable noise, but it increases tokens.
5. **Multiple messages on one screen**: one observation may contain several historical messages, and the extractor processes all of them; the target record this time was correct, but adjacent messages increase the surface for mis-extraction.

## 6. Read-Only and Safety Constraints

| Constraint | Execution |
| --- | --- |
| Do not send messages | not sent; the actual actions in the three runs were only swipe and finish |
| Do not type chat content | `type` is blocked by the interceptor; not triggered in practice |
| Do not tap send/Enter | `send` / `press_enter` are blocked by the interceptor; not triggered in practice |
| Do not modify contacts | never entered the contact-edit page |
| Do not write the real calendar/formal xlsx | only wrote to a local temporary output directory (not committed) |
| Device lock | acquired through `device_lock` each time; the check at the end of the run was `LOCK_RELEASED` |
| Plaintext keys | not written into any report or artifact |
| Privacy incident | the first WeChat launch landed directly on the private-chat page between the user and a contact; that screenshot was deleted (recycle bin) and was not read, extracted, or written into the report. Afterwards the flow was changed to return to the chat list first and then search 「文件传输助手」 |

## 7. Verification Boundaries (explicitly unverified)

First distinguish "capability exists" from "verified on a real device":

| Capability | Code/unit-test status | Real-device verification status |
| --- | --- | --- |
| WeChat phone screen reading | exists; unit tests cover parsing and partial results on timeout | 2026-11-18 self-chat 1 run; 2026-11-23 three groups, demo/launcher 1 run each |
| QQ phone screen reading | package-name mapping and the generic screen-reading path exist | only one scroll-up recovery probe, which failed (before PR #27); the rest unverified |
| Outlook phone screen reading | package-name mapping and the generic screen-reading path exist | **unverified** |
| monitor single-process screen reading→extraction | code and unit tests exist | launcher single run passed (3 groups, 3 records); the monitor-specific path was not verified separately |
| Phone system calendar writing | disabled by the read-only constraint | **not provided** (`PhoneCalendarWriter` always skips) |

- **Single device, single system**: only one OPPO PLJ110 / Android 17 (`adb_dump` fallback) was tested; there is no second device or OS version for comparison.
- **Single account, two batches of scenarios**: 2026-11-18 tested only one WeChat account's 「文件传输助手」 self-chat; 2026-11-23 tested 3 real groups of the same account (COMP7503 作业通知群, COMP7415 课程群, 2026 计算机班级群); third-party private chats were not tested.
- **QQ only one failed probe**: the 2026-11-23 QQ scroll-up recovery sub-item ran before PR #27 with `scroll_up_recovery_passed=false`; there is no real-device rerun evidence after #27. Outlook was not tested.
- **WeChat version unknown**: the device disconnected after verification, so `versionName` could not be read.
- **Image-type DDL not tested**: there is a timetable image on the same screen, but the image contains no deadline; whether a deadline that exists only in an image can be extracted was not verified.
- **Navigation is not done autonomously by the Agent**: WeChat was opened manually via ADB, and the target conversation was located by search/paste; ScreenAgent is responsible for scrolling to the latest, screen reading and OCR. The Agent autonomously searching for and entering a conversation was not verified.
- **Reading and extraction run as two steps**: first `read_latest.py` persists the real-device observation, then `extract_e2e.py` feeds it to `DdlAgent` through `CapturedSource`; it is not a single process from device straight to writer. The pipeline data is real real-device data, but the processes are separate.
- **Single run**: timings and tokens are single-run results, not repeated 3 times and averaged, and cannot be treated as stable performance metrics.
- **Vision-model tokens not counted**: only the 2 calls of the extraction chain were counted; the token/cost of the 5 screen-reading calls is unknown.
- **Real calendar not written**: `.ics` and phone system calendar writing were not verified in this round.

## 8. Evidence Paths

The evidence directories are under local `runs/` and are ignored by `.gitignore` (`runs/`), so they are not committed with the repository:

| Content | Path |
| --- | --- |
| 2026-11-23 demo artifacts | `runs/_verify/live2_20261123/demo_out/{data.json,deadlines.ics,calendar.html}` |
| 2026-11-23 launcher artifacts | `runs/_verify/live2_20261123/launcher_out/{data.json,deadlines.ics,calendar.html}` |
| 2026-11-23 summary and QQ sub-item | `runs/_verify/live2_20261123/summary.json`, `runs/_verify/live_20261123/qq_scrollup_probe.json` |
| 2026-11-24 read-only re-verification of this round | `runs/verify2_20261124/` (screenshots, OCR, skipped dump, read-only rejection, not-located failure, cleanup policy) |

The step-by-step screenshots, `read_result.json`, `extraction_result.json` and other intermediate artifacts of the 2026-11-18 first round and end-to-end runs have been cleaned up locally (`runs/` is not committed); the conclusions corresponding to the table above remain in the body text.

## 9. Conclusion

- After the fix, screen text can enter the observation and the extraction pipeline: messages 0 → 10, and the target message is correctly extracted.
- 2026-11-23 three-group rerun: both demo and launcher produce 3 records (**2 confirmed / 1 uncertain**), and the two details agree; this is a different batch of messages from the 2026-11-18 self-chat (1 confirmed / 2 uncertain).
- The 2026-11-23 QQ scroll-up recovery sub-item failed (`scroll_up_recovery_passed=false`, before PR #27) and cannot be recorded as that capability verified.
- The target record time `2026-11-29T23:59:00+08:00` matches the original text with no time-zone offset; the course field is empty and needs manual completion.
- The over-association problem still exists (`COMP7503` associated with `多媒体 project`); it is currently exposed via `uncertain` and cannot be regarded as a reliable result.
- Coverage remains limited: single device, single account; real group chats cover only 3 configured groups; post-fix QQ, Outlook, image-type DDL and stability statistics are all unverified.
