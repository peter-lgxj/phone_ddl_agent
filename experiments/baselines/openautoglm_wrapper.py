"""Open-AutoGLM 适配器。

真实接入步骤：
  1. 按上游 README 克隆并安装 Open-AutoGLM 到任意目录
  2. 设置 OPEN_AUTOGLM_DIR=<仓库根目录>
  3. 用 OPEN_AUTOGLM_CMD 覆盖入口命令（推荐，按上游 README 填）：
     OPEN_AUTOGLM_CMD="python main.py --task {task}"
     未设置时回退到 `python <root>/main.py --task <task>`
  4. 若上游输出格式不同，设置 BASELINE_STEP_MARKER 或改 run_and_report 的 step_marker

离线验证：tests/test_baselines.py 用假上游脚本跑通协议，不依赖真实安装。
"""

import os
import sys

from _common import baseline_dir, command_from_env, read_task, run_and_report


def build_command(task: str, root: str) -> list[str]:
    default = [sys.executable, os.path.join(root, "main.py"), "--task", task]
    return command_from_env("OPEN_AUTOGLM_CMD", default, task)


def main() -> int:
    task = read_task()
    root = baseline_dir("OPEN_AUTOGLM_DIR")
    marker = os.getenv("BASELINE_STEP_MARKER", "[step]")
    return run_and_report("Open-AutoGLM", build_command(task, root), cwd=root, step_marker=marker)


if __name__ == "__main__":
    raise SystemExit(main())
