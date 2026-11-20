"""自然语言查询规划：选数据源、课程过滤词、时间窗、检索关键词。"""

from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import datetime, timedelta

from .extract.courses import find_courses
from .extract.message_context import normalize_source_type
from .models import RetrievalPlan, SourceSpec
from .settings import TZ

_STOPWORDS = {
    "the", "and", "for", "what", "when", "which", "whats", "deadline", "deadlines",
    "due", "ddl", "about", "with", "from", "please", "tell", "show", "find", "any",
    "are", "is", "my", "me", "of", "on", "in", "to", "do", "does",
}

_EMAIL_HINTS = ("邮件", "email", "outlook", "收件箱", "邮箱")
_CHAT_HINTS = ("聊天", "群聊", "微信", "wechat", "qq", "群", "chat", "消息")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[a-z0-9]+")
_GROUP_SIMILARITY = 0.88


def normalize_source_name(name: str) -> str:
    """群名归一化：全半角、大小写、空格与标点都不参与比较。"""
    value = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def source_tokens(name: str) -> frozenset[str]:
    """把群名拆成「英文数字串 + 单个汉字」，用于词序无关的比较。"""
    compact = normalize_source_name(name)
    tokens = _ALNUM_RE.findall(compact)
    tokens.extend(_CJK_RE.findall(compact))
    return frozenset(tokens)


def source_names_equivalent(left: str, right: str) -> bool:
    """仅空格/大小写/标点/词序不同的群名视为同一个群；序号差异不算同群。"""
    if not left or not right:
        return False
    if normalize_source_name(left) == normalize_source_name(right):
        return True
    return source_tokens(left) == source_tokens(right)


def _name_ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(
        None, normalize_source_name(left), normalize_source_name(right)
    ).ratio()


def _is_similar_group(left: str, right: str) -> bool:
    """相似群名：等价、极高相似，或同一课程代码下的「基名 vs 年份后缀」变体。"""
    if source_names_equivalent(left, right):
        return True
    if _name_ratio(left, right) >= _GROUP_SIMILARITY:
        return True

    left_codes, right_codes = set(find_courses(left)), set(find_courses(right))
    if not (left_codes & right_codes):
        return False
    left_has_year = bool(re.search(r"20\d{2}", left))
    right_has_year = bool(re.search(r"20\d{2}", right))
    return left_has_year != right_has_year


def _name_in_query(name: str, query: str) -> bool:
    compact_name = normalize_source_name(name)
    return bool(compact_name) and compact_name in normalize_source_name(query)


def _base_preference(name: str) -> tuple[int, int]:
    """基名优先：不带年份后缀、名字更短的那个更像“主群”。"""
    return (int(bool(re.search(r"20\d{2}", name))), len(normalize_source_name(name)))


_AMBIGUOUS_GROUP_HINTS = ("相似", "两个", "同名", "正确那个", "歧义", "哪个")


def _mentions_ambiguous_groups(query: str) -> bool:
    return any(hint in query for hint in _AMBIGUOUS_GROUP_HINTS)


def _prefer_source(candidate: str, existing: str, query: str) -> bool:
    base, other = sorted((candidate, existing), key=_base_preference)
    base_named = _name_in_query(base, query)
    other_named = _name_in_query(other, query)

    if other_named and not base_named:
        # 查询明确点名年份/序号变体，但同时又提示「有两个相似群」时，仍回到底名主群
        if _base_preference(other) > _base_preference(base) and _mentions_ambiguous_groups(query):
            return candidate == base
        return candidate == other
    if base_named and not other_named:
        return candidate == base
    # 两个都点名或都没点名：基名优先，避免把「COMP7503-2026」当成主群
    return candidate == base


def select_sources(query: str, sources: list) -> tuple[list, list[dict]]:
    """相似群名只保留与查询最匹配的一个，并记录选择依据。

    返回 ``(选中的来源, 选择记录)``；没有相似群时记录为空。
    """
    kept: list = []
    decisions: list[dict] = []
    for source in sources:
        name = str(getattr(source, "name", source))
        match = next(
            ((index, item) for index, item in enumerate(kept)
             if _is_similar_group(str(getattr(item, "name", item)), name)),
            None,
        )
        if match is None:
            kept.append(source)
            continue

        index, existing = match
        existing_name = str(getattr(existing, "name", existing))
        if not _prefer_source(name, existing_name, query):
            decisions.append(
                _selection_entry(
                    kept=existing_name,
                    dropped=name,
                    reason="群名归一化后相似，仅保留与查询最匹配的一个",
                    left=existing_name,
                    right=name,
                )
            )
            continue

        kept[index] = source
        decisions.append(
            _selection_entry(
                kept=name,
                dropped=existing_name,
                reason="群名归一化后相似，仅保留与查询最匹配的一个",
                left=existing_name,
                right=name,
            )
        )
    return kept, decisions


def _selection_entry(kept: str, dropped: str, reason: str,
                     left: str, right: str) -> dict:
    equivalent = source_names_equivalent(left, right)
    return {
        "kept": kept,
        "dropped": [dropped],
        "reason": (
            "群名归一化后同名（仅空格/词序/标点不同）"
            if equivalent and _name_ratio(left, right) == 1.0
            else reason
        ),
        "similarity": round(_name_ratio(left, right), 4),
        "equivalent": equivalent,
    }


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
            name = str(getattr(spec, "name", ""))
            # 精确子串之外，再做空格/词序归一化匹配
            name_hit = (
                name.lower() in text
                or normalize_source_name(name) in normalize_source_name(text)
                or source_tokens(name) <= source_tokens(text)
            )
            kind = getattr(spec, "kind", None) or (
                "email" if normalize_source_type(getattr(spec, "source_type", "")) == "official_email"
                else "chat"
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
