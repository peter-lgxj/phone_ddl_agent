"""手机 GUI 数据源：微信 / QQ / Outlook App，按目标名称定位会话。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from ..connectors.base import SourceError
from ..models import RawMessage, RetrievalPlan
from ..settings import TZ
from .config import APP_LABELS, MonitorConfig, MonitorSource

_NOT_FOUND_STATUS = {"not_found", "target_not_found", "missing_target"}


class TargetNotFound(SourceError):
    """App 内找不到用户指定的群/联系人/邮箱文件夹。"""


class MobileAppSource:
    """复用现有手机智能体读屏；本实现**未经真机验证**。"""

    def __init__(self, source: MonitorSource, agent: Any | None = None,
                 agent_factory: Any | None = None, use_device_lock: bool = True,
                 now: datetime | None = None):
        self.source = source
        self.name = source.id
        self.source_type = source.source_type
        self.agent = agent
        self.agent_factory = agent_factory
        self.use_device_lock = use_device_lock
        self.now = now

    def fetch(self, plan: RetrievalPlan | None = None) -> list[RawMessage]:
        agent = self._get_agent()
        task = self._build_task()

        if self.use_device_lock:
            try:
                from device_lock import device_lock
            except ImportError as exc:
                raise SourceError(f"设备锁不可用：{exc}") from exc
            with device_lock(timeout=30) as acquired:
                if not acquired:
                    raise SourceError("设备锁被占用，本轮未读取手机")
                agent.run(task)
        else:
            agent.run(task)

        status = str(getattr(agent, "last_status", "") or "").lower()
        if status in ("failed", "timeout"):
            raise SourceError(f"手机读取未完成（status={status}）")
        if status in _NOT_FOUND_STATUS or self._mentions_not_found(agent):
            raise TargetNotFound(
                f"未找到目标「{self.source.target}」（{APP_LABELS.get(self.source.app, self.source.app)}）"
            )

        return self._messages_from_steps(agent)

    # ------------------------------------------------------------------ #
    def _build_task(self) -> str:
        app = APP_LABELS.get(self.source.app, self.source.app)
        kind = {"group": "群聊", "private": "联系人", "inbox": "收件箱"}.get(
            self.source.kind, "会话"
        )
        return (
            f"打开{app}，通过搜索框输入「{self.source.target}」找到并进入该{kind}；"
            "如果列表里没有，请继续滚动查找；仍然找不到就明确报告“未找到”，不要打开其他会话。\n"
            f"进入后向上翻阅最近 {self.source.lookback_days} 天的消息，逐条读出所有包含截止时间"
            "（deadline / due / DDL / 截止 / 提交）的消息原文、发送者和时间。\n"
            "只记录属于该会话的消息，不要猜测，不要修改任何内容。"
        )

    def _get_agent(self):
        if self.agent is not None:
            return self.agent
        if self.agent_factory is not None:
            self.agent = self.agent_factory()
            return self.agent
        try:
            from screen_agent import ScreenAgent
        except ImportError as exc:
            raise SourceError(f"手机自动化层不可用：{exc}") from exc
        self.agent = ScreenAgent()
        return self.agent

    def _messages_from_steps(self, agent) -> list[RawMessage]:
        memory = getattr(agent, "last_memory", None)
        steps = list(getattr(memory, "steps", []) or [])
        run_dir = Path(getattr(agent, "run_dir", "") or ".")
        now = self.now or datetime.now(TZ)
        messages: list[RawMessage] = []
        seen: set[str] = set()

        for index, step in enumerate(steps, start=1):
            observation = str(step.get("observation") or "").strip()
            if not observation:
                continue
            digest = hashlib.sha1(f"{self.source.id}|{observation}".encode("utf-8")).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            step_no = int(step.get("step") or index)
            evidence = run_dir / f"step_{step_no:03d}.json"
            messages.append(
                RawMessage(
                    message_id=digest,
                    source_name=self.source.target,
                    source_type=self.source.source_type,
                    sender=str(step.get("sender") or self.source.target),
                    published_at=now,
                    subject=f"{APP_LABELS.get(self.source.app, self.source.app)} / {self.source.target}",
                    body=observation,
                    evidence_path=str(evidence) if evidence.exists() else None,
                    thread_id=self.source.id,
                )
            )
        return messages

    @staticmethod
    def _mentions_not_found(agent) -> bool:
        memory = getattr(agent, "last_memory", None)
        steps = list(getattr(memory, "steps", []) or [])
        text = " ".join(str(step.get("observation") or "") for step in steps[-2:])
        return "未找到" in text or "not found" in text.lower()


def build_sources(config: MonitorConfig, agent_factory: Any | None = None,
                  use_device_lock: bool = True) -> list[MobileAppSource]:
    return [
        MobileAppSource(source, agent_factory=agent_factory, use_device_lock=use_device_lock)
        for source in config.sources
        if source.enabled
    ]
