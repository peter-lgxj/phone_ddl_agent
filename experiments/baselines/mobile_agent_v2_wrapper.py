"""Mobile-Agent-v2 适配器。

上游：https://github.com/X-PLUG/MobileAgent（Mobile-Agent-v2/ 目录）
入口：`python run.py`，**没有命令行参数**。任务、adb 路径、模型 API 都是
      run.py 顶部的模块级常量（instruction / adb_path / API_url / token / qwen_api）。
      因此实验侧必须二选一：
        1. 先在 run.py 顶部填好配置，再用本 wrapper（默认命令 `python run.py`）；
        2. 用 MOBILE_AGENT_V2_CMD 提供自定义入口（例如改写过的脚本），命令里可用 {task}。
      检测到 run.py 仍是 `instruction = ""` 时直接失败并给出提示，避免空任务空跑。

输出形态：每一步打印 `### Decision ###` 块（含 Thought/Action/Operation），
        截图写到 ./screenshot/，OCR 中间结果写到 ./temp/。
        动作 Stop 表示结束（run.py 内部 break），但不会打印结构化结果。
步骤数按 ` Decision ` 标题行统计；BASELINE_STEP_MARKER 可覆盖。
成功判定：上游不提供成功标记，这里只记录是否出现 Stop 动作（upstream_status=stopped），
         success 仍由 experiments 侧人工判定（建议结合 screenshot/ 复盘）。

离线验证：tests/test_baseline_wrappers.py 用假上游脚本跑通协议。
依赖（torch/modelscope/GroundingDINO）较重，本机未安装，未实跑。
"""

import os
import re
import sys

from _common import baseline_dir, command_from_env, fail, read_request, run_and_report

DEFAULT_STEP_MARKERS = [" Decision ", "[step]"]
PLACEHOLDER = re.compile(r'^\s*instruction\s*=\s*""\s*$', re.MULTILINE)


def build_command(task: str, root: str) -> list[str]:
    default = [sys.executable, os.path.join(root, "run.py")]
    return command_from_env("MOBILE_AGENT_V2_CMD", default, task)


def instruction_is_placeholder(root: str) -> bool:
    path = os.path.join(root, "run.py")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return PLACEHOLDER.search(f.read()) is not None
    except OSError:
        return False


def parse_status(output: str) -> dict:
    if re.search(r"###\s*Action\s*###\s*\n+\s*Stop\b", output):
        return {"upstream_status": "stopped"}
    return {"upstream_status": "unknown"}


def main() -> int:
    payload = read_request()
    task = str(payload["task"])
    root = baseline_dir("MOBILE_AGENT_V2_DIR")
    if not os.getenv("MOBILE_AGENT_V2_CMD", "").strip() and instruction_is_placeholder(root):
        fail("Mobile-Agent-v2 的 run.py 没有命令行参数，且 instruction 仍为空。"
             "请先在 run.py 顶部填写 instruction/adb_path/API_url/token，"
             "或设置 MOBILE_AGENT_V2_CMD 指向自定义入口（支持 {task} 占位）。")
    marker = os.getenv("BASELINE_STEP_MARKER", "").strip() or DEFAULT_STEP_MARKERS
    return run_and_report("Mobile-Agent-v2", build_command(task, root), cwd=root,
                          step_marker=marker, status_fn=parse_status)


if __name__ == "__main__":
    raise SystemExit(main())
