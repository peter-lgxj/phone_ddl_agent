# Phone Agent

Understands the Android phone screen with a vision-language model and performs actions through ADB and uiautomator2; supports task planning, multi-application tasks, OCR-based localization and a web interface.

## Submission Information

- Project: Phone Agent
- Group / Title: Group 54, An Agentic Cross-Application DDL Integration System
- Members: Xiao Shutian (3036795663), Huang Xiaoyu (3036807686), Ye Xiaofan (3036795912)
- Submitted version: `main` HEAD; the audit baseline cited in the report is `main@375700bb` (2026-11-24)

## Operating Model: the Computer Drives the Phone

- **The phone is the object being operated on**. The WeChat (group chats / direct chats), QQ (group chats) and Outlook App inbox interfaces are all read and tapped **on the phone**.
  The agent never opens the WeChat/QQ/Outlook desktop clients on the computer, and never reads local mail on the computer
  (local `.eml` files / chat archives are only offline test stand-ins used when no phone is available).
- **The runtime environment is the computer**. This project runs on the computer and drives the phone through **ADB (USB cable or WiFi debugging)**.
  **No component of this project is installed on the phone**, and **root is not required**. On first use, uiautomator2 may install an automation
  helper component by itself; that is uiautomator2's existing mechanism, not a deployment of this project.
- In one sentence: **the agent on the computer operates the phone GUI through ADB**, aggregating the group-chat and email DDLs on the phone into a calendar.

> Phone screen reading has been verified read-only on a single physical device: the WeChat "File Transfer Assistant" (「文件传输助手」) self-chat (2026-11-18) and
> a once-only scan of the QQ "COMP7503 作业通知群" test group (2026-11-24, `cycle 20261124T163807`, status=ok).
> Real WeChat course groups, Outlook, image-based DDLs, the scheduled mode and real experimental data have still not been verified.
>
> Outlook **goes through phone App screen reading only**; Microsoft Graph is not integrated. The Graph-related code is a retained but disabled skeleton,
> not a second, switchable read path.

## One-Click Launch (Windows)

Web entry point: run `python app.py` and open `http://127.0.0.1:5000/ddl`. In the page you can add WeChat / QQ / Outlook phone sources in natural language,
start a scan and view the calendar directly; the home page also has a "DDL calendar" entry.
A scan occupies the phone, and the API is by default only callable from the local machine; to access it from the LAN, set `DDL_WEB_TOKEN`
and send the same value in the `X-DDL-Token` request header. Real device mutual exclusion is guaranteed by the device lock held by the scanning thread,
so a "pre-check passed but then preempted by another window" situation cannot occur.

To present the web page on a machine without a phone, run `python scripts/demo.py --offline --out output` first: the page and its calendar panel read `output/calendar.html`, so the DDL calendar can be shown without a device. A scan started from the page itself still requires an authorized phone.

To present the web page on a machine without a phone, run `python scripts/demo.py --offline --out output` first: the page and its calendar panel read `output/calendar.html`, so the DDL calendar can be shown without a device. A scan started from the page itself still requires an authorized phone.

1. On first use, first tell the agent what to retrieve (natural language, written into `sources.yaml`):

   ```bash
   python -m ddl_agent.monitor.cli add "定期读取微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次"
   python -m ddl_agent.monitor.cli list
   ```

   (The quoted argument is a natural-language instruction meaning "periodically read the WeChat 「21级课程群」 group and the QQ 「机器学习课程群」 group, once every 6 hours"; it is passed to the CLI verbatim.)

2. Double-click `run_ddl_monitor.bat` in the repository root. The script checks adb, whether the phone is online and authorized, and the device lock
   in turn, then asks "1. 仅扫描一次 / 2. 定期" (1. scan once / 2. scan periodically); when it finishes it refreshes automatically and opens `output/calendar.html`.

3. Artifacts: `output/deadlines.xlsx` (including an audit sheet), `data.json`, `deadlines.ics` (importable into a system calendar), `calendar.html`. The `DTSTART` of `deadlines.ics` uses local time with `TZID=Asia/Shanghai` (`DTSTAMP` remains UTC); `calendar.html` is a static calendar view that only places items by deadline and shows the time — it has no countdown and no red/orange/grey status.

If the phone is not plugged in, USB debugging is not authorized, adb cannot be found, or the phone is occupied by another window (device lock), the script gives a corresponding hint and next action for each case
and does not throw a Python traceback. See [docs/ddl-agent-monitor.md](docs/ddl-agent-monitor.md) for details.

## Demo (One Command)

The demo takes a single command and requires no reading of the code.

```bash
python scripts/demo.py
```

The default is `auto`: if one authorized phone is detected it performs a read-only scan on the real device; without a phone it automatically replays the full pipeline
from the archives in `examples/demo/`. You can also choose explicitly:

```bash
python scripts/demo.py --offline      # no phone/API key needed, deterministic replay
python scripts/demo.py --offline --demo-config examples/demo/sources.json --engine rules
python scripts/demo.py --live         # force the real device, read sources.yaml
python scripts/demo.py --live --sources my_sources.yaml --out demo_output
```

**Prerequisites**

- Offline: install only the Python dependencies (`requirements.txt` / `requirements-dev.txt`).
- Real device: USB debugging enabled on the phone and authorized with the computer; `adb devices` can see the device;
  WeChat / QQ / Outlook sources configured in `sources.yaml` (copy and modify `sources.example.yaml`).

**What You Will See**

- A human-readable summary: how many DDLs in total, each task's name/deadline/status/source, and the basis for same-source postponement overrides or cross-source conflict resolution.
- Artifacts: `demo_output/data.json`, `deadlines.ics`, `calendar.html` (in real-device mode it also writes `deadlines.xlsx`).
- Read-only guarantee: the demo does not type into a chat box, does not tap send and does not send messages; the only things that change the screen are opening an App, scrolling and tapping into the target conversation.

**What to Do If It Fails**

- No phone / not authorized: it shows the next action for `adb devices` and automatically switches to offline replay (add `--no-fallback` to require a hard failure).
- No API key: the demo can still be completed with the rule engine; to demo LLM extraction, configure `DEEPSEEK_API_KEY` in `.env` and add `--engine llm`.
- Device occupied: it reports which process holds `runs/.device.lock`; close the Web/experiment process and retry, or just use `--offline`.
- No `sources.yaml`: it hints to copy `sources.example.yaml` and then modify the group names / inbox.

**How to Use Offline Replay**

`examples/demo/sources.json` is the demo configuration; the `path` of each entry in `sources` points to a local chat archive JSON.
Replace it with your own archive (format documented at the top of `ddl_agent/connectors/local_chat.py`), or point `--demo-config`
at a new file; no code changes and no phone are needed.

## Offline Evaluation and Ablation

```bash
# Three-way comparison: Agent-LLM / Agent-rules / single-shot baseline
python -m ddl_agent.evaluation.run_eval --split test

# RQ4 single-module ablation: all on / dedupe off / conflict validation off / fixed sources
python -m ddl_agent.evaluation.run_eval --ablation --split test \
  --out experiments/ddl_agent/results/ablation_distractors2

# Three semantic matching strategies: lexical / local embedding / combined
python -m ddl_agent.evaluation.run_eval --similarity-ablation --split test \
  --out experiments/ddl_agent/results/similarity_test4
```

Every sample in the dataset carries distractor sources unrelated to the query (internship referral groups, family groups, club activity groups, other course groups)
to test dynamic source selection; these messages never enter the ground truth.

Embedding is an **optional** capability: by default the evaluation runs without it installed and without network access.

```bash
# Install optional dependencies (not part of the main requirements / CI)
pip install -r requirements-embedding.txt

# Model and framework caches live in the user directory by default (~/.cache/ddl-agent/sentence-transformers); they can be set explicitly
set DDL_EMBEDDING_CACHE=%USERPROFILE%\.cache\ddl-agent\sentence-transformers
set DDL_EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2
```

The first run downloads the multilingual small model; for an **offline environment**, copy the whole cache directory to the target machine, or point `DDL_EMBEDDING_CACHE`
at an existing directory. If the model is missing, the download fails or loading raises an error, the evaluation automatically falls back to pure lexical matching, the report's
`similarity.available` is `false` and a `fallback_reason` is given; the run is never aborted.
Real-model test cases are skipped by default; set `RUN_EMBEDDING_TESTS=1` to run them.

Per-proposal compliance, the RQ4 ablation and the before/after numbers of the three similarity strategies are in
[docs/proposal-compliance.md](docs/proposal-compliance.md).
All numbers come from synthetic data; on a real device only a read-only read of a single-device, single-account self-chat session has been verified
(`docs/real-device-test-report.md`); real group chats, QQ / Outlook and user studies have not been verified.

## Environment and Installation

- Python 3.10 or later (exact compatibility depends on dependency versions)
- An Android phone with developer options and USB debugging enabled
- Android Platform Tools (`adb` on PATH; if it is not, point `ADB_BINARY` at the adb executable, or reuse the adb bundled with `uiautomator2` — `adb_utils.py` falls back automatically)
- A working model API key. DeepSeek / DashScope are both **billed per use**; without a key you can use the rule engine and the offline demo, but the LLM path is unavailable

```bash
git clone https://github.com/peter-lgxj/phone_ddl_agent.git
cd phone_ddl_agent
# Exactly the same as CI: runtime and test dependencies are both installed from this single lock file
pip install -r requirements-lock.txt
```

`requirements-lock.txt` is the complete version list (including transitive dependencies) actually resolved for this project on Windows + Python 3.12, and is the **single source of truth for both CI and local installation**; `requirements-dev.txt` only keeps a compatibility entry point `-r requirements-lock.txt`. On a lab machine or GPU environment, re-run `pip freeze` and replace the lock.

> When adding test dependencies, change only `requirements-lock.txt`; do not add hand-written pip lists to `ci.yml` again. See [Development and Testing](#development-and-testing).

`requirements.txt` only maintains the minimum versions of runtime dependencies, for scenarios where you need to resolve the environment yourself.

Use `adb devices` to confirm that the phone is connected and authorized. The default execution model and planning model are both `deepseek` (`deepseek-flash`, reads `DEEPSEEK_API_KEY`); the execution model can be switched back to `qwen` (`qwen-vl-max`, reads `DASHSCOPE_API_KEY`). Other model and endpoint settings are in `config.py`.

### Credential Configuration (Never Commit Keys to the Repository)

1. Copy the template: `copy .env.example .env` in PowerShell, `cp .env.example .env` in bash.
2. Fill in at least `DEEPSEEK_API_KEY`; fill in `DASHSCOPE_API_KEY` when using Qwen.
3. `.env` and `.env.*` are ignored by `.gitignore`; before committing, use `git status` to confirm there is no `.env`.
4. `sources.yaml` is the local source configuration and is also not tracked; copy and modify it from `sources.example.yaml`.
5. Only the placeholder values of `.env.example` are kept in the repository; never write real keys.

The first run uses RapidOCR by default and downloads the corresponding ONNX model (network required, small in size); EasyOCR and its models are used only when `OCR_ENGINE=easyocr` is set explicitly. Screenshot localization also depends on the automation component that uiautomator2 installs on the phone.

`torch` / `ultralytics` are real dependencies of the YOLO input-box fallback (`input_detector.py`): the model is loaded only when the control tree is unavailable or no EditText can be found and the input box must be located; regular screenshots, OCR and control-tree reads never load it. They therefore cannot be removed, and CI installs these two packages (they are large, so the pip cache is kept).

`.env`, task run records and phone UI export files are not committed to the repository. Logs and the `runs/` evidence directory store group names, nicknames and message bodies for troubleshooting; at startup only automatic run directories of the form `YYYYMMDD-HHMMSS` under `runs/` are cleaned up, keeping the most recent 20 by default (adjustable via the `RUNS_KEEP` environment variable); manual directories such as `verify_*` and `wx_*`, directories whose names start with `keep`, and directories containing a `KEEP` file are never deleted.

## Android 17 (API 37) and the ADB Fallback Driver

### Current State and Criteria

On a physical OPPO PLJ110 / Android 17 / 1080×2374, `uiautomator2` 3.7.1 throws `ApplicationSharedMemory not initialized` on the very first
`d.info` RPC, so `ScreenAgent` cannot take screenshots or tap; the built-in `adb shell uiautomator dump` and `screencap` work normally.

- **Upgrade conclusion**: as of 2026-11-21, the latest version on PyPI is `uiautomator2==3.7.1` (2026-10-23);
  the upstream issue [openatx/uiautomator2#1209](https://github.com/openatx/uiautomator2/issues/1209)
  records that the error is unchanged after upgrading from 3.3.1 to 3.7.1, and is still open. **No released version currently fixes Android 17**.
- **Version floor**: `requirements.txt` says `uiautomator2>=3.7.1`. This is the measured baseline for non-Android-17 devices;
  on Android 17 it fails even though the floor is satisfied, and the fallback driver must be used.
- **Criteria**: `SCREEN_DRIVER=auto` (the default) first calls `u2.connect()` and reads `device.info`;
  a connection failure (no adb / not authorized) still returns the original connection error, and only "connected but RPC unavailable"
  (`ApplicationSharedMemory`, SDK 37 or a `device.info` exception) falls back to `adb_dump`.
  `SCREEN_DRIVER=uiautomator2` forces u2 and, when incompatible, gives an explicit error containing the issue link and the fallback command;
  `SCREEN_DRIVER=adb_dump` forces the fallback.

### Choosing Between the Two Drivers

| Capability | `uiautomator2` (full pipeline) | `adb_dump` (fallback) |
|---|---|---|
| Screenshot | `device.screenshot()` | `adb exec-out screencap -p` |
| Control tree | `device.dump_hierarchy()` | `adb shell uiautomator dump` + `cat` |
| tap / swipe | u2 `click` / `swipe` | `adb shell input tap` / `input swipe` |
| back / home | u2 `press` | `adb shell input keyevent 4/3` |
| Open App | u2 / monkey | `adb shell monkey -p <pkg>` |
| Current screen text | control tree | `text` / `content-desc` of the dumped XML |
| Input box | `EditText` selector | find `EditText` in the dump and tap its centre |
| type / send / press_enter | upper-layer read-only guard | **blocked again at the driver layer**: raises `PermissionError` in read-only mode |
| u2-only capabilities | XPath, `set_text`, persistent session | not supported; the current `ScreenAgent` does not depend on them |

```powershell
# Recommended: auto-detect, Android 17 falls back automatically
python run.py

# Force the fallback (known u2 failure, or for A/B)
$env:SCREEN_DRIVER="adb_dump"; python run.py

# Force u2 (for troubleshooting; on Android 17 this yields the explicit error)
$env:SCREEN_DRIVER="uiautomator2"; python run.py
```

When a fallback happens:

- `runs/<run>/step_NNN.json` writes `"driver": "adb_dump"` at the top level;
- at the end of the task, `usage_summary.json` writes `driver` / `driver_mode` / `full_pipeline` /
  `driver_note`, where `full_pipeline=false`, explicitly marking that **this run did not use the full uiautomator2 pipeline**;
- both the startup log and the end of the task print a fallback notice.

### A/B and Scoring Conventions in Fallback Mode

The fallback driver does not change `ScreenAgent`'s decision-making, observation, OCR, settle, evidence archiving or token accounting,
so `step_NNN.json` still contains `timings` / `usage` / `image` / `driver`, and
`experiments/summarize_timings.py`, `experiments/run_experiment.py` and `experiments/metrics.py`
all run as usual. If a step's frame hash equals the previous frame, `step_NNN.json` writes `reused_frame=true` at the top level and
`timings.reuse_reason="hash"`, and that step skips re-screenshotting and re-encoding; when settle keeps a stable frame but the picture has changed,
`reuse_reason="settle"`, `reused_frame=false`, and only the screenshot is saved, not the encoding.

- A run with `driver=adb_dump` **must not be mixed into the same set of means as a `uiautomator2` run**. When doing A/B, treat
  `driver` as a grouping variable, or give "fallback mode" its own column in the table.
- Fallback mode can provide: decision/observation/OCR/settle/execute stage timings, per-step tokens, action execution results and
  whether the task completed; it cannot provide metrics of u2-only paths (such as `dump_hierarchy` internal RPC latency, `set_text`).
- Offline unit tests replay dump XML and screenshots cut from `tests/fixtures/android17`,
  so CI needs no physical device; real-device verification requires connecting an Android 17 device yourself and running:

```powershell
adb devices
# Expected to fail on Android 17, with a readable error
python -c "import uiautomator2 as u2; print(u2.connect().info)"
# Fallback driver smoke test: screenshot, dump and tap are enough
$env:SCREEN_DRIVER="adb_dump"; python run.py
```

### Chinese Input and Control-Tree Limitations

- **Chinese input (`adb_dump`)**: `adb shell input text` reliably supports ASCII only. Non-ASCII text goes through
  ADBKeyboard's `ADB_INPUT_B64` broadcast instead (the body is base64-encoded after UTF-8 encoding, avoiding shell encoding problems),
  and after sending, the input box text is checked against the control tree to confirm it actually changed. Prerequisites:
  1. Install ADBKeyboard (`com.android.adbkeyboard`);
  2. Enable it and set it as the current IME:
     `adb shell ime enable com.android.adbkeyboard/.AdbIME`,
     `adb shell ime set com.android.adbkeyboard/.AdbIME`.
  If it is not installed/enabled or the check fails, the agent does not repeat the same input action: it records `blocked_reason`
  and suggests switching to list scrolling to locate the target; two consecutive ineffective inputs abort the task immediately.
- **Empty WeChat control tree**: for some WeChat versions/pages (especially WebViews and custom-drawn lists),
  `uiautomator dump` returns very few or no nodes. This is a platform limitation and no code workaround is attempted;
  screen reading falls back to screenshot + OCR, with the control tree used only as a supplement.
- **QQ group location order**: control-tree rules first — if the first screen contains an exactly matching group name, tap it directly (`open_group`);
  otherwise open the search box, type the group name and tap only an exactly matching result (`search_entry` / `search_result`);
  only when search is unavailable or returns nothing does it scroll the list by control-tree text (`list_scroll`, at most `QQ_LIST_MAX_SCROLLS`
  (default 8) screens, only scrolling, never tapping similar names). Only when the rules reach no conclusion does it fall back to exact matching by screenshot OCR
  (`ocr_list`), and if that still reaches no conclusion the vision model decides. OCR only accepts titles that exactly equal the group name or are
  "group name + a small unread count", so that `2026 计算机班级二群` is not mistaken for `2026 计算机班级群`.
- **QQ search page dump**: a real-device re-test showed `uiautomator dump` returns normally on the search page (about 360–374 nodes); the earlier empty tree was a transient failure
  caused by dump running concurrently with screenshot/OCR or during a page transition, not `could not get idle state`. Disabling animations (`animator_duration_scale` etc.) did not increase the node count;
  the device settings were restored afterwards. The search page control tree is exactly what the `search_result` rule reads.

## Running

```bash
# Web interface (http://localhost:5000)
python app.py

# Single-task command line
python run.py

# Multi-application task command line
python multi_agent.py

# Optional Streamlit interface
streamlit run frontend.py
```

Windows users can also launch via `start.bat`; that script requires a Conda environment named `mobile_agent` to exist.

The optional Streamlit interface (`streamlit run frontend.py`) reads `agent_state.json` to show the per-step state and is intended for debugging; `streamlit` is not a required dependency, so if it is not installed just skip this entry point.

## Configuration

Models and behaviour switches are centralized in `config.py`; `.env` holds only secrets:

| Setting | Description |
|---|---|
| `ACTIVE_PROVIDER` | Execution model (vision), default `deepseek` → `deepseek-flash`, requires `DEEPSEEK_API_KEY`; can be set to `qwen` / `zhipu` / `openai` / `local` |
| `PLANNER_PROVIDER` | Planning/semantic model, default `deepseek` (`deepseek-flash`); both single-task and multi-application modes read this setting; can be switched back to `qwen3` |
| `MAX_STEPS` / `STEP_RETRIES` | Maximum number of steps (default 30, overridable with the environment variable of the same name) and the retry count for a failed step. Steps are counted per **model request**: one response that returns several consecutive actions in the `actions` array still counts as one step |
| `MAX_ACTIONS_PER_STEP` | Maximum number of consecutive actions accepted from one response, default 3; anything beyond is truncated. Actions execute in order, and `step_NNN.json` records `action_type="sequence"` and the `actions` array |
| `READ_ONLY_MODE` | Read-only guard, on by default: rejects `send` / `press_enter` / `type`; forced on for monitor production screen reading; can be set to 0 when running task scripts standalone |
| `SCREEN_DRIVER` | Screen driver: `auto` (default, automatically falls back to `adb_dump` when u2 is unavailable) / `uiautomator2` (force u2, explicit error on failure) / `adb_dump` (Android 17 fallback) |
| `LLM_TEMPERATURE` | Temperature for decision and observation calls, default 0.0, keeping repeated experiments comparable |
| `ENABLE_SEMANTIC_ANALYSIS` | Semantic analysis (one text-only request per step), off by default; set to 1 to enable the ablation comparison |
| `COMPLETION_CHECK` | Completion detection: `decision` (default, reads `task_done` from the decision response) / `every_step` (old behaviour, an extra image request every step) / `off` |
| `MERGED_OBSERVE` | Merge observation into the decision, on by default; normally 1 image request per step, and if the response is invalid that step falls back to the two-stage form |
| `RULE_FASTPATH` | Rule fast path, on by default: groups/conversations named in the task, back, and scrolling after entering a conversation are executed directly whenever the control tree can prove them, without calling the vision model; hits and saved calls are written to `timings.rule_*` in `step_NNN.json` and `rule_fastpath` in `usage_summary.json` |
| `RULE_MAX_SCROLLS` | Maximum number of consecutive scroll steps the rule fast path may take inside the same conversation (default 6); at the limit control returns to the model |
| `CONNECTION_WARMUP` | Pre-warm connections at startup, on by default: vision and planning share a single httpx connection pool (keep-alive) and issue `models.list()` concurrently to complete the TLS handshake; on the device side it calls the driver's `warmup()` (starts the adb server in adb fallback). Warm-up time is written to `warmup_seconds` in `usage_summary.json` and is not counted in the first step |
| `HTTP_KEEPALIVE_SECONDS` / `HTTP_MAX_CONNECTIONS` | Keep-alive seconds (default 90) and maximum connections (default 8) of the shared connection pool |
| `OCR_PREHEAT` | Pre-warm RapidOCR at startup, on by default |
| `OBSERVE_SETTLE_MODE` | `adaptive` (default, returns as soon as the screen is stable) / `fixed` (always waits the full `OBSERVE_SETTLE_SECONDS`) |
| `OBSERVE_SETTLE_POLL_SECONDS` | Screenshot polling interval for adaptive settle, default 0.15s |
| `OCR_MODE` | OCR invocation policy: `on_demand` (default, runs only on tap and snaps the landing point) / `always` (old behaviour, pre-run every step) / `off` |
| `OCR_SNAP_MODE` | Candidate source: `tree_first` (default, merges control tree, YOLO and OCR then matches against the target) / `ocr_only` / `tree_only` |
| `OCR_ENGINE` | OCR engine: `rapidocr` (default, measured 1.2–1.5s per image on ONNX CPU) / `easyocr` (old implementation, 12.7–23s per image) |
| `OCR_SCALE` | Scale factor for the OCR input image, default 0.5; detection boxes are mapped back to the original image by `1/scale`, and tap coordinates remain device coordinates |
| `OCR_SNAP_MAX_DISTANCE` | Snap radius (pixels), default 80; beyond it the model's original coordinates are kept |
| `SEARCH_ICON_DETECTION` | Whether to add YOLO candidates when the tap target is a search/contact element, on by default; search-icon candidates are produced only when the weights contain the `search_icon` class |
| `GUI_MODEL_PATH` | YOLO weights path, default `models/input_box.pt` (shipped with the repository, containing `input_box` and `search_icon`); with other weights, visual search-icon candidates are enabled only if the `search_icon` class is present |
| `OBSERVE_SETTLE_SECONDS` | Seconds to wait for the screen to stabilize after an action, default 1.0 |
| `HISTORY_IMAGE_WINDOW` / `HISTORY_TEXT_STEPS` | History context window: how many recent original images to include (default 1 — the current screen is always included as an image), and how many earlier steps to list as text summaries (default 8); both overridable with the environment variables of the same name |
| `IMAGE_PREPROCESS` | Master switch for screenshot preprocessing (on by default); when 0 the PNG is sent as-is |
| `IMAGE_JPEG_QUALITY` | JPEG quality, default 85; 0 keeps PNG |
| `IMAGE_MAX_SIDE` | Upper bound on the longest side of the proportional resize, default 0 (no resize). **Resizing breaks coordinate consistency — see the next section** |
| `IMAGE_ENHANCE` | Contrast/sharpness enhancement, off by default; does not change geometric dimensions |
| `ARTIFACT_DIR` | Directory for per-step screenshots and action records, default `runs/` |
| `WECHAT_INPUT_RESOURCE_ID` | WeChat input box resource-id, which changes between versions; leave empty to skip this branch |

Switchable providers are listed in `config.PROVIDERS`; `qwen3` / `minimax` are text-only providers and cannot be used as `ACTIVE_PROVIDER`.

The planner uses DeepSeek by default: `base_url=https://api.deepseek.com` (do not append `/v1`), model `deepseek-flash`, key read from `DEEPSEEK_API_KEY` in `.env`. The planner sends text only and does not receive screenshots; measured, a simple task returns parseable JSON in about 2 seconds and a three-application task in about 3.5 seconds. If `thinking` is enabled (passing `{"thinking": {"type": "enabled"}}` via `extra_body`) it returns `reasoning_content` and incurs reasoning tokens; it is currently off by default to reduce latency. Measured, the `content` returned in both modes is valid JSON.

## DeepSeek Vision Capability (Measured)

The official documentation says `deepseek-v4-flash-vision-exp` has been retired and that requests under the old name are served by DeepSeek-V4.1-Flash-1017 (i.e. `deepseek-flash`), which reads as if it were a text-only model. **Measurements contradict that statement: `deepseek-flash` accepts and understands `image_url` normally.**

- Control-image method: images containing random numbers were generated on the spot (`ORDER NO: ZX-739146`, `CNY 4287.35`, etc.); the model read them correctly character by character, ruling out "no error but the image is ignored".
- A real desktop screenshot (1707×1067): 10.4s, `prompt_tokens=1012`, accurately describing icons, windows and body paragraphs.
- Multiple images: 3 images in one request (each with a unique number); all three were answered correctly and in order, hence `support_multi_image=True`.
- End to end: going through the real call path of `ScreenAgent._think_and_act`, it returned `{"action_type": "tap", "x": 540, "y": 1441, ...}` in 5.9s, with complete fields and coordinates 1px off the ground truth.

Conclusion: `deepseek-flash` can serve as the execution (vision) model; this document keeps the record that "the official documentation contradicts the measurements", and it must be re-verified if server behaviour changes.

### Image Limits and thinking

- **At most 1024 tokens per image**: the server scales the image to roughly a 1300×1300 equivalent before billing; the measured 1707×1067 screenshot had `prompt_tokens=1012`, already close to the cap. **Client-side downscaling saves no tokens**.
- **`detail` semantics**: `low` compresses to 512×512 (small text on phone UIs becomes blurry — do not use it for the execution model); `high` / `original` / `auto` keep the original image. Measured on the control image, `detail=low` gave `prompt_tokens=240` vs `631` for `detail=high`, at the cost of lost detail.
- Images can be placed in user / system / tool messages; an assistant message with an image returns 400; at most 600 images per request. This project sends only 1–3 at a time and is nowhere near the limit.
- **thinking switch** (`extra_body`): on the same screenshot, all four settings produced exactly the same action (all `home`, all valid JSON), but the latency and reasoning tokens differ markedly:

  | Setting | Latency | completion_tokens | reasoning_tokens |
  |---|---|---|---|
  | `thinking` disabled | 1.42s | 60 | 0 |
  | `reasoning_effort=low` | 2.01s | 139 | 102 |
  | default (not passed) | 3.51s | 404 | 363 |
  | `thinking` enabled | 3.19s | 296 | 258 |

  There is no quality difference, so the provider defaults to `{"thinking": {"type": "disabled"}}`, saving both time and cost.

## Screenshot Preprocessing and Coordinate Consistency

After the screenshot and before the request, the following happen in order: encoding optimization (PNG→JPEG) → optional proportional resize → optional light enhancement. The implementation is in `image_preprocess.py` and is fully controlled by the `IMAGE_*` switches in `config.py`.

**Why there is no downscaling by default**: measured, client-side downscaling does not reduce tokens (the server already caps at 1024 per image), but it makes the coordinates returned by the model leave the coordinate system of the sent image, and the error after back-mapping is far beyond an acceptable range:

| Setting | Upload bytes | Sent size | prompt_tokens | Order-number recognition | Coordinate error |
|---|---|---|---|---|---|
| Original PNG | 78,302 | 1080×1920 | 1075 | Correct | 0 px |
| JPEG q85 | 82,052 | 1080×1920 | 1075 | Correct | 10 px |
| JPEG q85 + longest side 1300 | 48,165 | 731×1300 | 680 | Correct | **375 px** |
| JPEG q85 + longest side 1000 | 34,052 | 562×1000 | 451 | Correct | **691 px** |

(The test image was a simulated phone checkout page; the ground-truth centre of the red button is `(540, 1440)`. The raw coordinates of the scaled variants belong neither to the sent-image coordinate system nor to the original-image one, so after back-mapping they deviate by hundreds of pixels.)

The coordinates themselves jitter: repeating the same original image 3 times gave errors of 210/0/5 px for PNG and 10/146/38 px for JPEG q85; format is not the main cause, resizing is. Hence `IMAGE_MAX_SIDE=0` by default, with JPEG transcoding only.

**Size benefit**: a real desktop screenshot went from 730 KB PNG to 224 KB JPEG q85 (30.7%), with unchanged tokens (1044). Uploading saves bandwidth and reduces timeouts, but does not save inference cost.

If resizing really is needed, `image_preprocess.map_action_coords` back-maps `x/y`, `x1/y1`, `x2/y2` by `scale_x/scale_y`, rounds them and clips them to the screen bounds; the per-step `step_NNN.json` records `image` (original/sent sizes and ratios) and `model_action` (the model's raw coordinates) for later cross-checking. Without resizing the function returns its input unchanged and introduces no offset.

## Per-Step Latency and Optimizations

Measured real-device reading (OPPO PLJ110 / Android 17 / 1080×2374, WeChat self-chat conversation, 10 steps; because Android 17 is incompatible with uiautomator2 3.7.1, the real device used `SCREEN_DRIVER=adb_dump`): 81.42s (8.14s/step) before the fix, and 105.59s (10.56s/step) after enabling scroll observation and OCR, of which OCR accounted for 26.27s. The full record is in `docs/real-device-test-report.md`; this is a single result and not a stable performance figure.

| Stage | Latency | Notes |
|---|---|---|
| Screenshot | 0.2–0.9s | `uiautomator2` screenshot |
| OCR | 0 or 1.2–1.5s | on-demand mode triggers only on tap steps; measured with RapidOCR + `OCR_SCALE=0.5` |
| Decision request | 1.7–2.6s | DeepSeek, with history images + current image |
| Semantic analysis | ≈2s | only when enabled, text-only |
| Action execution | 1.9–3.0s | includes the fixed wait for `tap`/`swipe`, and about 2.5s for `open_app` |
| Observation request | 4.0–6.1s | includes a 1.0s stability wait + screenshot + image request |
| Completion detection | ≈2.5s | old behaviour, one image request per step |
| Loop interval | 0.5s | fixed |

Before optimization, each step made up to **4 model requests** (decision / semantic / observation / completion detection), 3 of them with images. The current changes:

1. Completion detection is merged into the decision response (the `task_done` field), so by default a separate completion request is no longer sent;
2. Semantic analysis is off by default (`ENABLE_SEMANTIC_ANALYSIS=0`);
3. The vision request's `extra_body` was added, ensuring `thinking` is really disabled (previously only the planning request carried this parameter);
4. `type_text` writes directly when the input box is already focused, avoiding double taps and a second YOLO fallback;
5. OCR became on-demand (`OCR_MODE=on_demand`): it does not run before the decision, only when the model returns `tap`; and it became "control tree first, OCR fallback" (`OCR_SNAP_MODE=tree_first`), so OCR does not run at all when the control tree matches;
6. The OCR engine was switched to RapidOCR (`OCR_ENGINE=rapidocr`, ONNX Runtime, skipping orientation classification), and by default the image is downscaled by `OCR_SCALE=0.5` before recognition: measured 1.2–1.5s on real 1080×2374 screenshots (about 1.3–1.6s without downscaling; both reach 5–8s under high parallel machine load). The EasyOCR path is retained and can be switched back for comparison;
7. **Observation merged into the decision (`MERGED_OBSERVE=1`, on by default)**: one image request returns both `observation` and `action`; the observation in `step_NNN.json` is tagged `observation_scope=pre_action`, meaning it comes from the merged response and describes the screen before this step's action was executed. If the response lacks `observation` / `action_type` or the JSON is invalid, that step falls back to the old two-stage form (observe + decide), and the action is still executed only once;
8. **OCR pre-warm at startup (`OCR_PREHEAT=1`, on by default)**: RapidOCR is loaded when `ScreenAgent` initializes, so the first 7s load is not pushed onto the first step;
9. **Adaptive settle (`OBSERVE_SETTLE_MODE=adaptive`, default)**: after an action, returns early once two consecutive screenshot hashes match, waiting at most `OBSERVE_SETTLE_SECONDS`; `fixed` keeps the old fixed wait;
10. Each step's `step_NNN.json` gains `timings` (screenshot/ocr_pre/ocr/ocr_triggered/ocr_reason/snap_source/snap_tree/snap_yolo/snap_ocr/ocr_snap/preprocess/semantic/decision/decide_total/execute/observe/settle/completion/merged_fallback) for computing trigger rates and saved time; `timings.ocr` already includes the control-tree, YOLO and OCR localization segments, and `snap_yolo` records the YOLO segment separately.

Estimating from the table above: the old flow made up to 4 model requests per step; after merging completion detection into the decision and turning semantic analysis off it is 2; with `MERGED_OBSERVE=1` merging observation and decision by default, **normally 1 image request per step**. Extrapolating from the old stage timings gives roughly **8–10s → 5–7s** per step (estimate). Real-device A/B is in the next section: on 235deea merged observation was actually slower, caused by the concurrent rule fast path scrolling a fixed 6 times, not by merged observation itself; that was fixed with "scroll limit 2 + light observation": measured 11.44s with rules vs 17.81s without, and the rule fast path is on by default under `adb_dump`. OCR pre-warm only saves about 6–7s on the first step after startup, and adaptive settle saves at most about 1s per step. The single real-device measurement is linked in the report above and is not a stable performance figure.

Fallback path of merged observation: if the merged response is invalid, the step makes 3 requests in total (1 failed merged request + 1 observe + 1 decide), and the action is still executed only once; with `COMPLETION_CHECK=every_step` merging is not done, keeping the original two-stage form + completion detection, to avoid degenerating into an extra task_done call. `tests/test_ocr_on_demand.py` uses a fake client to assert 1 request per step by default, 2 when merging is off, and that the action executes only once on fallback.

### How to Read Per-Step Tokens and Timings

- The `usage` of each step's `runs/<run>/step_NNN.json` is the token summary of that step's vision calls (listed by `decision` / `observe` / `completion` in `calls`; `null` when usage is missing); `timings` holds the per-stage latencies.
- When a task ends normally or reaches the maximum number of steps, `runs/<run>/usage_summary.json` is written: total prompt/completion/total tokens, per-step means, and a per-provider summary.
- Stage-timing summaries (single run or multi-run comparison):

```bash
python experiments/summarize_timings.py runs/20261124-095136
python experiments/summarize_timings.py runs/20261124-095045 runs/20261124-095136
```

The output includes the sample count, total, mean, P50 and P90 of stages such as `decision/observe/ocr/yolo/screenshot/execute/settle`; with several directories a `mean / p90` comparison table is appended.

### How to Run the MERGED_OBSERVE A/B

Run the same task with merged observation off and on, then compare the two runs:

```powershell
# 1) Merging off (old two-stage form)
$env:MERGED_OBSERVE="0"; python run.py
# Note the run directory produced, e.g. runs\20261120-101500-off

# 2) Merging on
$env:MERGED_OBSERVE="1"; python run.py
# Note runs\20261120-102000-on

# 3) Compare stage timings and tokens
python experiments\summarize_timings.py runs\20261120-101500-off runs\20261120-102000-on
Get-Content runs\20261120-101500-off\usage_summary.json
Get-Content runs\20261120-102000-on\usage_summary.json
```

For an A/B, keep the same task text, the same device, the same network and the same provider; otherwise the numbers are not comparable. `--json` lets `summarize_timings.py` hand its result to other scripts.

### Verification Labels: Real-Device Measurement / Offline Replay / Not Executed

Both the report and the scoring table carry source labels; an offline replay does not equal a real-device measurement.

| Label | Meaning | Current conclusion |
| --- | --- | --- |
| Real-device measurement (baseline 235deea) | Screenshots, `step_*.json` and `usage_summary.json` produced directly on the device | Same task, same group, `adb_dump`: `MERGED_OBSERVE=0` 20.63s / 3 requests; `=1` 71.07s / 1 request; the sequences differ (`tap→finish` vs `tap→swipe×6→finish`), which is a policy difference |
| Real-device measurement (after the 2026-11-22 change, rules on by default) | Same as above | `MERGED_OBSERVE=0` 21.03s / 5 requests / 2 dumps; `=1` 11.45s / 1 request / 3 dumps (rule `open_group` + 1 model completion; rule steps 5.47–5.65s). The two sequences differ (model navigation vs rule navigation), so this only counts as a policy difference |
| Real-device measurement (merged observe, rules off) | Same as above | 3 runs each for 0/1: 21.27s vs 17.81s (the first two; the third timed out at 56.3s), sequences identical (`wait→tap→finish`), merged observation about 16% faster |
| Real-device measurement (rule fast path on/off) | Same as above | rules forced on 11.44s / 1 request vs off by default 17.81s / 3 requests; a rule step takes 5.5s ≈ a model step's 5.3s, but with one fewer request, hence on by default |
| Real-device measurement (search vs list) | Same as above | same group, 3 runs each: search 39.01s / 7 dumps / 4 steps; list 11.48s / 3 dumps / 2 steps. Under `adb_dump` the search-page dump is clearly slower, so search is not the default (`RULE_SEARCH_FORCE=1` forces it) |
| Real-device measurement (monitor) | Single-process `run_cycle` (QQ, single group) | 7 steps / 51.2s / 19,591 tokens; the full `fetch→extract→dedupe→resolve→write` chain, outputting `data.json`, `deadlines.ics`, `calendar.html`; the phone-calendar test event was deleted after being written, and the .ics generates VEVENT+VALARM |
| Offline replay | Frozen real-device extraction candidates, recomputed with the current resolution/focus logic; no network, no phone | Scoring re-run **21/21 = 100%** (hit 21, partial 0, fail 0, violation 0; D02/D03 were not collected and are excluded from the denominator; D06 went from partial to hit after the pinned-chat filter fix, the old 20/21 is deprecated); the 2026-11-24 re-run on `main@411c48e` is byte-identical to the 2026-11-22 artifacts (SHA256 `F4894F17…`); evidence `runs/_verify/judge_replay_result.json` (`runs/` is not committed) |
| Offline replay | Rule stop conditions | Live control tree on the real device: on the chat page, when "操作系统作业一" is already displayed the rule scrolls 0 times; group list `open_group`; the send-area coordinates/text are rejected |

Same-screen reuse and connection warm-up are not listed as gains: `reused_frame` was 0 in all 6 archive A/B runs; reuse only hits on static pages, and chat pages change hash because of the cursor/animation; reuse saves at most 0.11–0.15s/step of `preprocess` (one same-screen reuse occurred in monitor, but not enough to change the task-latency conclusion); connection warm-up costs 0.22–0.43s only once at startup, i.e. 0.00s per step.

### Sanitized Evidence

`experiments/export_evidence.py` exports the minimal public set from `runs/` to `docs/evidence/<run>/`: it keeps step counts, action types, stage timings, token usage, observation length and hashes; it strips full-screen screenshots, sent images, raw observation/action text, group/course names and raw OCR text. See `docs/evidence/20261117-211201/` for an example. Do not commit the whole `runs/` directory.

`OCR_SCALE` trade-off (measured on the same batch of images, second invocation, idle machine):

| scale | Size after scaling | Latency | Settings-page elements | Checkout-page elements | Key fields (￥128.00/order number/confirm payment) | Centre deviation |
|---|---|---|---|---|---|---|
| 1.0 | 1080×2374 | 1.3–1.6s | 23 | 14 | all correct | baseline |
| 0.75 | 810×1780 | 1.2–1.4s | 25 | 14 | all correct | mean 1.2–4.4px, max 32px |
| **0.5 (default)** | 540×1187 | **1.2–1.5s** | 24 | 14 | all correct | mean 1.4–2.9px, max 32px |
| 0.35 | 378×831 | 1.3–1.5s | 23 | 14 | all correct | mean 1.4px, max 3px |

Conclusion: on this machine and this batch of images, 0.5 produced no missing characters — `￥128.00`, the order number and the button text were all correct — with a mean coordinate deviation of 1.4–2.9px (a few long text lines shift up to 32px because their bbox centre moves, still within the 80px snap radius). 0.5 rather than 0.35 was chosen to leave margin for smaller fonts / lower-contrast UIs; if small-text recognition degrades in some App, just set `OCR_SCALE` back to 1.0.

Triggers and paths of on-demand localization (`ocr_locator.should_trigger_ocr` + `OCR_SNAP_MODE`):

- Trigger condition: `OCR_MODE=on_demand` and the action is `tap`; all other actions (`open_app`/`back`/`home`/`wait`/`swipe`/`type`/`send`/`finish`) do not run it at all.
- `tree_first` (default): merges the control tree, OCR text and (on demand) YOLO candidates; with a `target` it scores by text match and snaps directly on a unique match, or picks the nearest to the model's coordinates among several; only without a `target` does it use the distance threshold.
- `ocr_only` / `tree_only`: force a single path, for comparison.
- If a named target cannot be matched, it first holds the screen and observes again; if it still cannot be matched twice in a row it fails explicitly ("目标未定位，未执行点击" — target not located, click not executed) and no longer guesses a tap from the model's coordinates.
- Snapping without a target happens only when the distance to the nearest element centre is ≤ `OCR_SNAP_MAX_DISTANCE` (default 80px) or the point falls inside a bbox; the actual path and latency are written to `timings.snap_source` (`tree`/`ocr`/`yolo`/`none`), `snap_tree`, `snap_yolo` and `snap_ocr`.

## Local Vision Model (Qwen3-VL-8B-Instruct GGUF)

> **Optional configuration, not the default**: the default execution and planning models are still the cloud `deepseek` (see
> `ACTIVE_PROVIDER` / `PLANNER_PROVIDER` in `config.py`; offline extraction also defaults to `deepseek`). The local 8B model is enabled only when
> `ACTIVE_PROVIDER=local` is set explicitly, and requires downloading the weights and mmproj yourself (not included in the repository);
> with 6 GB of VRAM the vision encoder has to stay on the CPU, giving about 75–80 seconds per image request.

Intended for offline execution without depending on a cloud vision API. Weights, mmproj, the llama.cpp runtime and the HF cache live outside the repository, by default in `%LOCALAPPDATA%\qwen3vl-local` (overridable with `-BaseDir` or `LOCAL_VLM_HOME`), and are not written to git.

### Hardware and Parameter Trade-offs

The machine is an RTX 3060 Laptop (6 GB VRAM). The Q4_K_M weights are 5,027,784,800 bytes (about 4.68 GiB) and mmproj F16 is 1,159,029,824 bytes (about 1.08 GiB); together they already exceed 6 GB, so **it is impossible to fit everything in VRAM**. Therefore:

- All text layers are offloaded to the GPU: `-ngl 36`
- The vision encoder stays on the CPU: `--no-mmproj-offload`
- KV cache quantization: `--cache-type-k q8_0 --cache-type-v q8_0`
- Context: `-c 8192`
- Measured steady usage about **5.3 GB / 6.1 GB** (`nvidia-smi` 5367 MiB), no OOM

The cost is that vision encoding runs on the CPU: a request with one 1707×1067 screenshot takes about **76–78 seconds**; text-only requests take about 10 seconds. Putting mmproj back on the GPU causes OOM due to insufficient VRAM; lowering `-ngl` leaves headroom but slows text generation.

### Files and Downloads

Fetch the weights and the vision projection from the same HF repository (the model cannot see images without mmproj):

```bash
# Example target directory (outside the repository; adjust with -BaseDir as needed)
mkdir -p "$LOCALAPPDATA/qwen3vl-local/models"

curl -L -o Qwen3VL-8B-Instruct-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf
curl -L -o mmproj-Qwen3VL-8B-Instruct-F16.gguf \
  https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf
```

llama.cpp uses the `b12470` Windows CUDA 12.4 prebuilt package: `llama-b12470-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`, extracted into the same directory (on this machine `runtime\llama-extract\`). Set the cache directory explicitly:

```powershell
$env:HF_HOME = "$env:LOCALAPPDATA\qwen3vl-local\hf-cache"
$env:HF_HUB_CACHE = "$env:HF_HOME\hub"
```

### Starting and Switching

One-click start (runs in the foreground, port 8080):

```powershell
powershell -ExecutionPolicy Bypass -File .\start_local_vlm.ps1
```

Script parameters can be adjusted: `-Port`, `-NGpuLayers`, `-Ctx`, `-Threads`, `-BaseDir`, `-ModelDir`, `-LlamaDir`. When `-BaseDir` is not given, `LOCAL_VLM_HOME` then `%LOCALAPPDATA%\qwen3vl-local` are used in that order. The server offers an OpenAI-compatible endpoint:

```bash
curl http://127.0.0.1:8080/v1/models
# Model alias: qwen3-vl-8b-instruct; multimodal (image_url base64) supported
```

Switching the execution model (no code changes):

```powershell
$env:ACTIVE_PROVIDER = "local"   # or "qwen" to switch back to the cloud
python run.py
```

`config.PROVIDERS["local"]` defaults to `base_url=http://127.0.0.1:8080/v1`, overridable with `LOCAL_VLM_BASE_URL` and `LOCAL_VLM_MODEL`; `llama-server` does not validate the key, so `LOCAL_VLM_API_KEY` only needs to be non-empty (default `local`).

Real-request verification (measured on this machine): a real desktop screenshot → correct visual description; following the execution prompt it returned `{"action_type": "home", "params": {}, "observation": "返回桌面"}` ("back to the home screen"), and the JSON is parseable. A request with an image spent about 76 seconds in prompt eval (CPU vision encoding) and about 1–2 seconds generating. It has not been run on a real Android device; only the model server and the image protocol were verified.

### Context Window (Why a 6 GB Card Must Be Limited)

Every screenshot becomes hundreds to over a thousand vision tokens, and carrying all historical original images across multi-turn tasks raises both context and VRAM usage. Default policy: `HISTORY_IMAGE_WINDOW=1` includes only the most recent historical original image, with the current screen always included; earlier steps are compressed into text summaries (action type, coordinates/typed text, observation conclusion), with the count controlled by `HISTORY_TEXT_STEPS`.

The cost of raising `HISTORY_IMAGE_WINDOW`: vision tokens and VRAM usage rise, a 6 GB card OOMs more easily, and per-step latency increases; it is recommended to raise it by 1 at a time on the current device while watching `nvidia-smi` and request latency.

## Project Structure

- Core pipeline: `screen_agent.py` (screen understanding and action loop), `phone_controller.py` (uiautomator2 operations), `phone_driver.py` (driver selection/fallback), `adb_controller.py` (the adb dump fallback for Android 17), `ocr_locator.py` (OCR + control-tree localization), `input_detector.py` (YOLO input-box fallback), `app_launcher.py` (app launching)
- Memory and state: `memory.py` (step records), `ui_state.py` (Streamlit state)
- Planning: `task_planner.py` (subtask decomposition and replanning, including `context_needed` information passing), `multi_agent.py` (multi-application orchestration)
- Interface: `app.py` + `templates/` (Flask + SocketIO), `frontend.py` (optional Streamlit)
- Configuration: `config.py`, `.env.example`
- Experiments: `experiments/` (task set, repeated runner, metric aggregation, baseline adapter layer and three upstream wrappers)
- Tests and engineering: `tests/` (644 passed, 2 skipped; CI runs the full suite with no physical device or model), `pyproject.toml` (ruff + pytest configuration), `.github/workflows/ci.yml`, `requirements-lock.txt` (single source of truth for dependencies), `scripts/check_test_deps.py` (checks that test imports are consistent with the lock), `scripts/create_pr.py` (UTF-8-safe REST script for creating PRs)
- Helper scripts: `get_packages.py` (export the package names of apps installed on the phone), `debug_detector.py` (YOLO weights self-check), `find_input.py` (manually probe which height ratios bring up the keyboard)
- Documentation: `test.md` is the comparative experiment plan (task set, metrics, judging criteria)

Input-box localization prefers the uiautomator2 control tree (`EditText` etc.); `models/input_box.pt` is the YOLO visual fallback weights.

### Input Box Detection Weights

`models/input_box.pt` is shipped with the repository (about 5.4 MB) and is a two-class model with `input_box` + `search_icon`; `input_detector.py` produces input-box and search-icon candidates separately by class name. `.gitignore` allow-lists only this one weights file; other `*.pt` files (training artifacts, local models) are still not tracked.

- Source: trained by this project on 2026-11-25 on its own screenshot dataset (YOLO11n, 30 epochs, CPU); training artifacts are not committed, only `best.pt`
- Validation set (54 images / 80 instances): overall P 0.929 / R 0.803 / mAP50 0.936 / mAP50-95 0.753; `input_box` mAP50 0.959, `search_icon` mAP50 0.912
- Real-device sampling (6 WeChat/QQ screenshots): `input_box` confidence 0.82–0.96, detected even with the light theme; the old weights gave 0.21–0.37 on the same screenshots and missed detections on the light theme
- Checksums:

  ```bash
  git hash-object models/input_box.pt
  # expected 7274985afc25368c0989ef69e1af71c60e2c6c15

  sha256sum models/input_box.pt
  # expected 435403193CC96FEC4BF72505AB55237D723735898B70B634E8B900A7F02FE2CE
  ```

- If you still want the old single-class weights: `runs/detect/runs/input_box5/weights/best.pt` in `peter-lgxj/gui_yolo_model` (blob sha1 `2f5adef8fe64be1898363e8d38b5098ff4c62c2a`); just point `GUI_MODEL_PATH` at it
- Confidence: `input_detector.py` uses `conf=0.3` by default; in the on-screen candidate filtering, `search_icon` uses 0.45 and `input_box` uses 0.65. Raise these if real-device false positives are frequent
- With no weights or a load failure it automatically degrades to uiautomator2 control-tree (`EditText`) detection and never aborts the task

## Experiments

`experiments/tasks.json` contains the 20 tasks from `test.md` (with App, theoretical step count and difficulty level); the runner decides the repeat count automatically by level (`repeat_by_level`, currently 3 for L1/L2 and 2 for L3; **the final L3 repeat count is an open item in `test.md` §4.2** — the current value is only the implementation default and can be unified with `--repeat`):

> D04 in the external dataset `测试数据集\微Q邮\ddl_tasks.json` only says "enter the class group" (进入班级群), which may enter the wrong group when several QQ groups exist on the phone. After loading tasks, `run_experiment.py` overrides it by `task_id` with "enter the 「2026 计算机班级群」 group" (`进入『2026 计算机班级群』`), **without modifying the dataset file itself**; other tasks are kept as-is. For a single-QQ-group smoke test you can use `sources.qq.example.yaml` in the repository root (copy it to `sources.qq.yaml`); it scans only that one group, with independent output/state directories that do not affect the default `sources.yaml`.

```bash
# Run the task set (requires a physical device and keys)
python experiments/run_experiment.py --tasks experiments/tasks.json --mode single

# Aggregate TSR / multi-App TSR / SE / AT / ST / CS
python experiments/metrics.py experiments/results/<run_dir>

# Ablation: turn off the semantic analysis layer
python experiments/run_experiment.py --tasks experiments/tasks.json --no-semantic --tag no_semantic

# Force every task to run only 3 times (overrides the by-level rule)
python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3

# Multiple devices: specify an adb serial (equivalent to setting ANDROID_SERIAL)
python experiments/run_experiment.py --tasks experiments/tasks.json --serial emulator-5554

# Failure replay: re-run only the tasks that failed/timed out/crashed last time
python experiments/run_experiment.py --tasks experiments/tasks.json \
  --only-failed experiments/results/<run_dir>/results.jsonl

# Run only the given levels / task IDs (does not affect the by-level repeat rule)
python experiments/run_experiment.py --tasks experiments/tasks.json --levels L1,L2
python experiments/run_experiment.py --tasks experiments/tasks.json --task-ids T01,T05

# AT conventions are switchable: --at-mode selects samples (all/success/best), --at-aggregate selects the cross-task aggregation
python experiments/metrics.py experiments/results/<run_dir> --at-mode success
python experiments/metrics.py experiments/results/<run_dir> --at-aggregate total_per_task
```

The two cross-task AT aggregations (`--at-aggregate`):

- `task_mean` (default): compute each task's mean attempt latency first, then average equally across tasks; consistent with older results
- `total_per_task`: total latency of all attempts included in the statistics ÷ number of tasks

The two are equal when every task has the same number of attempts, and diverge when attempt counts differ (timeouts, failure replay, missing judgements). `--at-mode` decides which attempts enter the statistics, and the two can be combined.

Results are written to `experiments/results/` by default (already in `.gitignore`). Filters (`levels` / `task_ids` / `only_failed`) are written into `manifest.json` and the `filters` field of every `results.jsonl` line, so it is possible afterwards to see that "only some tasks were run". The runner returns to the home screen before each task and force-stops the known apps involved in that task (disable with `--no-reset`); `manifest.json` records the git SHA, the full names of the execution/planning models, the temperature, the task-set hash and device information.

Baseline integration: the three wrappers construct commands according to the upstream entry points, and every complete command can also be overridden with an environment variable. The protocol is one line of JSON on stdin (`task` required, `apps` optional) → one line of JSON on stdout (`summary/steps/elapsed/returncode/upstream_status`), with the exit code passed through.

**Commands are built from upstream documentation and have not actually been run**: this machine has no physical device, nor the dependencies/keys of these three systems installed; the entry points, parameters and output formats below come from cross-checking the upstream repository code — complete the environment setup per each upstream README before integrating.

```bash
python experiments/run_experiment.py --baseline openautoglm \
  --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
```

- **Open-AutoGLM** (`zai-org/Open-AutoGLM`)
  - Environment variables: `OPEN_AUTOGLM_DIR`; optional `OPEN_AUTOGLM_MAX_STEPS`
  - Default command: `python main.py [--device-id $ANDROID_SERIAL] [--max-steps N] "<task>"`
  - The task is a positional argument and must be the last one (not `--task`); the model service is configured with the upstream `--base-url/--model/--apikey` or the `PHONE_AGENT_*` environment variables
  - Output: each step prints `💭 思考：` / `🎯 动作：`, and at the end prints `Result: <message>` (`Result: Max steps reached` when steps are exceeded)
  - Step count is taken from the `🎯` lines; `upstream_status` distinguishes only `finished` / `max_steps` and is not used as a success judgement
- **Mobile-Agent-v2** (the `Mobile-Agent-v2/` directory of `X-PLUG/MobileAgent`)
  - Environment variables: `MOBILE_AGENT_V2_DIR`; optional `MOBILE_AGENT_V2_CMD`
  - Default command: `python run.py`. Upstream has no command-line arguments; the task and API configuration are written at the top of run.py (`instruction` / `adb_path` / `API_url` / `token` / `qwen_api`); if they are still empty the wrapper reports an error directly
  - Output: a `### Decision ###` block per step (Thought/Action/Operation), screenshots in `screenshot/`; the action `Stop` ends the run
  - Step count is taken from the ` Decision ` heading lines; `upstream_status=stopped` only means that a Stop occurred
- **AppAgent** (`TencentQQGYLab/AppAgent`; the original `mnotgod96/AppAgent` has moved)
  - Environment variables: `APPAGENT_DIR`; the app name is taken from `apps[0]` of the stdin JSON or from `APPAGENT_APP`; optional `APPAGENT_CMD` / `APPAGENT_STDIN`
  - Default command: `python run.py --app <app>`; upstream reads interactively, so by default `APPAGENT_STDIN` (default `y\n<task>\n`) is fed to the child process, matching the prompt order for "no document library + single device"; set it yourself if your environment differs
  - Output: `Round <n>` per round, logs in `tasks/task_<app>_<timestamp>/log_*.txt`; the final text is `Task completed successfully` / `Task finished due to reaching max rounds` / `Task finished unexpectedly`
  - Step count is taken from the `Round ` lines; `upstream_status` records the corresponding final text and is still not used as a success judgement

`BASELINE_STEP_MARKER` can override the step marker (with several candidate markers the default is used, so there is no need to set it). `success` is still judged manually, and a baseline's `upstream_status` is only written into `baseline_raw` as a clue for review. The protocol layer already has an offline fake-upstream test.

Whether a task succeeded still has to be judged manually: either write the `success` field of `results.jsonl`, or a `judgement.json` in the same directory. Note that a task-level boolean `{"T01": true}` can only yield TSR; to compute CS you must judge each attempt `{"T01": [true, false, true]}`. Each task's raw execution evidence is in `runs/<timestamp>/`.

CI (`.github/workflows/ci.yml`) does not connect to a phone and does not call models.

### Submission Materials Index

| Material | Location | Description |
|---|---|---|
| Prompts | `prompts/ddl-agent-prompts.md` | Generated from the code by `python scripts/export_prompts.py`; regenerate after changing prompts |
| Three kinds of sample input | `examples/cases/README.md` | D01 normal / D08 changed conditions / D18 failure; includes offline substitutes that need no phone |
| Evaluation scripts | `experiments/run_experiment.py`, `experiments/metrics.py`, `experiments/export_evidence.py` | Task set, metric aggregation, sanitized evidence export |
| Evaluation results | `docs/proposal-compliance.md`, `docs/ddl-agent-design.md` | Actual numbers and limitations of baseline / ablation / similarity on the synthetic set |
| Sanitized traces | `docs/evidence/` | Sanitized step records of real-device read-only runs |

## Mocks, Disabled and Unavailable Dependencies

The items below are not complete, directly usable capabilities; when using them, label them as "disabled / unverified / optional":

| Item | Current status | Impact on use and reporting |
|---|---|---|
| Microsoft Graph | Only a retained skeleton, disabled | Outlook goes through phone App screen reading only, with no Graph integration; it must not be described as a second read path |
| Local vision model Qwen3-VL-8B GGUF | Optional, not the default; weights and the llama.cpp runtime live outside the repository and are not in CI | The default is still cloud DeepSeek; without local weights this path is unavailable |
| QQ channel | No official API; it uses the phone UI + screenshot OCR, and group-list scrolling has a limit (`QQ_LIST_MAX_SCROLLS`, default 8) | Not a mock, but subject to QQ UI versions; it must not be described as an "official API" |
| The three baselines (Open-AutoGLM / Mobile-Agent-v2 / AppAgent) | The wrappers are written per upstream documentation and the protocol has been tested with a fake upstream; **not installed and not actually run** | The report may only say "unverified", never comparison results or rankings |
| Embedding semantic matching | Optional dependency, not installed and no network by default | Without embeddings it uses lexical matching; it must not be described as a default capability |
| YOLO input-box weights | Shipped with the repository (`models/input_box.pt`, two classes `input_box` + `search_icon`) | Checked against the validation set and 6 real-device screenshots; no systematic tuning of the confidence threshold has been done |

## Known Limitations (Partial Real-Device Verification)

- Single device, single account: a read-only read and end-to-end extraction verification was done once on the WeChat "File Transfer Assistant" (「文件传输助手」) self-chat (`docs/real-device-test-report.md`); a once-only read-only scan was done on the QQ "COMP7503 作业通知群" test group (2026-11-24; 204 fetched / 184 new messages / 13 new candidates, final 11.28). Real WeChat course groups, Outlook, image-based DDLs, the scheduled mode, tapping and input, and cross-App information passing have still not been run end to end in a real environment.
- On Android 17 (API 37), uiautomator2 3.7.1 is still unusable ([#1209](https://github.com/openatx/uiautomator2/issues/1209)), and the program falls back to `adb_dump`. Real-device A/B of the fallback path has been completed once (235deea, one run in each merged-observation group; see "Verification Labels"), but **it has not been re-run since the rule-cost fix**; the pre-fix fallback samples are only used for offline parsing and are not speed-up evidence.
- Dependency versions: `requirements-lock.txt` was actually resolved in a Windows + Python 3.12 virtual environment; switching machines (especially with a different GPU/OS) requires re-running `pip freeze` and replacing it.
- Baseline systems (Open-AutoGLM / Mobile-Agent-v2 / AppAgent): their commands, output parsing and configuration requirements are written faithfully to the upstream code, and the protocol has been tested with a fake upstream; but the three real systems are still not installed on this machine and have not been integrated with a physical device, and the commands have not actually been run.
- The pre-task reset only does "return to home screen + force-stop known package names"; **it cannot automatically clear in-app drafts, chat input boxes, the clipboard or the IME state**; the `reset` field distinguishes `stopped` / `failed` / `not_installed` / `not_mapped`.
- `run.py`, `app.py`, `frontend.py` and the experiment runner share `runs/.device.lock`: the lock covers "reset + the whole task execution period", and an entry point that cannot acquire the lock either reports an error directly or is recorded as failed — it never force-stops while someone else is operating the phone. The lock file records a token + pid and verifies ownership on release; a stale lock is reclaimed only when the holding process has exited.
- Requires Python 3.10+ (annotations such as `dict | None` are used; 3.8/3.9 fail at import).

## Development and Testing

```bash
pip install -r requirements-lock.txt   # the same dependencies as CI (including pytest/ruff/openpyxl)
python scripts/check_test_deps.py      # verify that every third-party import in tests is in the lock

pytest -q          # unit tests: action fault tolerance, metric aggregation, task-set validation, config loading, baseline protocol
ruff check .       # only rules that catch real errors are enabled (E9/F63/F7/F82)
```

### Creating PRs with a Script (No Chinese Mojibake)

PowerShell 5's `Invoke-RestMethod` serializes JSON according to the local ANSI code page, so Chinese titles occasionally turn into `????` (this once produced `fix: ???? UI ...`). Use the script instead:

```bash
# Create a PR; the repository is inferred from origin by default
python scripts/create_pr.py --head fix/my-branch --title "fix: 中文标题" --body-file pr_body.md

# Update the title/body of an existing PR
python scripts/create_pr.py --update 19 --title "fix: 中文标题" --body-file pr_body.md
```

- The request body is sent by Python as UTF-8 bytes (`json.dumps(ensure_ascii=False).encode("utf-8")`), with an explicit `charset=utf-8`;
- The token is read from `git credential fill` and placed only in the Authorization header; it is never printed in plain text;
- After creating/updating, it immediately GETs the resource back and compares title and body character by character (newlines normalized to LF), exiting non-zero on any mismatch;
- Works on both PowerShell 5 and PowerShell 7: the script does not depend on the shell's default encoding. For the body, prefer `--body-file` (a UTF-8 file) to avoid long command-line arguments and quote escaping.

### Adding Test Dependencies Touches Only One File

1. Add an **exact version** to `requirements-lock.txt` (e.g. `bs4==4.12.3`), and add the minimum version to `requirements.txt` if necessary;
2. Do not change the dependency list in `.github/workflows/ci.yml` — CI simply runs `pip install -r requirements-lock.txt`;
3. Run `python scripts/check_test_deps.py` locally (CI runs it too):
   - it statically scans the `import`s in `tests/` and maps third-party modules to distribution names;
   - if an import is not in the lock, it exits non-zero immediately and lists the referencing files;
   - `python -m pip check` separately verifies that the installed dependencies do not conflict.
4. Test inputs may only go in `tests/fixtures/`; **do not read `runs/`, `output/` or `state/`** — these directories are ignored by `.gitignore` and do not exist after CI checkout. There is a guard in the tests that scans for such path concatenations.

### CI Behaviour

- Triggers: `push` only on `main`; feature branches go through `pull_request`; `workflow_dispatch` is also available for manual triggering.
- Concurrency: `concurrency` is grouped by workflow + ref, so a new push on the same branch cancels the old run.
- A single `test` job (ubuntu + Python 3.12): install from the lock → `pip check` → compileall → tasks JSON → dry-run → ruff → dependency consistency check → import smoke test → pytest.

The lock file is a **version lock (without hashes)** obtained by `pip freeze` on Windows + Python 3.12; the wheel sets on Linux/Windows differ, and a missing wheel makes the job fail immediately. If hashed locks are generated in the future with `pip-compile --generate-hashes` (or `uv pip compile --generate-hashes`), the workflow automatically switches to `pip install --require-hashes`.

> Historical lesson: `ci.yml` used to maintain a separate hand-written pip subset, and CI went red repeatedly when new tests needed `uiautomator2` / `cv2` / `flask`; it is now unified to the single lock source, with `check_test_deps.py` as a safety net.

## Notes

Running tasks operates a real phone; please validate the flow on a test device and pay attention to operations that send messages, place orders or make payments.

The web interface's `SECRET_KEY` and CORS can be overridden with the `FLASK_SECRET_KEY` and `CORS_ALLOWED_ORIGINS` environment variables; the defaults are only suitable for local debugging.

Screenshots are uploaded to the corresponding provider through the model API; assess privacy implications yourself when chat records or similar content are involved.

## License

MIT License, see [LICENSE](LICENSE).
