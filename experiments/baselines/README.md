# baseline 接入说明

本目录只提供**适配层**，不包含任何对比系统的代码。三个对比系统需要各自克隆、按其官方方式安装，
再由这里的 wrapper 以统一协议交给 `experiments/run_experiment.py` 调度。

> 三个系统的入口、参数与输出格式已按上游仓库代码核对，但本机没有真机、依赖与密钥，
> **命令未实跑**。协议层只用假上游做过离线测试。

## 协议

wrapper 必须：

1. 从 stdin 读取一行 JSON：`{"task": "任务描述", "apps": ["微信", "高德地图"]}`
   - `task` 必填；`apps` 可选，来自 `experiments/tasks.json` 的 `apps` 字段（AppAgent 用它作为 `--app`）
2. 驱动对应系统执行该任务
3. 在 stdout 最后一行输出 JSON：
   `{"summary": "...", "steps": 12, "elapsed": 45.6, "returncode": 0, "upstream_status": "finished", "command": [...]}`
   - `summary` 必填；`steps` / `elapsed` 可省略（省略时由适配器计时）
   - `upstream_status` 是复盘线索（见下表），**不参与 success 判定**
4. 正常结束退出码为 0，异常为非 0（适配器会把非 0 视为失败）

## 已接入的三个 wrapper

三个系统都不需要再改 `build_command()`；仍可用 `<SYSTEM>_CMD` 整体覆盖命令（支持 `{task}` 占位），
用 `BASELINE_STEP_MARKER` 覆盖步骤标记。

| 系统 | 环境变量 | 默认命令 | 步骤统计 | `upstream_status` 来源 |
|---|---|---|---|---|
| Open-AutoGLM | `OPEN_AUTOGLM_DIR`，可选 `OPEN_AUTOGLM_MAX_STEPS` | `python main.py [--device-id $ANDROID_SERIAL] [--max-steps N] "<task>"` | 含 `🎯` 的行 | 结尾 `Result:` → `finished` / `max_steps` |
| Mobile-Agent-v2 | `MOBILE_AGENT_V2_DIR` | `python run.py`（无 CLI 参数） | 含 ` Decision ` 的标题行 | 出现 `### Action ### Stop` → `stopped` |
| AppAgent | `APPAGENT_DIR`，可选 `APPAGENT_APP` / `APPAGENT_STDIN` | `python run.py --app <apps[0]>` | 含 `Round ` 的行 | 结束文案 → `completed` / `max_rounds` / `unexpected` |

### Open-AutoGLM

- 上游：`github.com/zai-org/Open-AutoGLM`
- 任务以**位置参数**传入（最后一个参数），没有 `--task`；设备用 `--device-id`（wrapper 会读 `ANDROID_SERIAL`）
- 模型服务用上游自己的 `--base-url` / `--model` / `--apikey`，或 `PHONE_AGENT_BASE_URL` / `PHONE_AGENT_MODEL` / `PHONE_AGENT_API_KEY`
- 每步打印 `💭 思考：` / `🎯 动作：`；结束时打印 `Result: <message>`，超步数为 `Result: Max steps reached`

### Mobile-Agent-v2

- 上游：`github.com/X-PLUG/MobileAgent` 的 `Mobile-Agent-v2/` 目录
- `run.py` **没有命令行参数**：`instruction` / `adb_path` / `API_url` / `token` / `qwen_api` 都写在文件顶部
- 使用前必须先在 `run.py` 顶部填好这些常量，或设置 `MOBILE_AGENT_V2_CMD` 指向自定义入口；
  仍为空时 wrapper 会直接报错，避免空任务空跑
- 每步打印 `### Decision ###` 块（Thought/Action/Operation），截图在 `screenshot/`；动作 `Stop` 结束

### AppAgent

- 上游：`github.com/TencentQQGYLab/AppAgent`（原 `mnotgod96/AppAgent` 已迁移）
- 入口 `python run.py --app <app>`，其余输入是交互式提示；wrapper 把 `APPAGENT_STDIN`
  （缺省 `y\n<task>\n`）喂给子进程，对应「无文档库 + 单设备」的提示顺序，环境不同请自行设置
- 应用名取 stdin JSON 的 `apps[0]`，没有时读 `APPAGENT_APP`
- 每轮打印 `Round <n>`；日志在 `tasks/task_<app>_<时间戳>/log_*.txt`；结束文案三选一：
  `Task completed successfully` / `Task finished due to reaching max rounds` / `Task finished unexpectedly`

## `_common.py`

负责读 stdin（`task` + 可选 `apps`）、校验目录、启动子进程、计时与按协议输出 JSON。
`status_fn` 只提取上游结束提示，不改写 `success`；`stdin_text` 用于 AppAgent 这类交互式入口。

## 运行

```bash
python experiments/run_experiment.py \
  --tasks experiments/tasks.json --repeat 3 \
  --baseline openautoglm \
  --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
```

结果同样写入 `experiments/results/<时间戳>_<tag>/results.jsonl`，`success` 仍需人工按
`test.md` 第 4 节的标准判定；`upstream_status` 会随 `baseline_raw` 一起落盘，便于复盘。
Level 3 任务如果 baseline 本身不支持多应用规划，按失败记录即可，这正是 `test.md` 里说明的对比点。

## 测试

`tests/test_baseline_wrappers.py` 用假上游覆盖：缺失目录、退出码透传、步骤/摘要统计、
Open-AutoGLM 参数拼装、Mobile-Agent-v2 空配置拒绝与 Stop 识别、AppAgent stdin 喂入与状态解析。
`tests/test_adapter.py` 覆盖 JSON 协议解析与 `apps` 透传。这些测试不依赖真实系统。
