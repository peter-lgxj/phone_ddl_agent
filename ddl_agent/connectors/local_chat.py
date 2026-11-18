"""本地聊天归档连接器：JSON / JSONL，模拟微信、QQ 群聊，离线可跑。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models import RawMessage, RetrievalPlan, SourceType
from ..settings import TZ
from .base import SourceError, message_in_plan

_ROLE_TO_SOURCE: dict[str, SourceType] = {
    "instructor": "instructor_chat",
    "teacher": "instructor_chat",
    "ta": "instructor_chat",
    "student": "student_chat",
    "classmate": "student_chat",
    "announcement": "course_announcement",
    "system": "course_announcement",
}


class LocalChatConnector:
    """读取聊天归档文件。

    支持两种格式：
    - ``{"chat": "...", "messages": [{sender, role, timestamp, text}]}``
    - 每行一个消息对象的 JSONL
    """

    def __init__(self, name: str, path: str | Path,
                 source_type: SourceType = "student_chat"):
        self.name = name
        self.path = Path(path)
        self.source_type = source_type

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        if not self.path.exists():
            raise SourceError(f"聊天归档不存在：{self.path}")

        messages: list[RawMessage] = []
        for index, item in enumerate(self._load(), start=1):
            message = self._to_message(item, index)
            if message_in_plan(message, plan):
                messages.append(message)
        return messages

    def _load(self) -> list[dict]:
        text = self.path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                return [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise SourceError(f"聊天归档格式非法：{self.path}：{exc}") from exc

        if isinstance(data, dict):
            return list(data.get("messages") or [])
        if isinstance(data, list):
            return data
        raise SourceError(f"聊天归档格式非法：{self.path}")

    def _to_message(self, item: dict, index: int) -> RawMessage:
        role = str(item.get("role") or "student").lower()
        source_type = item.get("source_type") or _ROLE_TO_SOURCE.get(role, self.source_type)
        timestamp = item.get("timestamp") or item.get("time") or item.get("published_at")
        try:
            published_at = datetime.fromisoformat(str(timestamp))
        except (TypeError, ValueError) as exc:
            raise SourceError(f"聊天消息时间非法：{timestamp!r}") from exc
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=TZ)

        return RawMessage(
            message_id=str(item.get("message_id") or f"{self.name}-{index}"),
            source_name=self.name,
            source_type=source_type,
            sender=str(item.get("sender") or "unknown"),
            published_at=published_at,
            subject=str(item.get("subject") or ""),
            body=str(item.get("text") or ""),
            evidence_path=str(self.path),
            thread_id=item.get("thread_id"),
        )
