"""冲突核查与解决。

优先用 LLM 裁决（可注入），不可用时退回规则：
来源可信度优先 → 同可信度下发布时间新者覆盖 → 追加检索（最多两轮）→
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
    TZ,
)

_MIN_TIME = datetime.min.replace(tzinfo=TZ)

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
    def __init__(self, max_rounds: int = MAX_SUPPLEMENTARY_ROUNDS, arbiter=None):
        self.max_rounds = max_rounds
        self.arbiter = arbiter

    def resolve(self, clusters: list[TaskCluster], plan: RetrievalPlan | None = None,
                retrieval: SupplementaryFn | None = None) -> ResolveResult:
        result = ResolveResult()
        for cluster in clusters:
            record, rounds = self._resolve_cluster(cluster, plan, retrieval, result.warnings)
            result.supplementary_rounds += rounds
            result.records.append(record)
        return result

    def resolve_without_verification(self, clusters: list[TaskCluster]) -> ResolveResult:
        """消融用：直接采用首个候选，不比较可信度/发布时间、不追加检索、不调用裁决模型。"""
        result = ResolveResult()
        for cluster in clusters:
            winner = cluster.candidates[0] if cluster.candidates else None
            unresolved = any(
                candidate.unresolved_time or candidate.needs_review
                for candidate in cluster.candidates
            )
            if winner is not None and winner.deadline_iso:
                status, confidence = "confirmed", 0.6
            elif unresolved:
                status, confidence = "uncertain", CONFIDENCE_UNCERTAIN
            else:
                status, confidence = "missing_deadline", CONFIDENCE_MISSING
            result.records.append(
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=winner.deadline_iso if winner else None,
                    status=status,
                    source=_describe(winner),
                    evidence_ids=[candidate.candidate_id for candidate in cluster.candidates],
                    sources=_sources(cluster.candidates),
                    confidence=confidence,
                    resolution_note="消融：未做冲突验证与追加检索，直接采用首个候选",
                )
            )
        return result

    # ------------------------------------------------------------------ #
    def _resolve_cluster(self, cluster: TaskCluster, plan: RetrievalPlan | None,
                         retrieval: SupplementaryFn | None,
                         warnings: list[str]) -> tuple[ResolvedRecord, int]:
        candidates = list(cluster.candidates)
        dated = [candidate for candidate in candidates if candidate.deadline_iso]

        if not dated:
            unresolved = any(
                candidate.unresolved_time or candidate.needs_review for candidate in candidates
            )
            winner = _best(candidates)
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=None,
                    status="uncertain" if unresolved else "missing_deadline",
                    source=_describe(winner) if winner else cluster.task_name,
                    evidence_ids=[candidate.candidate_id for candidate in candidates],
                    sources=_sources(candidates),
                    confidence=CONFIDENCE_UNCERTAIN if unresolved else CONFIDENCE_MISSING,
                    resolution_note=(
                        "时间表达无法可靠解析，标记 uncertain 交人工"
                        if unresolved
                        else "消息存在截止语义但没有可解析的日期，交由人工确认"
                    ),
                ),
                0,
            )

        unique = {candidate.deadline_iso for candidate in dated}
        if len(unique) == 1:
            if all(candidate.needs_review for candidate in dated):
                winner = _best(dated)
                return (
                    _uncertain_record(cluster, candidates, winner,
                                      "抽取置信度不足，标记 uncertain 交人工"),
                    0,
                )
            winner = _best(dated)
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=winner.deadline_iso,
                    status="confirmed",
                    source=_describe(winner),
                    evidence_ids=[candidate.candidate_id for candidate in candidates],
                    sources=_sources(candidates),
                    confidence=CONFIDENCE_CONSISTENT,
                    resolution_note="所有来源一致" if len(dated) > 1 else "单一来源",
                ),
                0,
            )

        if self.arbiter is not None:
            return self._resolve_with_llm(cluster, candidates, plan, retrieval, warnings)

        rounds_used = 0
        decision = self._decide(candidates)
        while decision is None and retrieval is not None and rounds_used < self.max_rounds:
            rounds_used += 1
            extra = self._supplement(plan, cluster, retrieval, rounds_used, warnings)
            if not extra:
                continue
            candidates.extend(extra)
            cluster.candidates = candidates
            decision = self._decide(candidates)

        if decision is not None:
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=decision.deadline_iso,
                    status="confirmed",
                    source=_describe(decision.winner),
                    evidence_ids=[candidate.candidate_id for candidate in candidates],
                    sources=_sources(candidates),
                    confidence=decision.confidence,
                    resolution_note=decision.note,
                    alternatives=_other_deadlines(candidates, decision.deadline_iso),
                ),
                rounds_used,
            )

        return (
            _uncertain_record(
                cluster, candidates, _best(candidates),
                f"存在 {len({c.deadline_iso for c in candidates})} 个互相矛盾的截止时间，"
                f"追加检索 {rounds_used} 轮仍无法判定，标记 uncertain 交人工",
            ),
            rounds_used,
        )

    # ------------------------------------------------------------------ #
    def _resolve_with_llm(self, cluster: TaskCluster, candidates: list[DeadlineCandidate],
                          plan: RetrievalPlan | None, retrieval: SupplementaryFn | None,
                          warnings: list[str]) -> tuple[ResolvedRecord, int]:
        rounds_used = 0
        arbitration = self.arbiter.arbitrate(cluster, candidates, 0)
        allow_more = plan is None or plan.allow_supplementary

        while (
            (arbitration is None or arbitration.deadline_iso is None)
            and retrieval is not None and allow_more and rounds_used < self.max_rounds
        ):
            rounds_used += 1
            extra = self._supplement(plan, cluster, retrieval, rounds_used, warnings)
            if extra:
                candidates.extend(extra)
                cluster.candidates = candidates
            arbitration = self.arbiter.arbitrate(cluster, candidates, rounds_used)

        if arbitration is not None and arbitration.deadline_iso:
            winner = next(
                (candidate for candidate in candidates
                 if candidate.deadline_iso == arbitration.deadline_iso),
                _best(candidates),
            )
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=arbitration.deadline_iso,
                    status="confirmed",
                    source=_describe(winner),
                    evidence_ids=[candidate.candidate_id for candidate in candidates],
                    sources=_sources(candidates),
                    confidence=arbitration.confidence,
                    resolution_note=f"LLM 裁决：{arbitration.reason}",
                    alternatives=_other_deadlines(candidates, arbitration.deadline_iso),
                ),
                rounds_used,
            )

        reason = arbitration.reason if arbitration is not None else "LLM 裁决不可用"
        return (
            _uncertain_record(
                cluster, candidates, _best(candidates),
                f"冲突未能可靠裁决（{reason}），追加检索 {rounds_used} 轮，标记 uncertain 交人工",
            ),
            rounds_used,
        )

    def _supplement(self, plan, cluster, retrieval, round_index, warnings) -> list[DeadlineCandidate]:
        try:
            extra = retrieval(plan, cluster, round_index)
        except Exception as exc:  # 追加检索失败不应中断整轮
            warnings.append(f"追加检索失败（{cluster.task_name} 第 {round_index} 轮）：{exc}")
            return []
        known = {candidate.candidate_id for candidate in cluster.candidates}
        return [candidate for candidate in extra if candidate.candidate_id not in known]

    # ------------------------------------------------------------------ #
    def _decide(self, candidates: list[DeadlineCandidate]) -> _Decision | None:
        dated = [candidate for candidate in candidates if candidate.deadline_iso]
        unique = {candidate.deadline_iso for candidate in dated}
        if not dated:
            return None

        if len(unique) == 1:
            winner = _best(dated)
            return _Decision(winner.deadline_iso, "所有来源一致", CONFIDENCE_CONSISTENT, winner)

        top_weight = max(_credibility(candidate) for candidate in dated)
        top_group = [candidate for candidate in dated if _credibility(candidate) == top_weight]
        if len({candidate.deadline_iso for candidate in top_group}) == 1:
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

        dated_times = [candidate.published_at for candidate in top_group
                       if candidate.published_at is not None]
        if not dated_times:
            return None
        latest = max(dated_times)
        latest_group = [candidate for candidate in top_group if candidate.published_at == latest]
        if len({candidate.deadline_iso for candidate in latest_group}) == 1:
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


def _uncertain_record(cluster: TaskCluster, candidates: list[DeadlineCandidate],
                      winner: DeadlineCandidate | None, note: str) -> ResolvedRecord:
    return ResolvedRecord(
        task_name=cluster.task_name,
        course=cluster.course,
        deadline_iso=None,
        status="uncertain",
        source=_describe(winner),
        evidence_ids=[candidate.candidate_id for candidate in candidates],
        sources=_sources(candidates),
        confidence=CONFIDENCE_UNCERTAIN,
        resolution_note=note,
        alternatives=_other_deadlines(candidates, None),
    )


def _credibility(candidate: DeadlineCandidate) -> int:
    return SOURCE_CREDIBILITY.get(candidate.source_type, 0)


def _best(candidates: list[DeadlineCandidate]) -> DeadlineCandidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda c: (
        _credibility(c), c.published_at or _MIN_TIME, c.candidate_id
    ))


def _describe(candidate: DeadlineCandidate | None) -> str:
    if candidate is None:
        return "unknown"
    return f"{candidate.source_name} ({candidate.source_type})"


def _sources(candidates: list[DeadlineCandidate]) -> list[str]:
    """保留同一任务的全部原始来源，按可信度与名称稳定排序。"""
    labels = {
        _describe(candidate): _credibility(candidate) for candidate in candidates
    }
    return sorted(labels, key=lambda label: (-labels[label], label))


def _other_deadlines(candidates: list[DeadlineCandidate], chosen: str | None) -> list[str]:
    values = sorted({c.deadline_iso for c in candidates if c.deadline_iso and c.deadline_iso != chosen})
    return values
