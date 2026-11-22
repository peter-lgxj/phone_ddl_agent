"""手机读屏结果的共享解析与状态归一化。

监控主路径（``monitor.sources.MobileAppSource``）与连接器
（``connectors.mobile_chat.MobileChatConnector``）都使用这里的逻辑，避免出现
“连接器保留部分结果、生产路径仍把 timeout 当硬失败”的分叉。

解析原则：
- 从 observation 中按行/按条拆分消息，尽量读出真实发送者与发布时间；
- 只有日期没有时间、只有时间没有日期、或完全解析不出来时，``published_at``
  置空并标记 ``needs_review``，不使用抓取时间冒充发布时间；
- ``[OCR]`` 段只在模型描述没有给出可解析消息时作为兜底文本。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import RawMessage
from ..settings import TZ
from observation_schema import ABSENT_VALUES

_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\d+[.、)]|[-*])\s*")
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DATE_RE = re.compile(
    r"(?:(?P<year>20\d{2})[-/年])?(?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?"
)
_CLOCK_RE = re.compile(r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})")
_SKIP_LINES = {
    "任务完成",
    "完成",
    "当前界面",
    "未看到消息文本",
    "没有消息",
    "已到最新",
    "已读",
    "微信",
    "文件传输助手",
    "[OCR]",
}
_UI_MARKERS = ("电池", "电量", "信号", "wifi", "WI-FI", "运营商")


@dataclass
class ParsedMessage:
    """从一段读屏文本中拆出的一条消息。"""

    body: str
    sender: str = ""
    published_at: datetime | None = None
    needs_review: bool = False
    review_reason: str = ""


def _clean_sender(value: str) -> str:
    text = value.strip(" \t\r\n:：,，;；")
    text = re.sub(r"[（(].*?[)）]", "", text).strip()
    if text.lower() in ABSENT_VALUES:
        return ""
    if text in {"自己", "本人", "me", "Me"}:
        return "我"
    return text


def _extract_field(line: str, label: str) -> str:
    match = re.search(rf"{label}[:：]\s*(.+?)(?=\s+(?:发送者|时间|原文)[:：]|$)", line)
    return match.group(1).strip() if match else ""


def parse_time_token(value: str, *, now: datetime | None = None
                     ) -> tuple[datetime | None, str]:
    """把读屏里的时间片段转成带时区的 datetime；不完整时返回原因。"""
    text = (value or "").strip()
    if not text or text.lower() in ABSENT_VALUES:
        return None, "未读到时间"

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ), ""
    except ValueError:
        pass

    current = now or datetime.now(TZ)
    date_match = _DATE_RE.search(text)
    clock_match = _CLOCK_RE.search(text)
    if not date_match:
        if clock_match:
            return None, "只有时间没有日期"
        return None, "时间无法解析"

    try:
        year = int(date_match.group("year") or current.year)
        month = int(date_match.group("month"))
        day = int(date_match.group("day"))
        hour = int(clock_match.group("hour")) if clock_match else 0
        minute = int(clock_match.group("minute")) if clock_match else 0
        parsed = datetime(year, month, day, hour, minute, tzinfo=TZ)
    except (TypeError, ValueError):
        return None, "时间无法解析"

    reasons: list[str] = []
    if not date_match.group("year"):
        reasons.append("年份按当前年份推断")
    if not clock_match:
        reasons.append("只有日期没有具体时间")
    return parsed, "；".join(reasons)


def _parse_structured(text: str, *, now: datetime) -> list[ParsedMessage]:
    """解析 ``发送者：... 时间：... 原文：...`` 这类结构化输出。"""
    records: list[ParsedMessage] = []
    current: dict[str, str] | None = None

    def _flush() -> None:
        nonlocal current
        if current is None:
            return
        body = current.get("body", "").strip()
        if body and body not in _SKIP_LINES:
            parsed_time, reason = parse_time_token(current.get("time", ""), now=now)
            sender = _clean_sender(current.get("sender", ""))
            # 屏幕确实没有时间（模型按约定写“未显示”）只留空并记原因，不整体降级；
            # 时间片段存在但解析不了，才需要人工复核。缺发送者只记审计，不阻断确认。
            needs_review = bool(reason) and reason != "未读到时间"
            review_bits = []
            if reason:
                review_bits.append(reason)
            if not sender:
                review_bits.append("未读到发送者")
            records.append(
                ParsedMessage(
                    body=body,
                    sender=sender,
                    published_at=parsed_time,
                    needs_review=needs_review,
                    review_reason="；".join(review_bits),
                )
            )
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        has_sender = "发送者" in line
        has_body = "原文" in line
        if has_sender:
            _flush()
            current = {
                "sender": _extract_field(line, "发送者"),
                "time": _extract_field(line, "时间"),
                "body": _extract_field(line, "原文"),
            }
            continue
        if current is not None:
            if "时间" in line and not current.get("time"):
                current["time"] = _extract_field(line, "时间")
            elif has_body:
                current["body"] = (current.get("body") or "") + _extract_field(line, "原文")
            elif not _TIME_ONLY_RE.match(line) and line not in _SKIP_LINES:
                current["body"] = (current.get("body") or "") + line
    _flush()
    return [record for record in records if record.body]


def _parse_plain(text: str, *, now: datetime) -> list[ParsedMessage]:
    """没有结构化字段时，按行拆成消息；时间和发送者保持未知。"""
    records: list[ParsedMessage] = []
    for raw_line in text.splitlines():
        line = _NUMBER_PREFIX_RE.sub("", raw_line.strip())
        if not line or line in _SKIP_LINES or _TIME_ONLY_RE.match(line):
            continue
        if any(marker in line for marker in _UI_MARKERS) and len(line) <= 12:
            continue
        records.append(
            ParsedMessage(
                body=line,
                sender="",
                published_at=None,
                needs_review=True,
                review_reason="未读到发送者与发布时间",
            )
        )
    return records


def split_observation(observation: str, *, now: datetime | None = None
                      ) -> list[ParsedMessage]:
    """把一条 observation 拆成若干条消息。"""
    text = (observation or "").strip()
    if not text:
        return []
    current = now or datetime.now(TZ)
    model_text, separator, ocr_text = text.partition("[OCR]")

    records = _parse_structured(model_text, now=current)
    if not records:
        # 模型只给了一句概括时，优先用 OCR 正文兜底；否则用模型文本
        fallback = ocr_text.strip() if separator and len(model_text.strip()) < 80 else model_text
        records = _parse_plain(fallback, now=current)
    if not records and text not in _SKIP_LINES:
        records = [ParsedMessage(body=text, needs_review=True,
                                 review_reason="未能拆分为独立消息")]
    return records


def build_mobile_messages(agent: Any, *, source_name: str, source_type: str,
                          subject: str, thread_id: str,
                          message_prefix: str = "",
                          now: datetime | None = None) -> list[RawMessage]:
    """把 ScreenAgent 的逐步观察转成 ``RawMessage``。

    与具体连接器无关；调用方只负责传入来源名称、类型和主题。
    """
    memory = getattr(agent, "last_memory", None)
    steps = list(getattr(memory, "steps", []) or [])
    run_dir = Path(getattr(agent, "run_dir", "") or ".")
    current = now or datetime.now(TZ)
    messages: list[RawMessage] = []
    seen: set[str] = set()

    for index, step in enumerate(steps, start=1):
        observation = str(step.get("observation") or "").strip()
        if not observation:
            continue
        step_no = int(step.get("step") or index)
        evidence = run_dir / f"step_{step_no:03d}.json"
        evidence_path = str(evidence) if evidence.exists() else None
        for position, record in enumerate(split_observation(observation, now=current), start=1):
            digest = hashlib.sha1(
                f"{thread_id}|{step_no}|{position}|{record.body}|"
                f"{record.sender}|{record.published_at}".encode("utf-8")
            ).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            messages.append(
                RawMessage(
                    message_id=f"{message_prefix}{digest}",
                    source_name=source_name,
                    source_type=source_type,  # type: ignore[arg-type]
                    sender=record.sender,
                    published_at=record.published_at,
                    subject=subject,
                    body=record.body,
                    evidence_path=evidence_path,
                    thread_id=thread_id,
                    needs_review=record.needs_review,
                    review_reason=record.review_reason,
                )
            )
    return messages
