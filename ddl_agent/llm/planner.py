"""LLM 规划器：决定查哪些源、用什么过滤词、是否允许追加检索。"""

from __future__ import annotations

from datetime import datetime

from ..models import RetrievalPlan
from ..planner import QueryPlanner
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
以及其他课程的群，不要选择。"""


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
        selected = [source for source in sources if getattr(source, "name", "") in chosen] or base.sources

        return RetrievalPlan(
            query=query,
            sources=list(selected),
            course_filters=_strings(data.get("course_filters")) or base.course_filters,
            keywords=list(dict.fromkeys([*_strings(data.get("keywords")), *base.keywords])),
            date_from=_parse_dt(data.get("date_from")) or base.date_from,
            date_to=_parse_dt(data.get("date_to")) or base.date_to,
            allow_supplementary=bool(data.get("allow_supplementary", True)),
            reason=str(data.get("reason") or ""),
        )


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
