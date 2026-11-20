"""消息级上下文：课程代码兜底、更正语义与邮件真实发送时间。

这几项都属于「同一条消息本身携带、但正文抽取容易漏掉」的信息：
- 群名/邮箱主题里的课程代码；
- 「更正/延期/改为/以最新为准」这类覆盖语义；
- 邮件正文 ``Sent:`` 行给出的真实发布时间（不能拿抓取时间冒充）。
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import RawMessage
from ..settings import TZ
from .courses import find_courses

_CORRECTION_RE = re.compile(
    r"(更正|延期|推迟|顺延|改为|改到|改期|以最新为准|以最新通知为准|最新通知|"
    r"updated|postponed|rescheduled|corrected|extended|new deadline)",
    re.IGNORECASE,
)

_SENT_RE = re.compile(
    r"(?:^|\n)\s*(?:Sent|发送时间|发送日期)\s*[:：]\s*"
    r"(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"
    r"(?:[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
    re.IGNORECASE,
)

_EMAIL_TYPES = {"official_email", "email", "mail", "outlook"}


def normalize_source_type(value: str | None) -> str:
    """把回放脚本里的 ``email`` 等别名归一到正式来源类型。"""
    text = (value or "").strip().lower()
    return "official_email" if text in _EMAIL_TYPES else text


def course_from_message(message: RawMessage) -> tuple[str | None, str | None]:
    """返回 (课程代码, 来源)。优先级：来源名称（群名）→ 主题。"""
    for text, origin in ((message.source_name, "source_name"), (message.subject, "subject")):
        codes = find_courses(text)
        if codes:
            return codes[0], origin
    return None, None


def detect_correction(text: str | None) -> str | None:
    """命中「更正/延期/改为/以最新为准」时返回命中的词，否则 None。"""
    match = _CORRECTION_RE.search(text or "")
    return match.group(1) if match else None


def email_sent_at(message: RawMessage) -> datetime | None:
    """从邮件正文 ``Sent:`` 行读真实发送时间；签名里的 Last updated 不算。"""
    if normalize_source_type(message.source_type) != "official_email":
        return None
    match = _SENT_RE.search(message.body or "")
    if not match:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour") or 0),
            int(match.group("minute") or 0),
            tzinfo=TZ,
        )
    except ValueError:
        return None


def effective_published_at(message: RawMessage) -> datetime | None:
    """邮件优先用正文 Sent 时间；其余来源沿用读屏/连接器给出的发布时间。"""
    return email_sent_at(message) or message.published_at
