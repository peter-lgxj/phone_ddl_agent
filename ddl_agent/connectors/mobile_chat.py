"""手机 GUI 自动化聊天连接器。

复用现有手机自动化层（``ScreenAgent`` + OCR/控件树 + 留证目录）读取微信/QQ 聊天。
读屏结果由 ``mobile_reading`` 统一拆分：真实发送者/时间能解析就填，解析不出来就置空
并标记 ``needs_review``；到达步数上限时返回部分结果，不丢弃已归档观察。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import RawMessage, RetrievalPlan, SourceType
from ..settings import TZ
from .base import SourceError, SourceNotConfigured, message_in_plan
from .mobile_reading import build_mobile_messages


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
        # 最近一次 fetch 的完成度：timeout 时仍返回已归档的观察，但标记 partial
        self.partial = False
        self.last_status: str | None = None
        self.last_fetch_meta: dict[str, Any] = {}

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        self.partial = False
        self.last_status = None
        self.last_fetch_meta = {}
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
        self.last_status = status
        if status == "failed":
            raise SourceError(f"手机读取失败（status={status}）")
        if status == "timeout":
            # 到达步数上限不等于没读到：把已归档的观察交给上层，由调用方决定是否使用
            self.partial = True

        messages = [
            message
            for message in self._messages_from_steps(agent)
            if message_in_plan(message, plan)
        ]
        self.last_fetch_meta = {
            "partial": self.partial,
            "last_status": status,
            "message_count": len(messages),
        }
        return messages

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
        return build_mobile_messages(
            agent,
            source_name=self.name,
            source_type=self.source_type,
            subject=f"{self.app} / {self.contact}",
            thread_id=f"{self.app}:{self.contact}",
            message_prefix=f"{self.name}-",
        )

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
