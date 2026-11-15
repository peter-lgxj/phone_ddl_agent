"""Open-AutoGLM 适配器。

上游：https://github.com/zai-org/Open-AutoGLM
入口：`python main.py [--device-id <serial>] [--max-steps N] <task>`
      task 是位置参数，不是 --task；其余模型参数用上游自己的环境变量/参数
      （--base-url / --model / --apikey，或 PHONE_AGENT_* 环境变量）。

本 wrapper 的命令构造：
  python <root>/main.py [--device-id $ANDROID_SERIAL] [--max-steps $OPEN_AUTOGLM_MAX_STEPS] <task>
  可用 OPEN_AUTOGLM_CMD 整体覆盖（推荐，便于按上游变更调整）。

输出形态：verbose 模式下每步打印 `💭 思考：` / `🎯 动作：` 块（默认 verbose）；
结束时 main.py 打印 `Result: <message>`，达到上限时为 `Result: Max steps reached`。
步骤数按 `🎯` 行统计；BASELINE_STEP_MARKER 可覆盖。
成功判定：上游不输出结构化成功标记，只把 `Result:` 文本记为 upstream_status/upstream_result，
         success 仍由 experiments 侧人工判定。

离线验证：tests/test_baseline_wrappers.py 用假上游脚本跑通协议，不依赖真实安装。
真机与模型服务未在本机实跑。
"""

import os
import sys

from _common import baseline_dir, command_from_env, read_request, run_and_report

DEFAULT_STEP_MARKERS = ["🎯", "[step]"]


def build_command(task: str, root: str, device_id: str = "") -> list[str]:
    default = [sys.executable, os.path.join(root, "main.py")]
    if device_id:
        default += ["--device-id", device_id]
    max_steps = os.getenv("OPEN_AUTOGLM_MAX_STEPS", "").strip()
    if max_steps:
        default += ["--max-steps", max_steps]
    default.append(task)
    return command_from_env("OPEN_AUTOGLM_CMD", default, task)


def parse_status(output: str) -> dict:
    """从 `Result:` 行提取结束原因；不参与 success 判定"""
    results = [ln.strip() for ln in output.splitlines() if ln.strip().startswith("Result:")]
    if not results:
        return {"upstream_status": "unknown"}
    text = results[-1][len("Result:"):].strip()
    status = "max_steps" if text.lower().startswith("max steps reached") else "finished"
    return {"upstream_status": status, "upstream_result": text}


def main() -> int:
    payload = read_request()
    task = str(payload["task"])
    root = baseline_dir("OPEN_AUTOGLM_DIR")
    marker = os.getenv("BASELINE_STEP_MARKER", "").strip() or DEFAULT_STEP_MARKERS
    device = os.getenv("ANDROID_SERIAL", "").strip()
    return run_and_report("Open-AutoGLM", build_command(task, root, device), cwd=root,
                          step_marker=marker, status_fn=parse_status)


if __name__ == "__main__":
    raise SystemExit(main())
