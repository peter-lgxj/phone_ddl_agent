"""LLM 规划器：决定查哪些源、用什么过滤词、是否允许追加检索。"""

from __future__ import annotations

from datetime import datetime

from ..extract.message_context import normalize_source_type
from ..models import RetrievalPlan
from ..planner import (
    QueryPlanner,
    expand_similar_sources,
    normalize_source_name,
    query_mentions_email,
    select_sources,
)
from ..settings import TZ
from .client import LLMClient, LLMUnavailable

_SYSTEM = (
    "你是截止时间汇总 agent 的规划器。只输出 JSON，不要解释。"
    "不得选择未列出的数据源；不确定时给出空列表并说明理由。"
)

_PROMPT = """当前时间：{now}

用户查询：{query}
用户配置的数据源：
{sources}

请决定检索计划，输出 JSON：
{{
  "sources": ["要查询的数据源名称"],
  "course_filters": ["课程代码，如 COMP7608"],
  "keywords": ["检索关键词"],
  "date_from": "ISO-8601 或 null",
  "date_to": "ISO-8601 或 null",
  "allow_supplementary": true,
  "reason": "一句话说明为什么查这些源"
}}
注意：date_from/date_to 指用户关心的截止时间范围；查询没提时间就返回 null。
只选择与查询课程/项目相关的来源；实习内推群、家人群、社团群等无关社交来源，
以及其他课程的群，不要选择。名称高度相似（只差空格、年份后缀、序号）的群
只选与查询最匹配的一个，不要同时选中。同名基名（不带年份/学期后缀）优先视为
主群；带年份后缀的通常是分流/选课群，只有查询明确点名该后缀时才选它。"""


class LLMQueryPlanner:
    def __init__(self, client: LLMClient, fallback: QueryPlanner | None = None):
        self.client = client
        self.fallback = fallback or QueryPlanner()

    def plan(self, query: str, sources: list, now: datetime | None = None) -> RetrievalPlan:
        now = now or datetime.now(TZ)
        base = self.fallback.plan(query, sources, now)
        source_lines = "\n".join(
            f"- {getattr(source, 'name', source)} ({getattr(source, 'source_type', 'unknown')})"
            for source in sources
        )
        prompt = _PROMPT.format(now=now.isoformat(), query=query, sources=source_lines)

        try:
            data = self.client.complete_json(prompt, system=_SYSTEM, kind="planner")
        except LLMUnavailable:
            data = None
        if not isinstance(data, dict):
            return base

        names = {getattr(source, "name", "") for source in sources}
        chosen = [name for name in (data.get("sources") or []) if name in names]
        matched = [source for source in sources if getattr(source, "name", "") in chosen]
        if not matched and chosen:
            # 模型常把「Outlook/收件箱」写成主题片段；名称不完全相等时按归一化名再匹配一次
            matched = _match_by_name(sources, chosen)
        selected = matched or base.sources
        selected, email_added = _ensure_email_sources(query, sources, selected)
        # 初次选源就把「同名基名优先」前置，不依赖 pipeline 事后纠偏；
        # pipeline 的 expand_similar_sources 仍保留为兜底。
        selected = _prefer_similar_base_group(query, sources, selected)

        reason = str(data.get("reason") or "")
        if email_added:
            reason = f"{reason}；查询点名邮件，保留邮件来源" if reason else "查询点名邮件，保留邮件来源"

        return RetrievalPlan(
            query=query,
            sources=list(selected),
            course_filters=_strings(data.get("course_filters")) or base.course_filters,
            keywords=list(dict.fromkeys([*_strings(data.get("keywords")), *base.keywords])),
            date_from=_parse_dt(data.get("date_from")) or base.date_from,
            date_to=_parse_dt(data.get("date_to")) or base.date_to,
            allow_supplementary=bool(data.get("allow_supplementary", True)),
            reason=reason,
        )


def _prefer_similar_base_group(query: str, sources: list, selected: list) -> list:
    """模型选中带年份后缀的相似群时，初次选源就按基名优先换回主群。"""
    pool = expand_similar_sources(selected, sources)
    if len(pool) == len(selected):
        return list(selected)
    chosen, _ = select_sources(query, pool)
    return chosen or list(selected)


def _match_by_name(sources: list, names: list[str]) -> list:
    """按归一化名匹配来源；主题被截断时允许子串互含，避免把邮件源整类丢掉。"""
    wanted = [normalize_source_name(name) for name in names if name]
    matched = []
    for source in sources:
        compact = normalize_source_name(str(getattr(source, "name", "")))
        if not compact:
            continue
        if any(compact == item for item in wanted):
            matched.append(source)
        elif any(len(compact) >= 4 and len(item) >= 4
                 and (compact in item or item in compact) for item in wanted):
            matched.append(source)
    return matched


def _is_email_source(source) -> bool:
    kind = getattr(source, "kind", None)
    if kind in ("email", "chat"):
        return kind == "email"
    return normalize_source_type(getattr(source, "source_type", "")) == "official_email"


def _ensure_email_sources(query: str, sources: list, selected: list) -> tuple[list, bool]:
    """查询出现「邮件/Outlook/收件箱」时，邮件来源必须留在候选里。

    模型规划可能只返回聊天群（D13 即如此）；来源名带主题片段时更难精确回填。
    这里按来源类型兜底：只要查询点名邮件，就把未选中的邮件源补回，保证冲突两侧
    都能被读到。
    """
    if not query_mentions_email(query):
        return list(selected), False
    kept = list(selected)
    kept_ids = {id(source) for source in kept}
    added = False
    for source in sources:
        if id(source) in kept_ids or not _is_email_source(source):
            continue
        kept.append(source)
        kept_ids.add(id(source))
        added = True
    return kept, added


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)
