"""按配置组装 agent：LLM 版优先，缺 key/未启用时自动降级到规则版。"""

from __future__ import annotations

import os

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


def build_components(engine: str | None = None,
                     tracker: UsageTracker | None = None) -> dict:
    """返回规划/抽取/裁决组件；``engine`` 为 auto/llm/rules。"""
    tracker = tracker or UsageTracker()
    requested = (engine or ("llm" if llm_enabled() else "rules")).lower()

    if requested != "rules" and llm_enabled():
        client = LLMClient.from_config(tracker=tracker)
        if client is not None:
            return {
                "engine": "llm",
                "client": client,
                "tracker": tracker,
                "planner": LLMQueryPlanner(client),
                "extractor": LLMDeadlineExtractor(client),
                "resolver": ConflictResolver(arbiter=LLMConflictArbiter(client)),
            }

    return {
        "engine": "rules",
        "client": None,
        "tracker": tracker,
        "planner": QueryPlanner(),
        "extractor": RuleExtractor(),
        "resolver": ConflictResolver(),
    }


def build_agent(sources: list, writer=None, now=None, engine: str | None = None,
                tracker: UsageTracker | None = None) -> tuple[DdlAgent, str]:
    """返回 ``(agent, engine_name)``；engine 为 llm 或 rules。"""
    components = build_components(engine, tracker)
    agent = DdlAgent(
        sources=sources,
        extractor=components["extractor"],
        resolver=components["resolver"],
        planner=components["planner"],
        writer=writer,
        now=now,
        usage=components["tracker"],
    )
    return agent, components["engine"]
