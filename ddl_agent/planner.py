"""自然语言查询规划：选数据源、课程过滤词、时间窗、检索关键词。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .extract.rules import find_courses
from .models import RetrievalPlan, SourceSpec
from .settings import TZ

_STOPWORDS = {
    "the", "and", "for", "what", "when", "which", "whats", "deadline", "deadlines",
    "due", "ddl", "about", "with", "from", "please", "tell", "show", "find", "any",
    "are", "is", "my", "me", "of", "on", "in", "to", "do", "does",
}

_EMAIL_HINTS = ("邮件", "email", "outlook", "收件箱", "邮箱")
_CHAT_HINTS = ("聊天", "群聊", "微信", "wechat", "qq", "群", "chat", "消息")


class SourceConfigParser:
    """把「跟踪课程邮件和课程群聊」这类自然语言配置转成数据源列表。

    既接受 ``SourceSpec`` 配置，也接受已实例化的连接器（只要有 ``name`` / ``source_type``）。
    """

    def parse(self, config_text: str, available: list) -> list:
        enabled = [spec for spec in available if getattr(spec, "enabled", True)]
        text = (config_text or "").strip().lower()
        if not text:
            return enabled

        selected: list = []
        for spec in enabled:
            name_hit = str(getattr(spec, "name", "")).lower() in text
            kind = getattr(spec, "kind", None) or (
                "email" if getattr(spec, "source_type", "") == "official_email" else "chat"
            )
            kind_hit = (
                kind == "email" and any(hint in text for hint in _EMAIL_HINTS)
            ) or (
                kind == "chat" and any(hint in text for hint in _CHAT_HINTS)
            )
            if name_hit or kind_hit:
                selected.append(spec)

        return selected or enabled


class QueryPlanner:
    def plan(self, query: str, sources: list[SourceSpec],
             now: datetime | None = None) -> RetrievalPlan:
        now = now or datetime.now(TZ)
        course_filters = find_courses(query)
        keywords = list(course_filters)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", query):
            lowered = token.lower()
            if lowered not in _STOPWORDS and lowered not in {k.lower() for k in keywords}:
                keywords.append(token)
        keywords += re.findall(r"[「“\"]([^」”\"]{2,30})[」”\"]", query)

        date_from, date_to = _parse_date_range(query, now)
        return RetrievalPlan(
            query=query,
            sources=list(sources),
            course_filters=course_filters,
            keywords=keywords,
            date_from=date_from,
            date_to=date_to,
        )


def _parse_date_range(query: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)

    if "下周" in query:
        start = monday + timedelta(days=7)
        return start, start + timedelta(days=7)
    if "本周" in query or "这周" in query:
        return monday, monday + timedelta(days=7)
    if "本月" in query:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        return start, end
    if "今天" in query:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    match = re.search(r"(?:未来|近)\s*(\d+)\s*天", query)
    if match:
        return now, now + timedelta(days=int(match.group(1)))
    return None, None
