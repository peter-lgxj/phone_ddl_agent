from __future__ import annotations

"""
baseline 适配层

把外部手机智能体（Open-AutoGLM / Mobile-Agent-v2 / AppAgent 等）包装成与本项目一致的最小协议，
使 experiments/run_experiment.py 能以同一套任务集、同一套指标采集它们的结果。

协议（子进程）
  输入：stdin 一行 JSON，如 {"task": "打开微信发送..."}
  输出：stdout 最后一行 JSON，如 {"summary": "...", "steps": 12, "elapsed": 45.6}
        - summary 必填（可为空字符串）
        - steps / elapsed 可缺省；elapsed 缺省时由适配器自己计时
  退出码：0 表示执行结束，非 0 视为失败

注意：本文件只提供接口与通用子进程实现，不包含任何 baseline 的具体集成；
各系统的启动方式不同，需要为它们各写一个符合上述协议的 wrapper 脚本。
"""

import abc
import json
import os
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class BaselineResult:
    summary: str
    steps: int | None = None
    elapsed: float = 0.0
    raw: dict = field(default_factory=dict)


class BaselineAdapter(abc.ABC):
    """所有 baseline 接入实现的基类"""

    name = "baseline"

    @abc.abstractmethod
    def run(self, task: str) -> BaselineResult:
        ...


class SubprocessAdapter(BaselineAdapter):
    """通用子进程适配器：命令自行负责驱动 baseline，并按协议输出 JSON"""

    def __init__(self, name: str, command: list[str], cwd: str | None = None,
                 timeout: int = 900, env: dict | None = None):
        self.name = name
        self.command = command
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout
        self.env = env

    def run(self, task: str) -> BaselineResult:
        started = time.time()
        proc = subprocess.run(
            self.command,
            input=json.dumps({"task": task}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.cwd,
            timeout=self.timeout,
            env=self.env,
        )
        elapsed = round(time.time() - started, 1)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.name} 退出码 {proc.returncode}：{(proc.stderr or proc.stdout).strip()[:500]}"
            )

        payload = {}
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

        return BaselineResult(
            summary=str(payload.get("summary", "")),
            steps=payload.get("steps"),
            elapsed=float(payload.get("elapsed", elapsed)),
            raw=payload,
        )
