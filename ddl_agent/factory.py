"""按配置组装 agent：LLM 版优先，缺 key/未启用时自动降级到规则版。"""

from __future__ import annotations

import os
from datetime import datetime

from .conflict import ConflictResolver
from .extract.llm_extractor import LLMDeadlineExtractor
from .extract.rules import RuleExtractor
from .llm.arbiter import LLMConflictArbiter
from .llm.client import LLMClient, UsageTracker
from .llm.planner import LLMQueryPlanner
from .pipeline import DdlAgent
from .planner import QueryPlanner


def llm_enabled() -> bool:
    return os.getenv("LLM_ENABLED", "1").strip().lower() not in ("0", "false", "off", "")


def build_agent(sources: list, writer=None, now: datetime | None = None,
                engine: str | None = None,
                tracker: UsageTracker | None = None) -> tuple[DdlAgent, str]:
    """返回 ``(agent, engine_name)``；engine 为 llm 或 rules。"""
    tracker = tracker or UsageTracker()
    engine = (engine or ("llm" if llm_enabled() else "rules")).lower()

    if engine == "llm":
        client = LLMClient.from_config(tracker=tracker)
        if client is not None:
            agent = DdlAgent(
                sources=sources,
                extractor=LLMDeadlineExtractor(client),
                resolver=ConflictResolver(arbiter=LLMConflictArbiter(client)),
                planner=LLMQueryPlanner(client),
                writer=writer,
                now=now,
                usage=tracker,
            )
            return agent, "llm"

    agent = DdlAgent(
        sources=sources,
        extractor=RuleExtractor(),
        resolver=ConflictResolver(),
        planner=QueryPlanner(),
        writer=writer,
        now=now,
        usage=tracker,
    )
    return agent, "rules"
