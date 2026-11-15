"""AppAgent 适配器。

上游：https://github.com/TencentQQGYLab/AppAgent（原 mnotgod96/AppAgent 已迁移）
入口：`python run.py --app <应用名>`，交互式读取其余输入（scripts/task_executor.py）：
      1. 无文档库时询问是否继续：`y`
      2. 有文档库时选择文档来源（回车/编号）
      3. 多设备时选择设备 ID
      4. 输入任务描述
      本 wrapper 默认按「无文档库 + 单设备」把 `APPAGENT_STDIN`（缺省 `y\\n<task>\\n`）
      喂给子进程；如果你的环境提示顺序不同，请显式设置 APPAGENT_STDIN。
      app 依次取 stdin JSON 的 apps[0]、APPAGENT_APP。

输出形态：每轮打印 `Round <n>`；日志写到
        tasks/task_<app>_<YYYY-mm-dd_HH-MM-SS>/log_<app>_task_...txt（每行一条 JSON）。
        结束时打印三类之一：
          Task completed successfully / Task finished due to reaching max rounds /
          Task finished unexpectedly
步骤数按 `Round ` 标题行统计；BASELINE_STEP_MARKER 可覆盖。
成功判定：`Task completed successfully` 记为 upstream_status=completed（仅提示），
         success 仍由 experiments 侧人工判定。

离线验证：tests/test_baseline_wrappers.py 用假上游脚本跑通协议。
GPT-4V/Qwen 密钥与真机未在本机实跑。
"""

import os
import sys

from _common import baseline_dir, command_from_env, fail, read_request, run_and_report

DEFAULT_STEP_MARKERS = ["Round ", "[step]"]


def build_command(task: str, root: str, app: str) -> list[str]:
    default = [sys.executable, os.path.join(root, "run.py"), "--app", app]
    return command_from_env("APPAGENT_CMD", default, task)


def parse_status(output: str) -> dict:
    if "Task completed successfully" in output:
        return {"upstream_status": "completed"}
    if "reaching max rounds" in output:
        return {"upstream_status": "max_rounds"}
    if "Task finished unexpectedly" in output:
        return {"upstream_status": "unexpected"}
    return {"upstream_status": "unknown"}


def main() -> int:
    payload = read_request()
    task = str(payload["task"])
    root = baseline_dir("APPAGENT_DIR")
    apps = payload.get("apps") or []
    app = (apps[0] if apps else os.getenv("APPAGENT_APP", "")).strip()
    if not app:
        fail("缺少 AppAgent 目标应用：请在 stdin JSON 里给 apps，或设置 APPAGENT_APP")
    marker = os.getenv("BASELINE_STEP_MARKER", "").strip() or DEFAULT_STEP_MARKERS
    stdin_text = os.getenv("APPAGENT_STDIN") or f"y\n{task}\n"
    return run_and_report("AppAgent", build_command(task, root, app), cwd=root,
                          step_marker=marker, stdin_text=stdin_text,
                          status_fn=parse_status)


if __name__ == "__main__":
    raise SystemExit(main())
