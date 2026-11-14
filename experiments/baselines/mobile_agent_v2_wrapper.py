"""Mobile-Agent-v2 接入骨架（未在真实仓库上验证）。

用法：
  1. 按上游 README 克隆并安装 MobileAgent
  2. 设置环境变量 MOBILE_AGENT_V2_DIR=<仓库根目录>
  3. 按上游实际入口调整 build_command() 与 step_marker
  4. 通过 --baseline mobile_agent_v2 交给 run_experiment.py
"""

import sys

from _common import baseline_dir, read_task, run_and_report


def build_command(task: str, root: str) -> list[str]:
    # TODO: 换成上游真实入口；Mobile-Agent-v2 通常以配置文件 + 轮次调度启动
    return [sys.executable, f"{root}/run.py", "--instruction", task]


def main() -> int:
    task = read_task()
    root = baseline_dir("MOBILE_AGENT_V2_DIR")
    return run_and_report("Mobile-Agent-v2", build_command(task, root), cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
