"""抽取结果的硬性证据校验。

课程名与截止时间必须能在**该条消息自己的正文**里找到字串或等价归一化形式。
同屏其他消息、课程表图片里出现的内容不算证据；校验失败就置空并标记复核。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from ..models import DeadlineCandidate

_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(
    r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})[-/月.](?P<day>\d{1,2})日?"
)
_CLOCK_RE = re.compile(r"(?P<hour>\d{1,2})[:：时点.](?P<minute>\d{1,2})")
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME_RE = re.compile(
    r"\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})\b",
    re.IGNORECASE,
)


def normalize_for_evidence(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = value.replace("晚上", "").replace("上午", "").replace("下午", "")
    return _WS_RE.sub("", value)


def _time_parts(text: str) -> tuple[int | None, ...] | None:
    value = unicodedata.normalize("NFKC", text or "")
    date_match = _DATE_RE.search(value)
    month = day = None
    if date_match:
        month = int(date_match.group("month"))
        day = int(date_match.group("day"))
        value = value[:date_match.start()] + " " + value[date_match.end():]
    else:
        name_match = _MONTH_NAME_RE.search(value)
        if name_match:
            month = _MONTH_NAMES[name_match.group("month").lower()]
            day = int(name_match.group("day"))
            value = value[:name_match.start()] + " " + value[name_match.end():]

    clock_match = _CLOCK_RE.search(value)
    hour = minute = None
    if clock_match:
        hour = int(clock_match.group("hour"))
        minute = int(clock_match.group("minute"))
    if month is None and hour is None:
        return None
    return (month, day, hour, minute)


def _time_evidence_in_body(raw: str | None, iso: str | None, body: str) -> bool:
    body_norm = normalize_for_evidence(body)
    if raw and normalize_for_evidence(raw) in body_norm:
        return True

    wanted = _time_parts(raw or "") or _time_parts(iso or "")
    if wanted is None:
        return False
    body_parts = _time_parts(body)
    if body_parts is None:
        return False

    wanted_month, wanted_day, wanted_hour, wanted_minute = wanted
    body_month, body_day, body_hour, body_minute = body_parts
    if wanted_month is not None:
        if (body_month, body_day) != (wanted_month, wanted_day):
            return False
    if wanted_hour is not None:
        if (body_hour, body_minute) != (wanted_hour, wanted_minute):
            return False
    return True


def validate_candidate_evidence(candidate: DeadlineCandidate, body: str,
                                course_context: str = "") -> DeadlineCandidate:
    """把正文里找不到依据的课程/截止时间置空，并标记 ``needs_review``。

    ``course_context`` 是群名、主题等来源级上下文：课程代码在这里出现也算有效证据，
    避免「群名是 COMP7503课程群、正文只写作业三」被误判成缺少课程。
    """
    reasons: list[str] = []
    body_norm = normalize_for_evidence(body)
    course_norm = normalize_for_evidence(f"{body}\n{course_context}")

    course = candidate.course
    if course and normalize_for_evidence(course) not in course_norm:
        reasons.append(f"课程 {course} 未在该条消息正文/来源上下文中出现")
        course = None

    deadline_iso = candidate.deadline_iso
    deadline_raw = candidate.deadline_raw
    if (deadline_iso or deadline_raw) and not _time_evidence_in_body(
        deadline_raw, deadline_iso, body
    ):
        reasons.append("截止时间原文未在该条消息正文中出现")
        deadline_iso = None
        deadline_raw = None

    if not reasons:
        return candidate

    existing = candidate.review_reason
    return replace(
        candidate,
        course=course,
        deadline_iso=deadline_iso,
        deadline_raw=deadline_raw,
        needs_review=True,
        unresolved_time=candidate.unresolved_time or deadline_iso is None,
        review_reason="；".join(filter(None, [existing, *reasons])),
    )
