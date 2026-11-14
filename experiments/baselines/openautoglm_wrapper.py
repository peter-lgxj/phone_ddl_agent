"""Open-AutoGLM 接入骨架（未在真实仓库上验证）。

用法：
  1. 按上游 README 克隆并安装 Open-AutoGLM
  2. 设置环境变量 OPEN_AUTOGLM_DIR=<仓库根目录>
  3. 按上游实际入口调整 build_command() 与 step_marker
  4. python experiments/run_experiment.py --baseline openautoglm \
       --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
"""

import sys

from _common import baseline_dir, read_task, run_and_report


def build_command(task: str, root: str) -> list[str]:
    # TODO: 换成上游真实的入口脚本与参数；{task} 会被替换为任务文本
    return [sys.executable, f"{root}/main.py", "--task", task]


def main() -> int:
    task = read_task()
    root = baseline_dir("OPEN_AUTOGLM_DIR")
    return run_and_report("Open-AutoGLM", build_command(task, root), cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
