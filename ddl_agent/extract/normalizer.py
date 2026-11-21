"""时间表达式归一化：把消息里的日期/相对时间转成 ISO-8601。

相对时间一律以调用方传入的 ``now`` 为基准，保证离线评测可复现。
解析不出来时返回 ``unresolved=True``，绝不猜测具体日期。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..models import NormalizedTime
from ..settings import DEFAULT_DEADLINE_HOUR, DEFAULT_DEADLINE_MINUTE, TZ

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"

_ISO_RE = re.compile(
    r"(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})"
    r"(?:[ T]+(?P<hh>\d{1,2}):(?P<mm>\d{2}))?"
)
_CN_DATE_RE = re.compile(
    r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"
    r"(?:\s*(?P<hh>\d{1,2})[:：点]\s*(?P<mm>\d{1,2})?)?"
)
_EN_DATE_RE = re.compile(
    rf"{_MONTH_RE}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<y>20\d{2}))?"
    r"(?:\s*(?:at\s*)?(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*(?P<ampm>am|pm)?)?",
    re.IGNORECASE,
)
_SLASH_DATE_RE = re.compile(
    r"(?<![\d/-])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?![\d/-])"
    r"(?:\s+(?P<hh>\d{1,2}):(?P<mm>\d{2}))?"
)
# 群消息/OCR 常见的「11.20 23:59」写法：必须带时刻，避免把普通小数当日期
_DOT_DATE_RE = re.compile(
    r"(?<![\d.])(?P<m>\d{1,2})[.．](?P<d>\d{1,2})(?![\d.])"
    r"\s*(?P<hh>\d{1,2})[:：](?P<mm>\d{2})"
)

_CN_REL_DAY = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
_CN_REL_DAY_RE = re.compile(r"(大后天|今天|今日|明天|明日|后天)")
# 「月底前 / 本月底 / 下月初」这类相对表述：能确定的按月末/月初推断并标注依据
_CN_MONTH_END_RE = re.compile(r"(下个?月|这个月|本月|当月)?\s*(?:月底|月末)")
# 「本月底 / 下个月底」：月份前缀后直接跟「底/末」
_CN_MONTH_END_ALT_RE = re.compile(r"(下个?月|这个月|本月|当月)\s*(?:底|末)")
_CN_MONTH_START_RE = re.compile(r"(下个?月|这个月|本月|当月)?\s*(?:月初|月头)")
# 「下月初 / 本月初」：月份前缀后直接跟「初/头」
_CN_MONTH_START_ALT_RE = re.compile(r"(下个?月|这个月|本月|当月)\s*(?:初|头)")
_CN_WEEK_RE = re.compile(
    r"(下个星期|下星期|下周|这个星期|本星期|本周|这周|星期|周)([一二三四五六日天])"
)
_CN_IN_DAYS_RE = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*天\s*(?:后|之后|以后)")
_EN_REL_DAY_RE = re.compile(r"\b(today|tomorrow|day after tomorrow)\b", re.IGNORECASE)
_EN_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
)
_EN_WEEKDAY_RE = re.compile(
    r"\b(?:this\s+|on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
)
_EN_IN_DAYS_RE = re.compile(r"\bin\s+(\d+)\s+days?\b", re.IGNORECASE)
_EN_EOD_RE = re.compile(r"\b(eod|end of day)\b", re.IGNORECASE)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_CN_WEEKDAYS = "一二三四五六日"

_TIME_CN_RE = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚)?\s*"
    r"(\d{1,2})\s*(?:[:：]\s*(\d{1,2}))?\s*(?:点|时)?\s*(?:(\d{1,2})\s*分)?"
)
_TIME_EN_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

_DATE_LIKE_RE = re.compile(
    r"\d{1,2}\s*[月/.-]\s*\d{1,2}|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(?:today|tomorrow|friday|monday)\b",
    re.IGNORECASE,
)


def _month_boundary(today: date, offset: int, *, end: bool) -> date:
    """返回相对 ``today`` 偏移 ``offset`` 个月的最后一天 / 第一天。"""
    year, month = today.year, today.month + offset
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    if end:
        return date(year, month, calendar.monthrange(year, month)[1])
    return date(year, month, 1)


def cn_number(text: str) -> int | None:
    """把「一」「十二」「二十」之类的简单中文数字转成整数。"""
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in digits:
        return digits[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _scan_time_of_day(text: str) -> tuple[int, int] | None:
    """从文本里找时刻；找不到返回 None（由调用方补默认 23:59）。"""
    for match in _TIME_CN_RE.finditer(text):
        marker, hour_s, minute_s, minute_alt_s = match.groups()
        # 「11月20日」里的数字不该被当成时刻，至少要有分隔符、点/时或时段词
        has_time_shape = bool(minute_s or minute_alt_s or "点" in match.group(0) or "时" in match.group(0)
                              or marker)
        if not has_time_shape:
            continue
        hour = int(hour_s)
        minute = int(minute_s or minute_alt_s or 0)
        if marker in ("下午", "傍晚", "晚上", "今晚", "明晚") and hour < 12:
            hour += 12
        elif marker in ("凌晨", "早上", "上午") and hour == 12:
            hour = 0
        elif marker == "中午" and hour < 11:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    match = _TIME_EN_RE.search(text)
    if match:
        hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


@dataclass
class _Match:
    day: date
    time_of_day: tuple[int, int] | None
    text: str
    inferred: bool = False
    basis: str = ""


class TimeNormalizer:
    """把自然语言时间转成 ``YYYY-MM-DDTHH:MM:SS+08:00``。"""

    def __init__(self, tz=TZ, default_time: tuple[int, int] = (DEFAULT_DEADLINE_HOUR,
                                                              DEFAULT_DEADLINE_MINUTE)):
        self.tz = tz
        self.default_time = default_time

    # ------------------------------------------------------------------ #
    #  对外入口
    # ------------------------------------------------------------------ #
    def normalize(self, text: str | None, now: datetime | None = None) -> NormalizedTime:
        if not text or not text.strip():
            return NormalizedTime(iso=None, matched_text=None, unresolved=False)

        now = self._ensure_tz(now or datetime.now(self.tz))
        text = text.strip()

        try:
            match = self._match_absolute(text, now) or self._match_relative(text, now)
        except ValueError as exc:  # 例如 13 月 40 日
            return NormalizedTime(iso=None, matched_text=None, unresolved=True,
                                  note=f"日期字段非法：{exc}")

        if match is None:
            looks_like_date = bool(_DATE_LIKE_RE.search(text))
            return NormalizedTime(
                iso=None,
                matched_text=None,
                unresolved=looks_like_date,
                note="未能解析出具体日期" if looks_like_date else "未包含时间表达",
            )

        hour, minute = match.time_of_day or self.default_time
        default_time_assumed = match.time_of_day is None
        dt = datetime.combine(match.day, time(hour=hour, minute=minute), tzinfo=self.tz)
        note = "仅有日期，按当天 23:59 处理" if default_time_assumed else ""
        if match.inferred and match.basis:
            note = "；".join(filter(None, [note, match.basis]))
        return NormalizedTime(
            iso=dt.isoformat(timespec="seconds"),
            matched_text=match.text,
            unresolved=False,
            note=note,
            default_time_assumed=default_time_assumed,
            inferred=match.inferred,
            inference_basis=match.basis,
        )

    # ------------------------------------------------------------------ #
    #  绝对日期
    # ------------------------------------------------------------------ #
    def _match_absolute(self, text: str, now: datetime) -> _Match | None:
        match = _ISO_RE.search(text)
        if match:
            year, month, day = int(match.group("y")), int(match.group("m")), int(match.group("d"))
            day_obj = date(year, month, day)
            time_of_day = self._groups_time(match) or _scan_time_of_day(match.group(0))
            return _Match(day_obj, time_of_day, match.group(0))

        match = _CN_DATE_RE.search(text)
        if match:
            month, day = int(match.group("m")), int(match.group("d"))
            day_obj = self._resolve_year(now, month, day)
            time_of_day = self._groups_time(match) or _scan_time_of_day(text)
            return _Match(day_obj, time_of_day, match.group(0))

        match = _EN_DATE_RE.search(text)
        if match:
            month = _MONTHS[match.group(1).lower()[:3]]
            day = int(match.group("d"))
            year = int(match.group("y")) if match.group("y") else None
            day_obj = date(year, month, day) if year else self._resolve_year(now, month, day)
            time_of_day = self._groups_time(match, ampm_group="ampm") or _scan_time_of_day(match.group(0))
            return _Match(day_obj, time_of_day, match.group(0))

        match = _SLASH_DATE_RE.search(text)
        if match:
            month, day = int(match.group("m")), int(match.group("d"))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                raise ValueError(f"{match.group(0)}")
            day_obj = self._resolve_year(now, month, day)
            time_of_day = self._groups_time(match) or _scan_time_of_day(text)
            return _Match(day_obj, time_of_day, match.group(0))

        match = _DOT_DATE_RE.search(text)
        if match:
            month, day = int(match.group("m")), int(match.group("d"))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                raise ValueError(f"{match.group(0)}")
            day_obj = self._resolve_year(now, month, day)
            time_of_day = self._groups_time(match) or _scan_time_of_day(text)
            return _Match(day_obj, time_of_day, match.group(0))

        return None

    # ------------------------------------------------------------------ #
    #  相对时间
    # ------------------------------------------------------------------ #
    def _match_relative(self, text: str, now: datetime) -> _Match | None:
        match = _CN_MONTH_END_ALT_RE.search(text) or _CN_MONTH_END_RE.search(text)
        if match:
            prefix = match.group(1) or ""
            offset = 1 if prefix.startswith("下") else 0
            day_obj = _month_boundary(now.date(), offset, end=True)
            basis = (
                f"「{match.group(0).strip()}」按"
                f"{'下' if offset else '本'}月最后一天 23:59 推断"
            )
            return _Match(day_obj, (23, 59), match.group(0), inferred=True, basis=basis)

        match = _CN_MONTH_START_ALT_RE.search(text) or _CN_MONTH_START_RE.search(text)
        if match:
            prefix = match.group(1) or ""
            offset = 1 if prefix.startswith("下") else 0
            day_obj = _month_boundary(now.date(), offset, end=False)
            basis = (
                f"「{match.group(0).strip()}」按"
                f"{'次' if offset else '本'}月 1 日 23:59 推断"
            )
            return _Match(day_obj, (23, 59), match.group(0), inferred=True, basis=basis)

        match = _CN_REL_DAY_RE.search(text)
        if match:
            offset = _CN_REL_DAY[match.group(1)]
            day_obj = now.date() + timedelta(days=offset)
            return _Match(day_obj, _scan_time_of_day(text), match.group(0))

        match = _CN_WEEK_RE.search(text)
        if match:
            prefix, weekday_char = match.group(1), match.group(2)
            target = 6 if weekday_char == "天" else _CN_WEEKDAYS.index(weekday_char)
            today_idx = now.weekday()
            if prefix in ("下周", "下星期", "下个星期"):
                delta = 7 - today_idx + target
            else:
                delta = (target - today_idx) % 7
            return _Match(now.date() + timedelta(days=delta), _scan_time_of_day(text), match.group(0))

        match = _CN_IN_DAYS_RE.search(text)
        if match:
            amount = cn_number(match.group(1))
            if amount is None:
                raise ValueError(match.group(0))
            return _Match(now.date() + timedelta(days=amount), _scan_time_of_day(text), match.group(0))

        match = _EN_EOD_RE.search(text)
        if match:
            return _Match(now.date(), (23, 59), match.group(0))

        match = _EN_REL_DAY_RE.search(text)
        if match:
            phrase = match.group(1).lower()
            offset = {"today": 0, "tomorrow": 1, "day after tomorrow": 2}[phrase]
            return _Match(now.date() + timedelta(days=offset), _scan_time_of_day(text), match.group(0))

        match = _EN_NEXT_WEEKDAY_RE.search(text)
        if match:
            target = _WEEKDAYS.index(match.group(1).lower())
            delta = 7 - now.weekday() + target
            return _Match(now.date() + timedelta(days=delta), _scan_time_of_day(text), match.group(0))

        match = _EN_IN_DAYS_RE.search(text)
        if match:
            return _Match(now.date() + timedelta(days=int(match.group(1))), _scan_time_of_day(text),
                          match.group(0))

        match = _EN_WEEKDAY_RE.search(text)
        if match:
            target = _WEEKDAYS.index(match.group(1).lower())
            delta = (target - now.weekday()) % 7
            return _Match(now.date() + timedelta(days=delta), _scan_time_of_day(text), match.group(0))

        return None

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    def _resolve_year(self, now: datetime, month: int, day: int) -> date:
        candidate = date(now.year, month, day)
        # 只写月日时，若已过去较久则理解为明年
        if candidate < now.date() - timedelta(days=30):
            candidate = date(now.year + 1, month, day)
        return candidate

    @staticmethod
    def _groups_time(match: re.Match, ampm_group: str | None = None) -> tuple[int, int] | None:
        hour_s = match.groupdict().get("hh")
        if not hour_s:
            return None
        hour = int(hour_s)
        minute = int(match.groupdict().get("mm") or 0)
        if ampm_group:
            ampm = (match.groupdict().get(ampm_group) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @staticmethod
    def _ensure_tz(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
