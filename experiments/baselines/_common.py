"""baseline wrapper 的公共实现。

三个 wrapper 只负责「读任务 → 调外部命令 → 按协议输出 JSON」，
所有系统差异都集中在各自的 build_command() 里，便于按上游 README 调整。
"""

import json
import os
import shlex
import subprocess
import sys
import time


def fail(message: str):
    print(f"[baseline] {message}", file=sys.stderr)
    raise SystemExit(2)


def command_from_env(env_name: str, default: list[str], task: str) -> list[str]:
    """优先用环境变量覆盖完整命令，便于适配上游不同入口。

    env 示例：
      OPEN_AUTOGLM_CMD="python main.py --task {task} --device emulator-5554"
    未设置时回退到 default。
    """
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    raw = raw.replace("{task}", task)
    return shlex.split(raw, posix=(os.name != "nt"))


def read_task() -> str:
    line = sys.stdin.readline()
    try:
        payload = json.loads(line) if line.strip() else {}
    except json.JSONDecodeError as e:
        fail(f"stdin 不是合法 JSON：{e}")
    task = str(payload.get("task", "")).strip()
    if not task:
        fail("stdin 缺少 task 字段")
    return task


def baseline_dir(env_name: str) -> str:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        fail(f"未设置环境变量 {env_name}（指向 baseline 仓库根目录）")
    if not os.path.isdir(raw):
        fail(f"{env_name}={raw} 不是目录")
    return raw


def run_and_report(name: str, cmd: list[str], cwd: str,
                   step_marker: str = "[step]", timeout: int = 900) -> int:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as e:
        fail(f"启动 {name} 失败：{e}")
    except subprocess.TimeoutExpired:
        fail(f"{name} 执行超时（{timeout}s）")

    # 两个流都要保留，否则上游把错误写到 stderr 时会丢诊断信息
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    steps = sum(1 for line in (proc.stdout or "").splitlines() if step_marker in line)

    print(json.dumps({
        "summary": output[-500:],
        "steps": steps or None,
        "elapsed": round(time.time() - started, 1),
        "returncode": proc.returncode,
        "command": cmd,
    }, ensure_ascii=False))
    # 透传退出码：上游崩溃必须让 SubprocessAdapter 判为失败，而不是记为正常结束
    return proc.returncode
