"""AppAgent 适配器。

真实接入步骤：
  1. 按上游 README 安装 AppAgent
  2. 设置 APPAGENT_DIR=<仓库根目录>
  3. 用 APPAGENT_CMD 覆盖入口命令，例如：
     APPAGENT_CMD="python run.py --task {task} --app wechat"
  4. 上游若用不同步数标记，设置 BASELINE_STEP_MARKER

离线验证：tests/test_baselines.py 用假上游脚本跑协议。
"""

import os
import sys

from _common import baseline_dir, command_from_env, read_task, run_and_report


def build_command(task: str, root: str) -> list[str]:
    default = [sys.executable, os.path.join(root, "run.py"), "--task", task]
    return command_from_env("APPAGENT_CMD", default, task)


def main() -> int:
    task = read_task()
    root = baseline_dir("APPAGENT_DIR")
    marker = os.getenv("BASELINE_STEP_MARKER", "[step]")
    return run_and_report("AppAgent", build_command(task, root), cwd=root, step_marker=marker)


if __name__ == "__main__":
    raise SystemExit(main())
