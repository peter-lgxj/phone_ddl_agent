"""增量状态：来源游标、已抓消息库、已抽取候选库。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from ..extract.message_context import detect_correction
from ..models import DeadlineCandidate, RawMessage
from ..settings import TZ


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再替换，进程中断不会留下半截状态文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                # 游标损坏只影响增量优化：从空游标重建，不阻断整轮检索
                print(f"  ⚠️  游标文件损坏，按空游标重新建立：{exc}")
                payload = {}
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
        _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))


class MessageStore:
    """跨轮保留原始消息；按 message_id 去重。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._messages: dict[str, RawMessage] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    message = _message_from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    # 单行损坏（例如写入中断）只丢这一条，不阻断整轮检索
                    print(f"  ⚠️  跳过损坏的消息行：{exc}")
                    continue
                self._messages[message.message_id] = message

    def __len__(self) -> int:
        return len(self._messages)

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def get(self, message_id: str) -> RawMessage | None:
        return self._messages.get(message_id)

    def all(self) -> list[RawMessage]:
        return list(self._messages.values())

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
                try:
                    candidate = _candidate_from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    print(f"  ⚠️  跳过损坏的候选行：{exc}")
                    continue
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


def _json_value(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _message_to_dict(message: RawMessage) -> dict:
    """按 dataclass 字段全量序列化；新增字段不会再被静默丢弃。"""
    return {field.name: _json_value(getattr(message, field.name))
            for field in fields(message)}


def _message_from_dict(item: dict) -> RawMessage:
    kwargs = {}
    for field in fields(RawMessage):
        if field.name not in item:
            continue
        value = item[field.name]
        if field.name in {"published_at", "observed_at"} and isinstance(value, str):
            value = datetime.fromisoformat(value)
        kwargs[field.name] = value
    return RawMessage(**kwargs)


def _candidate_to_dict(candidate: DeadlineCandidate) -> dict:
    """按 dataclass 字段全量序列化，避免 is_correction/source_order 等审计字段丢失。"""
    return {field.name: _json_value(getattr(candidate, field.name))
            for field in fields(candidate)}


def _candidate_from_dict(item: dict) -> DeadlineCandidate:
    kwargs = {}
    for field in fields(DeadlineCandidate):
        if field.name not in item:
            continue
        value = item[field.name]
        if field.name in {"published_at", "observed_at"} and isinstance(value, str):
            value = datetime.fromisoformat(value)
        kwargs[field.name] = value
    return DeadlineCandidate(**kwargs)


def backfill_correction_flags(
    candidates: list[DeadlineCandidate], messages: MessageStore,
) -> int:
    """为历史候选补回「更正」标记：旧版状态文件未持久化该字段。"""
    changed = 0
    for candidate in candidates:
        if candidate.is_correction:
            continue
        message = messages.get(candidate.message_id)
        if message is None:
            continue
        marker = detect_correction(message.body)
        if marker:
            candidate.is_correction = True
            candidate.correction_marker = marker
            changed += 1
    return changed
