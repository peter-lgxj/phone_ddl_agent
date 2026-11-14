# baseline 接入说明

本目录只提供**适配层**，不包含任何对比系统的代码。三个对比系统需要各自克隆、按其官方方式安装，
然后写一个符合下面协议的 wrapper，交给 `experiments/run_experiment.py` 统一调度。

## 协议

wrapper 必须：

1. 从 stdin 读取一行 JSON：`{"task": "任务描述"}`
2. 驱动对应系统执行该任务
3. 在 stdout 最后一行输出 JSON：`{"summary": "...", "steps": 12, "elapsed": 45.6}`
   - `summary` 必填；`steps` / `elapsed` 可省略（省略时由适配器计时）
4. 正常结束退出码为 0，异常为非 0

## 建议的接入方式

| 系统 | 仓库 | wrapper 需要做的事 |
|---|---|---|
| Open-AutoGLM | `github.com/zai-org/Open-AutoGLM` | 调它的 CLI/脚本，传入任务文本，读取其 ADB 操作日志统计步数 |
| Mobile-Agent-v2 | `github.com/X-PLUG/MobileAgent` | 按其 README 启动多智能体流程，输出最终文本与步数 |
| AppAgent | `github.com/TencentQQGYLab/AppAgent` | 走它的 explore/execute 流程，注意它需要 App 文档，需提前对齐 |

伪代码（`openautoglm_wrapper.py` 放到本目录或任意位置均可）：

```python
import json, subprocess, sys, time

payload = json.loads(sys.stdin.readline())
started = time.time()
proc = subprocess.run(["python", r"F:\baselines\Open-AutoGLM\main.py",
                       "--task", payload["task"]], capture_output=True, text=True)
steps = proc.stdout.count("[step]")          # 按各系统日志格式调整
print(json.dumps({"summary": proc.stdout.strip()[-500:],
                  "steps": steps,
                  "elapsed": round(time.time() - started, 1)}, ensure_ascii=False))
```

## 运行

```bash
python experiments/run_experiment.py \
  --tasks experiments/tasks.json --repeat 3 \
  --baseline openautoglm \
  --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
```

结果同样写入 `experiments/results/<时间戳>_<tag>/results.jsonl`，`success` 仍需人工按
`test.md` 第 4 节的标准判定。Level 3 任务如果 baseline 本身不支持多应用规划，按失败记录即可，
这正是 `test.md` 里说明的对比点。
