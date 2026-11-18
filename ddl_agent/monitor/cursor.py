"""增量状态：来源游标、已抓消息库、已抽取候选库。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import DeadlineCandidate, RawMessage
from ..settings import TZ


@dataclass
class Cursor:
    last_timestamp: str | None = None
    last_message_id: str | None = None
    baseline_done: bool = False


class CursorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Cursor] = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for source_id, item in (payload.get("sources") or {}).items():
                self._data[source_id] = Cursor(
                    last_timestamp=item.get("last_timestamp"),
                    last_message_id=item.get("last_message_id"),
                    baseline_done=bool(item.get("baseline_done")),
                )

    def get(self, source_id: str) -> Cursor:
        return self._data.get(source_id, Cursor())

    def advance(self, source_id: str, messages: list[RawMessage]) -> Cursor:
        cursor = self.get(source_id)
        dated = [message for message in messages if message.published_at is not None]
        if dated:
            latest = max(dated, key=lambda message: (message.published_at, message.message_id))
            timestamp = latest.published_at.astimezone(TZ).isoformat(timespec="seconds")
            if cursor.last_timestamp is None or timestamp >= cursor.last_timestamp:
                cursor.last_timestamp = timestamp
                cursor.last_message_id = latest.message_id
        # 没有任何可解析发布时间时保持游标不动，避免把“本轮”刷成伪时间
        cursor.baseline_done = True
        self._data[source_id] = cursor
        self.save()
        return cursor

    def reset(self, source_id: str | None = None) -> None:
        if source_id:
            self._data.pop(source_id, None)
        else:
            self._data.clear()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sources": {
                source_id: {
                    "last_timestamp": cursor.last_timestamp,
                    "last_message_id": cursor.last_message_id,
                    "baseline_done": cursor.baseline_done,
                }
                for source_id, cursor in self._data.items()
            },
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class MessageStore:
    """跨轮保留原始消息；按 message_id 去重。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._messages: dict[str, RawMessage] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                message = _message_from_dict(item)
                self._messages[message.message_id] = message

    def __len__(self) -> int:
        return len(self._messages)

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def add(self, messages: list[RawMessage]) -> list[RawMessage]:
        fresh = [message for message in messages if message.message_id not in self._messages]
        if not fresh:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for message in fresh:
                self._messages[message.message_id] = message
                handle.write(json.dumps(_message_to_dict(message), ensure_ascii=False) + "\n")
        return fresh


class CandidateStore:
    """跨轮保留候选；LLM 只抽取新消息，旧候选直接复用。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._candidates: dict[str, DeadlineCandidate] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                candidate = _candidate_from_dict(json.loads(line))
                self._candidates[candidate.candidate_id] = candidate

    def add(self, candidates: list[DeadlineCandidate]) -> None:
        fresh = [item for item in candidates if item.candidate_id not in self._candidates]
        if not fresh:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for candidate in fresh:
                self._candidates[candidate.candidate_id] = candidate
                handle.write(json.dumps(_candidate_to_dict(candidate), ensure_ascii=False) + "\n")

    def all(self) -> list[DeadlineCandidate]:
        return list(self._candidates.values())


def _message_to_dict(message: RawMessage) -> dict:
    return {
        "message_id": message.message_id,
        "source_name": message.source_name,
        "source_type": message.source_type,
        "sender": message.sender,
        "published_at": message.published_at.isoformat() if message.published_at else None,
        "subject": message.subject,
        "body": message.body,
        "evidence_path": message.evidence_path,
        "thread_id": message.thread_id,
        "needs_review": message.needs_review,
        "review_reason": message.review_reason,
    }


def _message_from_dict(item: dict) -> RawMessage:
    return RawMessage(
        message_id=item["message_id"],
        source_name=item["source_name"],
        source_type=item["source_type"],
        sender=item.get("sender") or "",
        published_at=(
            datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None
        ),
        subject=item.get("subject") or "",
        body=item.get("body") or "",
        evidence_path=item.get("evidence_path"),
        thread_id=item.get("thread_id"),
        needs_review=bool(item.get("needs_review")),
        review_reason=item.get("review_reason") or "",
    )


def _candidate_to_dict(candidate: DeadlineCandidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "task_name_raw": candidate.task_name_raw,
        "course": candidate.course,
        "deadline_raw": candidate.deadline_raw,
        "deadline_iso": candidate.deadline_iso,
        "source_type": candidate.source_type,
        "source_name": candidate.source_name,
        "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
        "message_id": candidate.message_id,
        "message_subject": candidate.message_subject,
        "missing_fields": candidate.missing_fields,
        "unresolved_time": candidate.unresolved_time,
        "extractor_confidence": candidate.extractor_confidence,
        "needs_review": candidate.needs_review,
        "review_reason": candidate.review_reason,
    }


def _candidate_from_dict(item: dict) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=item["candidate_id"],
        task_name_raw=item["task_name_raw"],
        course=item.get("course"),
        deadline_raw=item.get("deadline_raw"),
        deadline_iso=item.get("deadline_iso"),
        source_type=item["source_type"],
        source_name=item["source_name"],
        published_at=(
            datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None
        ),
        message_id=item["message_id"],
        message_subject=item.get("message_subject") or "",
        missing_fields=item.get("missing_fields") or [],
        unresolved_time=bool(item.get("unresolved_time")),
        extractor_confidence=item.get("extractor_confidence"),
        needs_review=bool(item.get("needs_review")),
        review_reason=item.get("review_reason") or "",
    )
