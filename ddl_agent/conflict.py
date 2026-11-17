"""冲突核查与解决。

规则顺序：来源可信度优先 → 同可信度下发布时间新者覆盖 → 追加检索（最多两轮）→
仍无法判定则标 ``uncertain`` 交人工，绝不随便挑一个时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .models import DeadlineCandidate, ResolvedRecord, RetrievalPlan, TaskCluster
from .settings import (
    CONFIDENCE_CONSISTENT,
    CONFIDENCE_MISSING,
    CONFIDENCE_NEWER_ANNOUNCEMENT,
    CONFIDENCE_SOURCE_PRIORITY,
    CONFIDENCE_UNCERTAIN,
    MAX_SUPPLEMENTARY_ROUNDS,
    SOURCE_CREDIBILITY,
)

SupplementaryFn = Callable[[RetrievalPlan | None, TaskCluster, int], list[DeadlineCandidate]]


@dataclass
class ResolveResult:
    records: list[ResolvedRecord] = field(default_factory=list)
    supplementary_rounds: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Decision:
    deadline_iso: str
    note: str
    confidence: float
    winner: DeadlineCandidate


class ConflictResolver:
    def __init__(self, max_rounds: int = MAX_SUPPLEMENTARY_ROUNDS):
        self.max_rounds = max_rounds

    def resolve(self, clusters: list[TaskCluster], plan: RetrievalPlan | None = None,
                retrieval: SupplementaryFn | None = None) -> ResolveResult:
        result = ResolveResult()
        for cluster in clusters:
            record, rounds = self._resolve_cluster(cluster, plan, retrieval, result.warnings)
            result.supplementary_rounds += rounds
            result.records.append(record)
        return result

    # ------------------------------------------------------------------ #
    def _resolve_cluster(self, cluster: TaskCluster, plan: RetrievalPlan | None,
                         retrieval: SupplementaryFn | None,
                         warnings: list[str]) -> tuple[ResolvedRecord, int]:
        candidates = list(cluster.candidates)
        dated = [c for c in candidates if c.deadline_iso]

        if not dated:
            winner = _best(candidates)
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=None,
                    status="missing_deadline",
                    source=_describe(winner) if winner else cluster.task_name,
                    evidence_ids=[c.candidate_id for c in candidates],
                    confidence=CONFIDENCE_MISSING,
                    resolution_note="消息存在截止语义但没有可解析的日期，交由人工确认",
                ),
                0,
            )

        rounds_used = 0
        decision = self._decide(cluster, candidates)

        while decision is None and retrieval is not None and rounds_used < self.max_rounds:
            rounds_used += 1
            try:
                extra = retrieval(plan, cluster, rounds_used)
            except Exception as exc:  # 追加检索失败不应中断整轮
                warnings.append(f"追加检索失败（{cluster.task_name} 第 {rounds_used} 轮）：{exc}")
                extra = []

            known = {c.candidate_id for c in candidates}
            fresh = [c for c in extra if c.candidate_id not in known]
            if not fresh:
                continue
            candidates.extend(fresh)
            if fresh:
                cluster.candidates = candidates
            decision = self._decide(cluster, candidates)

        if decision is not None:
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=decision.deadline_iso,
                    status="confirmed",
                    source=_describe(decision.winner),
                    evidence_ids=[c.candidate_id for c in candidates],
                    confidence=decision.confidence,
                    resolution_note=decision.note,
                    alternatives=_other_deadlines(candidates, decision.deadline_iso),
                ),
                rounds_used,
            )

        return (
            ResolvedRecord(
                task_name=cluster.task_name,
                course=cluster.course,
                deadline_iso=None,
                status="uncertain",
                source=_describe(_best(candidates)),
                evidence_ids=[c.candidate_id for c in candidates],
                confidence=CONFIDENCE_UNCERTAIN,
                resolution_note=(
                    f"存在 {len({c.deadline_iso for c in candidates})} 个互相矛盾的截止时间，"
                    f"追加检索 {rounds_used} 轮仍无法判定，标记 uncertain 交人工"
                ),
                alternatives=_other_deadlines(candidates, None),
            ),
            rounds_used,
        )

    # ------------------------------------------------------------------ #
    def _decide(self, cluster: TaskCluster, candidates: list[DeadlineCandidate]) -> _Decision | None:
        dated = [c for c in candidates if c.deadline_iso]
        unique = {c.deadline_iso for c in dated}
        if not dated:
            return None

        if len(unique) == 1:
            winner = _best(dated)
            return _Decision(
                deadline_iso=winner.deadline_iso,
                note="所有来源一致" if len(dated) > 1 else "单一来源",
                confidence=CONFIDENCE_CONSISTENT,
                winner=winner,
            )

        top_weight = max(_credibility(c) for c in dated)
        top_group = [c for c in dated if _credibility(c) == top_weight]
        top_unique = {c.deadline_iso for c in top_group}

        if len(top_unique) == 1:
            winner = _best(top_group)
            return _Decision(
                deadline_iso=winner.deadline_iso,
                note=(
                    f"采用最高可信度来源（{winner.source_type}）的时间，"
                    f"覆盖 {len(dated) - len(top_group)} 条较低可信度来源"
                ),
                confidence=CONFIDENCE_SOURCE_PRIORITY,
                winner=winner,
            )

        latest = max(c.published_at for c in top_group)
        latest_group = [c for c in top_group if c.published_at == latest]
        latest_unique = {c.deadline_iso for c in latest_group}
        if len(latest_unique) == 1:
            winner = _best(latest_group)
            return _Decision(
                deadline_iso=winner.deadline_iso,
                note=(
                    f"同等可信度下采用发布时间更新的公告（{winner.published_at.isoformat()}），"
                    "较旧的截止时间视为被取代"
                ),
                confidence=CONFIDENCE_NEWER_ANNOUNCEMENT,
                winner=winner,
            )

        return None


def _credibility(candidate: DeadlineCandidate) -> int:
    return SOURCE_CREDIBILITY.get(candidate.source_type, 0)


def _best(candidates: list[DeadlineCandidate]) -> DeadlineCandidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda c: (_credibility(c), c.published_at, c.candidate_id))


def _describe(candidate: DeadlineCandidate | None) -> str:
    if candidate is None:
        return "unknown"
    return f"{candidate.source_name} ({candidate.source_type})"


def _other_deadlines(candidates: list[DeadlineCandidate], chosen: str | None) -> list[str]:
    values = sorted({c.deadline_iso for c in candidates if c.deadline_iso and c.deadline_iso != chosen})
    return values
