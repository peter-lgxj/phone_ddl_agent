"""冲突核查与解决。

规则（不再给任何来源固定高低先验）：
1. 同来源内出现「更正/延期/改为/以最新为准」时，按发布时间（相同则按读取顺序）
   取最新一条覆盖旧的，旧值写入 ``superseded_by``，只输出生效值；判断不出先后
   标 ``needs_review``，不并列输出。
2. 跨来源冲突按同权起算，用发布时间与显式更正/延期语义裁决；裁决结果附带
   两侧原文、各自时间与依据（``conflicts``）。
3. 规则无法裁决时才调用 LLM 裁决，仍无法判定则标 ``uncertain`` 交人工。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .extract.message_context import normalize_source_type
from .models import DeadlineCandidate, ResolvedRecord, RetrievalPlan, TaskCluster
from .settings import (
    CONFIDENCE_CONSISTENT,
    CONFIDENCE_MISSING,
    CONFIDENCE_NEWER_ANNOUNCEMENT,
    CONFIDENCE_UNCERTAIN,
    MAX_SUPPLEMENTARY_ROUNDS,
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
            _stamp_evidence_time(record, cluster)
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
            record = ResolvedRecord(
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
            _stamp_evidence_time(record, cluster)
            result.records.append(record)
        return result

    # ------------------------------------------------------------------ #
    def _resolve_cluster(self, cluster: TaskCluster, plan: RetrievalPlan | None,
                         retrieval: SupplementaryFn | None,
                         warnings: list[str]) -> tuple[ResolvedRecord, int]:
        candidates = list(cluster.candidates)
        kept, superseded, ambiguous = self._apply_same_source_updates(candidates)
        if ambiguous:
            return (
                _uncertain_record(
                    cluster, candidates, _best(candidates), ambiguous,
                    superseded=superseded, include_alternatives=False,
                ),
                0,
            )

        dated = [candidate for candidate in kept if candidate.deadline_iso]

        if not dated:
            unresolved = any(
                candidate.unresolved_time or candidate.needs_review for candidate in kept
            )
            winner = _best(kept)
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=None,
                    status="uncertain" if unresolved else "missing_deadline",
                    source=_describe(winner) if winner else cluster.task_name,
                    evidence_ids=[candidate.candidate_id for candidate in kept],
                    sources=_sources(kept),
                    confidence=CONFIDENCE_UNCERTAIN if unresolved else CONFIDENCE_MISSING,
                    resolution_note=(
                        "时间表达无法可靠解析，标记 uncertain 交人工"
                        if unresolved
                        else "消息存在截止语义但没有可解析的日期，交由人工确认"
                    ),
                    superseded_by=superseded,
                ),
                0,
            )

        unique = {candidate.deadline_iso for candidate in dated}
        if len(unique) == 1:
            if all(candidate.needs_review for candidate in dated):
                winner = _best(dated)
                return (
                    _uncertain_record(cluster, kept, winner,
                                      "抽取置信度不足，标记 uncertain 交人工",
                                      superseded=superseded),
                    0,
                )
            winner = _best(dated)
            note = "单一来源" if len(dated) == 1 else "所有来源一致"
            if superseded:
                note = (
                    "同源更正/延期：采用最新一条生效值，"
                    "旧值保留在 superseded_by，未并列输出"
                )
            return (
                ResolvedRecord(
                    task_name=cluster.task_name,
                    course=cluster.course,
                    deadline_iso=winner.deadline_iso,
                    status="confirmed",
                    source=_describe(winner),
                    evidence_ids=[candidate.candidate_id for candidate in kept],
                    sources=_sources(kept),
                    confidence=CONFIDENCE_CONSISTENT,
                    resolution_note=note,
                    superseded_by=superseded,
                    inferred=winner.inferred,
                    inference_basis=winner.inference_basis,
                ),
                0,
            )

        decision = self._decide(kept)
        if decision is not None:
            return (
                self._confirmed_record(cluster, kept, decision, superseded),
                0,
            )

        if self.arbiter is not None:
            return self._resolve_with_llm(cluster, kept, plan, retrieval, warnings, superseded)

        rounds_used = 0
        while decision is None and retrieval is not None and rounds_used < self.max_rounds:
            rounds_used += 1
            extra = self._supplement(plan, cluster, retrieval, rounds_used, warnings)
            if not extra:
                continue
            candidates.extend(extra)
            cluster.candidates = candidates
            kept, superseded, ambiguous = self._apply_same_source_updates(candidates)
            if ambiguous:
                return (
                    _uncertain_record(
                        cluster, candidates, _best(candidates), ambiguous,
                        superseded=superseded, include_alternatives=False,
                    ),
                    rounds_used,
                )
            decision = self._decide(kept)

        if decision is not None:
            return (
                self._confirmed_record(cluster, kept, decision, superseded),
                rounds_used,
            )

        return (
            _uncertain_record(
                cluster, candidates, _best(candidates),
                f"存在 {len({c.deadline_iso for c in candidates})} 个互相矛盾的截止时间，"
                f"追加检索 {rounds_used} 轮仍无法判定，标记 uncertain 交人工",
                superseded=superseded,
            ),
            rounds_used,
        )

    def _confirmed_record(self, cluster: TaskCluster, candidates: list[DeadlineCandidate],
                          decision: _Decision, superseded: list[dict]) -> ResolvedRecord:
        conflicts = _build_conflicts(cluster, candidates, decision)
        return ResolvedRecord(
            task_name=cluster.task_name,
            course=cluster.course,
            deadline_iso=decision.deadline_iso,
            status="confirmed",
            source=_describe(decision.winner),
            evidence_ids=[candidate.candidate_id for candidate in candidates],
            sources=_sources(candidates),
            confidence=decision.confidence,
            resolution_note=decision.note,
            # 冲突两侧已在 conflicts 里留档，主字段只保留生效值
            alternatives=[] if conflicts else _other_deadlines(
                candidates, decision.deadline_iso
            ),
            superseded_by=superseded,
            conflicts=conflicts,
            inferred=decision.winner.inferred,
            inference_basis=decision.winner.inference_basis,
        )

    # ------------------------------------------------------------------ #
    def _resolve_with_llm(self, cluster: TaskCluster, candidates: list[DeadlineCandidate],
                          plan: RetrievalPlan | None, retrieval: SupplementaryFn | None,
                          warnings: list[str],
                          superseded: list[dict] | None = None) -> tuple[ResolvedRecord, int]:
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
            decision = _Decision(
                deadline_iso=arbitration.deadline_iso,
                note=f"LLM 裁决：{arbitration.reason}",
                confidence=arbitration.confidence,
                winner=winner,
            )
            record = self._confirmed_record(cluster, candidates, decision, superseded or [])
            record.alternatives = _other_deadlines(candidates, arbitration.deadline_iso)
            return record, rounds_used

        reason = arbitration.reason if arbitration is not None else "LLM 裁决不可用"
        return (
            _uncertain_record(
                cluster, candidates, _best(candidates),
                f"冲突未能可靠裁决（{reason}），追加检索 {rounds_used} 轮，标记 uncertain 交人工",
                superseded=superseded or [],
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
    def _apply_same_source_updates(self, candidates: list[DeadlineCandidate]
                                   ) -> tuple[list[DeadlineCandidate], list[dict], str | None]:
        """按来源分组应用「最新一条覆盖旧值」，返回（有效候选, 旧值记录, 无法判定说明）。"""
        groups: dict[str, list[DeadlineCandidate]] = {}
        for candidate in candidates:
            groups.setdefault(candidate.source_name, []).append(candidate)

        kept: list[DeadlineCandidate] = []
        superseded: list[dict] = []
        ambiguous_note: str | None = None

        for group in groups.values():
            dated = [candidate for candidate in group if candidate.deadline_iso]
            if len({candidate.deadline_iso for candidate in dated}) <= 1:
                kept.extend(group)
                continue
            if not any(candidate.is_correction for candidate in dated):
                # 同来源内没有「更正/延期」语义，交给跨来源规则或人工，不擅自覆盖
                kept.extend(group)
                continue

            order_known = any(candidate.source_order is not None for candidate in dated)
            time_known = len({candidate.published_at for candidate in dated}) > 1
            if not order_known and not time_known:
                ambiguous_note = (
                    "同一来源内出现多条互相矛盾的截止时间，但读不到发布时间/顺序，"
                    "无法判断哪条最新，标记 needs_review 交人工"
                )
                kept.extend(group)
                continue

            winner = max(dated, key=_time_key)
            losers = [
                candidate for candidate in dated
                if candidate.deadline_iso != winner.deadline_iso
            ]
            if not losers:
                kept.extend(group)
                continue
            for loser in losers:
                loser.superseded_by = winner.candidate_id
            superseded.extend(_superseded_entries(losers, winner))
            kept.extend(candidate for candidate in group if candidate not in losers)

        return kept, _dedupe_superseded(superseded), ambiguous_note

    # ------------------------------------------------------------------ #
    def _decide(self, candidates: list[DeadlineCandidate]) -> _Decision | None:
        dated = [candidate for candidate in candidates if candidate.deadline_iso]
        if not dated:
            return None

        unique = {candidate.deadline_iso for candidate in dated}
        if len(unique) == 1:
            winner = _best(dated)
            return _Decision(winner.deadline_iso, "所有来源一致", CONFIDENCE_CONSISTENT, winner)

        # 显式更正/延期语义优先，但只有它不是更旧时才采用
        corrections = [candidate for candidate in dated if candidate.is_correction]
        if corrections and len(corrections) < len(dated):
            winner = max(corrections, key=_time_key)
            others = [candidate for candidate in dated if candidate is not winner]
            if all(_time_key(winner) > _time_key(other) for other in others):
                return _Decision(
                    deadline_iso=winner.deadline_iso,
                    note=(
                        f"采用含「{winner.correction_marker or '更正'}」表述的最新来源"
                        f"（{winner.source_name}，{_time_text(winner)}），旧值视为被取代"
                    ),
                    confidence=CONFIDENCE_NEWER_ANNOUNCEMENT,
                    winner=winner,
                )

        # 同权的时间裁决：发布时间最新者生效，不再给邮件/群聊固定高低先验
        timed = [candidate for candidate in dated if candidate.published_at is not None]
        if len({candidate.published_at for candidate in timed}) >= 2:
            latest = max(_time_key(candidate) for candidate in timed)
            latest_group = [
                candidate for candidate in timed if _time_key(candidate) == latest
            ]
            if len({candidate.deadline_iso for candidate in latest_group}) == 1:
                winner = _best(latest_group)
                return _Decision(
                    deadline_iso=winner.deadline_iso,
                    note=(
                        f"按发布时间更新裁决：采用最新（{winner.source_name}，"
                        f"{_time_text(winner)}），较早的 {len(dated) - len(latest_group)} 条视为旧值"
                    ),
                    confidence=CONFIDENCE_NEWER_ANNOUNCEMENT,
                    winner=winner,
                )

        # 同权多数：某个时间被更多独立来源印证时采用它（仍不引入来源高低先验）
        support: dict[str, set[str]] = {}
        for candidate in dated:
            support.setdefault(candidate.deadline_iso, set()).add(candidate.source_name)
        ranked = sorted(support.items(), key=lambda item: len(item[1]), reverse=True)
        if len(ranked) >= 2 and len(ranked[0][1]) > len(ranked[1][1]):
            deadline, sources = ranked[0]
            winner = _best(
                [candidate for candidate in dated if candidate.deadline_iso == deadline]
            )
            return _Decision(
                deadline_iso=deadline,
                note=(
                    f"同权多数：{len(sources)} 个独立来源支持该时间，"
                    f"其余 {len(dated) - len(sources)} 条视为少数证据"
                ),
                confidence=CONFIDENCE_NEWER_ANNOUNCEMENT,
                winner=winner,
            )

        return None


def _time_key(candidate: DeadlineCandidate) -> tuple[datetime, int]:
    return (candidate.published_at or _MIN_TIME, candidate.source_order or 0)


def _stamp_evidence_time(record: ResolvedRecord, cluster: TaskCluster) -> None:
    """把最近一条证据的发布时间/读取顺序写到记录上，供“最近一条”查询排序。"""
    evidence = set(record.evidence_ids)
    candidates = [
        candidate for candidate in cluster.candidates
        if candidate.candidate_id in evidence
    ] or list(cluster.candidates)
    latest = max(candidates, key=_time_key) if candidates else None
    if latest is None:
        return
    if latest.published_at is not None:
        record.latest_published_at = latest.published_at.isoformat()
    record.latest_source_order = latest.source_order


def _time_text(candidate: DeadlineCandidate) -> str:
    if candidate.published_at is None:
        return "发布时间未知"
    return candidate.published_at.isoformat()


def _superseded_entries(losers: list[DeadlineCandidate], winner: DeadlineCandidate) -> list[dict]:
    """按 (截止时间, 原文) 合并重复旧值，保留完整证据 id。"""
    merged: dict[tuple[str, str], dict] = {}
    for loser in losers:
        key = (loser.deadline_iso or "", loser.deadline_raw or "")
        entry = merged.setdefault(
            key,
            {
                "deadline": loser.deadline_iso,
                "deadline_raw": loser.deadline_raw,
                "sources": [],
                "published_at": [],
                "message_ids": [],
                "evidence_ids": [],
                "superseded_by": winner.candidate_id,
                "reason": f"同源「{winner.correction_marker or '更新'}」覆盖旧值",
            },
        )
        entry["sources"].append(_describe(loser))
        entry["message_ids"].append(loser.message_id)
        entry["evidence_ids"].append(loser.candidate_id)
        if loser.published_at is not None:
            entry["published_at"].append(loser.published_at.isoformat())
    return list(merged.values())


def _dedupe_superseded(entries: list[dict]) -> list[dict]:
    """追加检索后可能重复应用同源覆盖，这里按证据集合去重。"""
    seen: set[tuple[str, ...]] = set()
    result: list[dict] = []
    for entry in entries:
        key = tuple(sorted(entry.get("evidence_ids") or []))
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _build_conflicts(cluster: TaskCluster, candidates: list[DeadlineCandidate],
                     decision: _Decision) -> list[dict]:
    dated = [candidate for candidate in candidates if candidate.deadline_iso]
    if len({candidate.deadline_iso for candidate in dated}) <= 1:
        return []
    sides = [
        {
            "source": _describe(candidate),
            "source_type": candidate.source_type,
            "source_name": candidate.source_name,
            "deadline": candidate.deadline_iso,
            "deadline_raw": candidate.deadline_raw,
            # 只用真实发布时间；解析不到就留空并标注，不用读屏时刻填充
            "published_at": (
                candidate.published_at.isoformat() if candidate.published_at else None
            ),
            "published_at_note": "" if candidate.published_at else "发布时间未知",
            "message_id": candidate.message_id,
            "evidence_id": candidate.candidate_id,
            "course_from": candidate.course_from,
            "is_correction": candidate.is_correction,
        }
        for candidate in dated
    ]
    return [
        {
            "task_name": cluster.task_name,
            "course": cluster.course,
            "effective": decision.deadline_iso,
            "winner": _describe(decision.winner),
            "sides": sides,
            "basis": decision.note,
        }
    ]


def _uncertain_record(cluster: TaskCluster, candidates: list[DeadlineCandidate],
                      winner: DeadlineCandidate | None, note: str,
                      *, superseded: list[dict] | None = None,
                      include_alternatives: bool = True) -> ResolvedRecord:
    # 即使无法裁决，也把冲突两侧（含真实发布时间）留给人工复核
    conflicts = []
    if len({candidate.deadline_iso for candidate in candidates if candidate.deadline_iso}) > 1:
        decision = _Decision(
            deadline_iso="",
            note=note,
            confidence=CONFIDENCE_UNCERTAIN,
            winner=winner or candidates[0],
        )
        conflicts = _build_conflicts(cluster, candidates, decision)
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
        alternatives=_other_deadlines(candidates, None) if include_alternatives else [],
        superseded_by=superseded or [],
        conflicts=conflicts,
    )


def _best(candidates: list[DeadlineCandidate]) -> DeadlineCandidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (_time_key(candidate), candidate.candidate_id))


def _describe(candidate: DeadlineCandidate | None) -> str:
    if candidate is None:
        return "unknown"
    return f"{candidate.source_name} ({normalize_source_type(candidate.source_type)})"


def _sources(candidates: list[DeadlineCandidate]) -> list[str]:
    """保留同一任务的全部原始来源；按名称稳定排序，不做来源高低先验。"""
    return sorted({_describe(candidate) for candidate in candidates})


def _other_deadlines(candidates: list[DeadlineCandidate], chosen: str | None) -> list[str]:
    values = sorted({c.deadline_iso for c in candidates if c.deadline_iso and c.deadline_iso != chosen})
    return values
