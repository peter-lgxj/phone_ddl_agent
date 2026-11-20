"""检索编排：逐个来源取消息，并支持冲突时的定向追加检索。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .connectors.base import MessageSource, SourceError
from .dedupe import normalize_task_name, task_similarity
from .extract.rules import RuleExtractor
from .models import DeadlineCandidate, RawMessage, RetrievalPlan, TaskCluster
from .settings import DEDUPE_THRESHOLD, TZ


class RetrievalOrchestrator:
    def __init__(self, sources: list[MessageSource], extractor: RuleExtractor | None = None,
                 now: datetime | None = None):
        self.sources = sources
        self.extractor = extractor or RuleExtractor()
        self.now = now or datetime.now(TZ)
        self.warnings: list[str] = []
        self.message_count = 0

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        pairs = self.fetch_sources(plan)
        messages = [message for _, items in pairs for message in items]
        self.message_count = len(messages)
        return messages

    def fetch_sources(self, plan: RetrievalPlan) -> list[tuple[MessageSource, list[RawMessage]]]:
        """按来源返回消息，供内容粗扫在选源前复用同一次读取。"""
        pairs: list[tuple[MessageSource, list[RawMessage]]] = []
        for source in plan.sources or self.sources:
            try:
                pairs.append((source, list(source.fetch(plan))))
            except SourceError as exc:
                self.warnings.append(f"{source.name}: {exc}")
                pairs.append((source, []))
        self.message_count = sum(len(items) for _, items in pairs)
        return pairs

    def supplementary(self, plan: RetrievalPlan | None, cluster: TaskCluster,
                      round_index: int) -> list[DeadlineCandidate]:
        """冲突未决时，以任务名+关键词向所有已选来源再查一轮。"""
        if plan is None:
            return []
        focused = replace(
            plan,
            round_index=round_index,
            focus_task=cluster.task_name,
            keywords=list(dict.fromkeys([*plan.keywords, cluster.task_name])),
        )
        messages = self.fetch(focused)
        candidates = self.extractor.extract(messages, now=self.now)

        target = normalize_task_name(cluster.task_name)
        matched: list[DeadlineCandidate] = []
        for candidate in candidates:
            name = normalize_task_name(candidate.task_name_raw)
            same_course = (
                not cluster.course or not candidate.course
                or cluster.course.upper() == candidate.course.upper()
            )
            if name == target or (same_course and task_similarity(name, target) >= DEDUPE_THRESHOLD):
                matched.append(candidate)
        return matched
