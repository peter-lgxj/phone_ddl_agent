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
from datetime import datetime, timedelta
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
_UI_CLOCK_ONLY_RE = re.compile(r"^(?P<hour>\d{1,2})[:：](?P<minute>\d{2})$")
_UI_YESTERDAY_RE = re.compile(
    r"^(?:昨天|昨日)\s*(?:(?P<hour>\d{1,2})[:：](?P<minute>\d{2}))?$"
)
_UI_WEEKDAY_RE = re.compile(
    r"^(?:周|星期)(?P<weekday>[一二三四五六日天])\s*"
    r"(?:(?P<hour>\d{1,2})[:：](?P<minute>\d{2}))?$"
)
_WEEKDAY_INDEX = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
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
    published_time: str = ""
    published_at_inferred: bool = False
    published_at_basis: str = ""
    needs_review: bool = False
    review_reason: str = ""


@dataclass
class ParsedTime:
    """时间片段的解析结果。

    ``published_at`` 只放能确定日期的完整时间；只有时刻时保留
    ``published_time``，不把读屏当天日期补成完整时间戳。
    """

    published_at: datetime | None = None
    published_time: str = ""
    date_inferred: bool = False
    date_inference_basis: str = ""
    reason: str = ""


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


def parse_time_details(value: str, *, now: datetime | None = None) -> ParsedTime:
    """解析读屏时间片段，区分完整时间、只有时刻、只有日期和跨周歧义。"""
    text = (value or "").strip()
    if not text or text.lower() in ABSENT_VALUES:
        return ParsedTime(reason="未读到时间")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return ParsedTime(published_at=parsed.astimezone(TZ))
    except ValueError:
        pass

    current = now or datetime.now(TZ)

    clock = _UI_CLOCK_ONLY_RE.fullmatch(text)
    if clock:
        hour, minute = int(clock.group("hour")), int(clock.group("minute"))
        if hour > 23 or minute > 59:
            return ParsedTime(reason="时间无法解析")
        return ParsedTime(
            published_time=f"{hour:02d}:{minute:02d}",
            reason="只有时刻、日期未知",
        )

    yesterday = _UI_YESTERDAY_RE.fullmatch(text)
    if yesterday:
        date = (current - timedelta(days=1)).date()
        clock_text = _clock_text(yesterday)
        basis = "按读屏日期推断“昨天”"
        if clock_text:
            hour, minute = _clock_parts(yesterday)
            return ParsedTime(
                published_at=datetime(date.year, date.month, date.day, hour, minute,
                                      tzinfo=TZ),
                published_time=clock_text,
                date_inferred=True,
                date_inference_basis=basis,
            )
        return ParsedTime(
            published_at=datetime(date.year, date.month, date.day, tzinfo=TZ),
            date_inferred=True,
            date_inference_basis=basis,
            reason="只有日期没有具体时刻",
        )

    weekday = _UI_WEEKDAY_RE.fullmatch(text)
    if weekday:
        label = ("周" if text.startswith("周") else "星期") + weekday.group("weekday")
        target = _WEEKDAY_INDEX[weekday.group("weekday")]
        monday = current.date() - timedelta(days=current.weekday())
        target_date = monday + timedelta(days=target)
        clock_text = _clock_text(weekday)
        if target_date > current.date():
            # 本周还没到这一天，无法判断是上周还是下周，按约定不猜日期
            return ParsedTime(
                published_time=clock_text,
                reason="星期跨周歧义，日期未知",
            )
        basis = f"按读屏日期锚定本周{label}"
        if clock_text:
            hour, minute = _clock_parts(weekday)
            return ParsedTime(
                published_at=datetime(target_date.year, target_date.month,
                                      target_date.day, hour, minute, tzinfo=TZ),
                published_time=clock_text,
                date_inferred=True,
                date_inference_basis=basis,
            )
        return ParsedTime(
            published_at=datetime(target_date.year, target_date.month,
                                  target_date.day, tzinfo=TZ),
            date_inferred=True,
            date_inference_basis=basis,
            reason="只有日期没有具体时刻",
        )

    date_match = _DATE_RE.search(text)
    clock_match = _CLOCK_RE.search(text)
    if not date_match:
        if clock_match:
            hour, minute = int(clock_match.group("hour")), int(clock_match.group("minute"))
            return ParsedTime(
                published_time=f"{hour:02d}:{minute:02d}",
                reason="只有时刻、日期未知",
            )
        return ParsedTime(reason="时间无法解析")

    try:
        year = int(date_match.group("year") or current.year)
        month = int(date_match.group("month"))
        day = int(date_match.group("day"))
        hour = int(clock_match.group("hour")) if clock_match else 0
        minute = int(clock_match.group("minute")) if clock_match else 0
        parsed = datetime(year, month, day, hour, minute, tzinfo=TZ)
    except (TypeError, ValueError):
        return ParsedTime(reason="时间无法解析")

    reasons: list[str] = []
    basis = ""
    if not date_match.group("year"):
        basis = "年份按读屏年份推断"
        reasons.append(basis)
    if not clock_match:
        reasons.append("只有日期没有具体时刻")
    return ParsedTime(
        published_at=parsed,
        published_time=f"{hour:02d}:{minute:02d}" if clock_match else "",
        date_inferred=bool(basis),
        date_inference_basis=basis,
        reason="；".join(reasons),
    )


def _clock_text(match: re.Match) -> str:
    if match.group("hour") is None:
        return ""
    return f"{int(match.group('hour')):02d}:{int(match.group('minute')):02d}"


def _clock_parts(match: re.Match) -> tuple[int, int]:
    return int(match.group("hour")), int(match.group("minute"))


def parse_time_token(value: str, *, now: datetime | None = None
                     ) -> tuple[datetime | None, str]:
    """兼容旧调用：只返回完整时间与原因。"""
    details = parse_time_details(value, now=now)
    return details.published_at, details.reason


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
            details = parse_time_details(current.get("time", ""), now=now)
            sender = _clean_sender(current.get("sender", ""))
            # 屏幕确实没有时间（模型按约定写“未显示”）只留空并记原因，不整体降级；
            # 只有时刻/跨周歧义保留时刻和原因，不把读屏日期补成完整时间戳。
            needs_review = bool(details.reason) and details.reason != "未读到时间"
            review_bits = []
            if details.reason:
                review_bits.append(details.reason)
            if not sender:
                review_bits.append("未读到发送者")
            records.append(
                ParsedMessage(
                    body=body,
                    sender=sender,
                    published_at=details.published_at,
                    published_time=details.published_time,
                    published_at_inferred=details.date_inferred,
                    published_at_basis=details.date_inference_basis,
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
                    published_time=record.published_time,
                    published_at_inferred=record.published_at_inferred,
                    published_at_basis=record.published_at_basis,
                    observed_at=current,
                )
            )
    return messages
