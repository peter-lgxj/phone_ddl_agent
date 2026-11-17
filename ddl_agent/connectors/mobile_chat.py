"""手机 GUI 自动化聊天连接器。

复用现有手机自动化层（``ScreenAgent`` + OCR/控件树 + 留证目录）读取微信/QQ 聊天。

**状态：未经真机验证。** 设备已拔线，当前只保证接口与解析逻辑可用；
真正跑通需要手机在线、微信/QQ 已登录，并建议外层套 ``device_lock``。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import RawMessage, RetrievalPlan, SourceType
from ..settings import TZ
from .base import SourceError, SourceNotConfigured, message_in_plan


class MobileChatConnector:
    """通过 ScreenAgent 在真机上读取某个会话，并把观察结果转成 ``RawMessage``。"""

    def __init__(self, name: str, contact: str, app: str = "微信",
                 agent: Any | None = None, agent_factory: Any | None = None,
                 source_type: SourceType = "student_chat",
                 use_device_lock: bool = True):
        self.name = name
        self.contact = contact
        self.app = app
        self.agent = agent
        self.agent_factory = agent_factory
        self.source_type = source_type
        self.use_device_lock = use_device_lock

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        agent = self._get_agent()
        task = (
            f"打开{self.app}，进入与「{self.contact}」的聊天窗口，向上翻阅最近消息，"
            "逐条读出包含截止时间（deadline / due / 截止 / 提交）的消息原文与发送者"
        )

        if self.use_device_lock:
            try:
                from device_lock import device_lock  # 复用仓库根目录的手机层实现
            except ImportError as exc:  # 非手机仓库环境下不强行依赖
                raise SourceNotConfigured(f"设备锁模块不可用：{exc}") from exc
            with device_lock(timeout=30) as acquired:
                if not acquired:
                    raise SourceError("设备锁被占用，未执行真机读取")
                agent.run(task)
        else:
            agent.run(task)

        status = getattr(agent, "last_status", None)
        if status in ("failed", "timeout"):
            raise SourceError(f"手机读取未完成（status={status}）")

        return [
            message
            for message in self._messages_from_steps(agent)
            if message_in_plan(message, plan)
        ]

    def _get_agent(self):
        if self.agent is not None:
            return self.agent
        if self.agent_factory is not None:
            self.agent = self.agent_factory()
            return self.agent
        try:
            from screen_agent import ScreenAgent
        except ImportError as exc:
            raise SourceNotConfigured(f"手机自动化层不可用：{exc}") from exc
        self.agent = ScreenAgent()
        return self.agent

    def _messages_from_steps(self, agent) -> list[RawMessage]:
        memory = getattr(agent, "last_memory", None)
        steps = list(getattr(memory, "steps", []) or [])
        run_dir = Path(getattr(agent, "run_dir", "") or ".")
        now = datetime.now(TZ)
        messages: list[RawMessage] = []

        for step in steps:
            observation = str(step.get("observation") or "").strip()
            if not observation:
                continue
            step_no = int(step.get("step") or len(messages) + 1)
            evidence = run_dir / f"step_{step_no:03d}.json"
            messages.append(
                RawMessage(
                    message_id=f"{self.name}-step-{step_no:03d}",
                    source_name=self.name,
                    source_type=self.source_type,
                    sender=str(step.get("action", {}).get("sender") or self.contact),
                    published_at=now,
                    subject=f"{self.app} / {self.contact}",
                    body=observation,
                    evidence_path=str(evidence) if evidence.exists() else self._run_json(run_dir),
                    thread_id=f"{self.app}:{self.contact}",
                )
            )
        return messages

    @staticmethod
    def _run_json(run_dir: Path) -> str | None:
        """没有单步文件时，退到运行目录里的汇总日志。"""
        candidates = sorted(run_dir.glob("*.json")) if run_dir.exists() else []
        return str(candidates[-1]) if candidates else None


def load_mobile_messages(json_path: str | Path) -> list[RawMessage]:
    """把离线导出的真机读取结果（JSON）转成 ``RawMessage``，便于不上手机时回放。"""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    items = data.get("messages", []) if isinstance(data, dict) else data
    messages: list[RawMessage] = []
    for index, item in enumerate(items, start=1):
        published_at = datetime.fromisoformat(str(item.get("timestamp")))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=TZ)
        messages.append(
            RawMessage(
                message_id=str(item.get("message_id") or f"mobile-{index}"),
                source_name=str(item.get("source_name") or "mobile-chat"),
                source_type=item.get("source_type") or "student_chat",
                sender=str(item.get("sender") or "unknown"),
                published_at=published_at,
                subject=str(item.get("subject") or ""),
                body=str(item.get("text") or ""),
                evidence_path=item.get("evidence_path"),
            )
        )
    return messages
