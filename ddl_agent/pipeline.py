"""流水线入口：规划 → 检索 → 抽取 → 归一化 → 去重 → 冲突 → 写入。"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

from .conflict import ConflictResolver
from .connectors.base import MessageSource
from .dedupe import Deduplicator, clusters_without_dedup
from .extract.normalizer import TimeNormalizer
from .extract.rules import RuleExtractor
from .models import AgentRunResult, RetrievalPlan
from .planner import QueryPlanner, SourceConfigParser
from .retrieval import RetrievalOrchestrator
from .settings import TZ
from .writer.base import DeadlineWriter


class DdlAgent:
    """按自然语言查询，从多个来源汇总 DDL 并写入共享文档。"""

    def __init__(self, sources: list[MessageSource], extractor=None,
                 deduplicator: Deduplicator | None = None,
                 resolver: ConflictResolver | None = None,
                 writer: DeadlineWriter | None = None,
                 now: datetime | None = None,
                 normalizer: TimeNormalizer | None = None,
                 planner=None,
                 usage=None,
                 dedupe_enabled: bool = True,
                 verify_enabled: bool = True,
                 fixed_sources: bool = False):
        self.sources = sources
        self.now = now or datetime.now(TZ)
        self.normalizer = normalizer or TimeNormalizer()
        self.extractor = extractor or RuleExtractor(self.normalizer)
        self.deduplicator = deduplicator or Deduplicator()
        self.resolver = resolver or ConflictResolver()
        self.writer = writer
        self.source_parser = SourceConfigParser()
        self.planner = planner or QueryPlanner()
        self.usage = usage
        self.dedupe_enabled = dedupe_enabled
        self.verify_enabled = verify_enabled
        self.fixed_sources = fixed_sources
        self.orchestrator = RetrievalOrchestrator(sources, self.extractor, self.now)

    # ------------------------------------------------------------------ #
    def run(self, query: str, source_config: str = "",
            plan: RetrievalPlan | None = None, write: bool = True) -> AgentRunResult:
        started = perf_counter()
        stages: dict[str, float] = {}
        usage_before = self.usage.snapshot() if self.usage is not None else None

        begin = perf_counter()
        selected = (
            list(self.sources) if self.fixed_sources
            else self.source_parser.parse(source_config, self.sources)
        )
        plan = plan or self.planner.plan(query, selected, self.now)
        stages["plan"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        messages = self.orchestrator.fetch(plan)
        stages["retrieve"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        candidates = self.extractor.extract(messages, now=self.now)
        stages["extract"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        clusters = (
            self.deduplicator.cluster(candidates) if self.dedupe_enabled
            else clusters_without_dedup(candidates)
        )
        stages["dedupe"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        if self.verify_enabled:
            outcome = self.resolver.resolve(clusters, plan=plan,
                                            retrieval=self.orchestrator.supplementary)
        else:
            outcome = self.resolver.resolve_without_verification(clusters)
        records = [record for record in outcome.records if _within_window(record, plan)]
        stages["resolve"] = round(perf_counter() - begin, 4)

        write_result = None
        begin = perf_counter()
        if write and self.writer is not None:
            write_result = self.writer.upsert(records, timestamp=self.now)
        stages["write"] = round(perf_counter() - begin, 4)

        tokens = sum(len(message.subject) + len(message.body) for message in messages) // 4
        usage = self.usage.since(usage_before) if self.usage is not None else {}
        trace = {
            "stages": stages,
            "elapsed_seconds": round(perf_counter() - started, 4),
            "message_count": len(messages),
            "candidate_count": len(candidates),
            "cluster_count": len(clusters),
            "record_count": len(records),
            "supplementary_rounds": outcome.supplementary_rounds,
            "planner_reason": plan.reason,
            "planner": type(self.planner).__name__,
            "ablation": {
                "dedupe": self.dedupe_enabled,
                "verify": self.verify_enabled,
                "fixed_sources": self.fixed_sources,
            },
            "llm_calls": usage.get("llm_calls", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "tokens_estimate": usage.get("tokens_estimate", tokens) or tokens,
            "llm_seconds": usage.get("llm_seconds", 0.0),
            "cost_usd": usage.get("cost_usd", 0.0),
        }

        return AgentRunResult(
            query=query,
            records=records,
            plan=plan,
            write_result=write_result,
            warnings=[*self.orchestrator.warnings, *outcome.warnings],
            trace=trace,
        )


def _within_window(record, plan: RetrievalPlan) -> bool:
    """用户提到「下周/本月」时，按最终截止时间筛记录；缺时间的记录保留给人工。"""
    if record.deadline_iso is None:
        return True
    try:
        deadline = datetime.fromisoformat(record.deadline_iso)
    except ValueError:
        return True
    if plan.date_from and deadline < plan.date_from:
        return False
    if plan.date_to and deadline > plan.date_to:
        return False
    return True
