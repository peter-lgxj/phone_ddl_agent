# Baseline Integration Guide

This directory provides only the **adapter layer** and contains no comparison-system code. The three comparison systems must each be cloned and installed in their official way,
then handed to `experiments/run_experiment.py` for scheduling through the wrappers here under a unified protocol.

> The entry points, arguments and output formats of the three systems have been checked against the upstream repository code, but this machine has no real device, dependencies or keys,
> so **the commands have not actually been run**. The protocol layer has only been tested offline with fake upstreams.

## Protocol

A wrapper must:

1. Read one line of JSON from stdin: `{"task": "任务描述", "apps": ["微信", "高德地图"]}`
   - `task` is required; `apps` is optional and comes from the `apps` field in `experiments/tasks.json` (AppAgent uses it as `--app`)
2. Drive the corresponding system to execute the task
3. Output JSON on the last line of stdout:
   `{"summary": "...", "steps": 12, "elapsed": 45.6, "returncode": 0, "upstream_status": "finished", "command": [...]}`
   - `summary` is required; `steps` / `elapsed` may be omitted (the adapter times them when omitted)
   - `upstream_status` is a debugging clue (see the table below) and **does not participate in the success judgement**
4. Exit code 0 on normal completion, non-0 on exception (the adapter treats non-0 as failure)

## The three integrated wrappers

None of the three systems needs further changes to `build_command()`; you can still override the whole command with `<SYSTEM>_CMD` (supports the `{task}` placeholder),
and override the step marker with `BASELINE_STEP_MARKER`.

| System | Environment variables | Default command | Step counting | `upstream_status` source |
|---|---|---|---|---|
| Open-AutoGLM | `OPEN_AUTOGLM_DIR`, optional `OPEN_AUTOGLM_MAX_STEPS` | `python main.py [--device-id $ANDROID_SERIAL] [--max-steps N] "<task>"` | Lines containing `🎯` | Trailing `Result:` → `finished` / `max_steps` |
| Mobile-Agent-v2 | `MOBILE_AGENT_V2_DIR` | `python run.py` (no CLI arguments) | Title lines containing ` Decision ` | `### Action ### Stop` appears → `stopped` |
| AppAgent | `APPAGENT_DIR`, optional `APPAGENT_APP` / `APPAGENT_STDIN` | `python run.py --app <apps[0]>` | Lines containing `Round ` | Closing message → `completed` / `max_rounds` / `unexpected` |

### Open-AutoGLM

- Upstream: `github.com/zai-org/Open-AutoGLM`
- The task is passed as a **positional argument** (the last argument); there is no `--task`; the device is selected with `--device-id` (the wrapper reads `ANDROID_SERIAL`)
- The model service uses upstream's own `--base-url` / `--model` / `--apikey`, or `PHONE_AGENT_BASE_URL` / `PHONE_AGENT_MODEL` / `PHONE_AGENT_API_KEY`
- Each step prints `💭 思考：` / `🎯 动作：`; at the end it prints `Result: <message>`, and step overflow is `Result: Max steps reached`

### Mobile-Agent-v2

- Upstream: the `Mobile-Agent-v2/` directory of `github.com/X-PLUG/MobileAgent`
- `run.py` has **no command-line arguments**: `instruction` / `adb_path` / `API_url` / `token` / `qwen_api` are all written at the top of the file
- Before use, fill in these constants at the top of `run.py`, or set `MOBILE_AGENT_V2_CMD` to point at a custom entry point;
  if it is still empty, the wrapper fails directly, avoiding an empty run with an empty task
- Each step prints a `### Decision ###` block (Thought/Action/Operation), screenshots go to `screenshot/`; the `Stop` action ends the task

### AppAgent

- Upstream: `github.com/TencentQQGYLab/AppAgent` (the original `mnotgod96/AppAgent` has migrated)
- Entry point is `python run.py --app <app>`, and the remaining input is interactive prompts; the wrapper feeds `APPAGENT_STDIN`
  (default `y\n<task>\n`) to the child process, corresponding to the "no document library + single device" prompt order; adjust it for different environments
- The app name is taken from `apps[0]` of the stdin JSON, falling back to `APPAGENT_APP`
- Each round prints `Round <n>`; logs go to `tasks/task_<app>_<timestamp>/log_*.txt`; the closing message is one of three:
  `Task completed successfully` / `Task finished due to reaching max rounds` / `Task finished unexpectedly`

## `_common.py`

Responsible for reading stdin (`task` + optional `apps`), validating directories, launching the child process, timing, and outputting JSON per the protocol.
`status_fn` only extracts the upstream closing hint and does not rewrite `success`; `stdin_text` is used for interactive entry points such as AppAgent.

## Running

```bash
python experiments/run_experiment.py \
  --tasks experiments/tasks.json --repeat 3 \
  --baseline openautoglm \
  --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
```

Results are written to `experiments/results/<timestamp>_<tag>/results.jsonl` in the same way, and `success` still needs manual judgement against
Section 4 of `test.md`; `upstream_status` is persisted together with `baseline_raw` for review.
If a Level 3 task is not supported by a baseline's own multi-app planning, just record it as a failure; this is exactly the comparison point explained in `test.md`.

## Tests

`tests/test_baseline_wrappers.py` uses fake upstreams to cover: missing directory, exit-code passthrough, step/summary statistics,
Open-AutoGLM argument assembly, Mobile-Agent-v2 empty-config rejection and Stop detection, AppAgent stdin feeding and status parsing.
`tests/test_adapter.py` covers JSON protocol parsing and `apps` passthrough. These tests do not depend on real systems.
