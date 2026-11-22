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
from .planner import (
    QueryPlanner,
    SourceConfigParser,
    expand_similar_sources,
    select_sources,
)
from .query_focus import (
    focus_records,
    pinned_message_ids,
    select_sources_by_content,
    task_hints,
    wants_pinned_query,
)
from .retrieval import RetrievalOrchestrator
from .settings import TZ
from .writer.base import DeadlineWriter


def recover_pinned_candidates(query: str, messages: list, candidates: list,
                              now: datetime) -> list:
    """置顶查询的确定性兜底：LLM 漏抽置顶消息时，用规则抽取补回目标。

    置顶消息由 ``pinned_message_ids``（元数据/正文标记/横幅邻接）定位；没有这些
    信号时取列表顶部 3 条，交给 ``focus_records`` 的「顶部 + 时间最早」兜底。
    只合并带截止时间的规则候选，补不出就保持空，由 pipeline 标记 needs_review。
    """
    if not wants_pinned_query(query) or not messages:
        return candidates
    pinned_ids = pinned_message_ids(messages)
    targets = (
        [message for message in messages if message.message_id in pinned_ids]
        if pinned_ids else list(messages)[:3]
    )
    if not targets:
        return candidates
    covered = {
        str(candidate.message_id) for candidate in candidates
        if candidate.deadline_iso
    }
    if all(str(message.message_id) in covered for message in targets):
        return candidates

    recovered = RuleExtractor().extract(targets, now=now)
    recovered = recover_relative_deadlines(recovered, now)
    seen = {
        (str(candidate.message_id), candidate.task_name_raw, candidate.deadline_iso)
        for candidate in candidates
    }
    for candidate in recovered:
        if not candidate.deadline_iso:
            continue
        key = (str(candidate.message_id), candidate.task_name_raw, candidate.deadline_iso)
        if key in seen:
            continue
        candidates.append(candidate)
        seen.add(key)
    return candidates


def recover_relative_deadlines(candidates: list, now: datetime,
                               normalizer: TimeNormalizer | None = None) -> list:
    """把抽取器留空的相对时间（如「月底前」）用确定性归一化补回。

    LLM 抽取器可能按「有歧义不要猜」直接给 null；但「月底前/本月底/下月初」
    是可推断的，这里统一补成具体日期并保留 ``inferred`` 与依据。真正无法确定
    的表达（如「考完试那周」）归一化仍返回空，保持 null + uncertain。
    """
    normalizer = normalizer or TimeNormalizer()
    for candidate in candidates:
        if candidate.deadline_iso or not candidate.deadline_raw:
            continue
        normalized = normalizer.normalize(candidate.deadline_raw, now=now)
        if not normalized.iso:
            continue
        candidate.deadline_iso = normalized.iso
        candidate.unresolved_time = False
        candidate.inferred = normalized.inferred
        candidate.inference_basis = normalized.inference_basis
        candidate.missing_fields = [
            field for field in candidate.missing_fields if field != "deadline"
        ]
    return candidates


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
        planner_sources = list(plan.sources)
        selection: list[dict] = []
        hints = task_hints(query)
        # 只有查询点名了具体任务时才扩到全部候选来源做内容粗扫；
        # 课程泛查（D10/D16）仍按规划器/相似群规则走，避免把同课其他群带进来。
        probe_enabled = not self.fixed_sources and bool(hints)
        if not self.fixed_sources:
            # LLM 规划器可能只回相似群中的一个；先把同组候选补齐再比较，
            # 否则「基名优先」没有另一个候选可比，D16 会一直选中带年份后缀的诱饵群。
            pool = selected if probe_enabled else expand_similar_sources(
                plan.sources, selected
            )
            plan.sources, selection = select_sources(query, pool)
        stages["plan"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        source_messages = self.orchestrator.fetch_sources(plan)
        content_decision: dict = {"enabled": False}
        if probe_enabled:
            chosen, content_decision = select_sources_by_content(
                query, source_messages, fallback_sources=planner_sources
            )
            if chosen:
                chosen_ids = {id(source) for source in chosen}
                source_messages = [
                    (source, messages) for source, messages in source_messages
                    if id(source) in chosen_ids
                ]
                plan.sources = chosen
        messages = [message for _, items in source_messages for message in items]
        stages["retrieve"] = round(perf_counter() - begin, 4)

        begin = perf_counter()
        candidates = self.extractor.extract(messages, now=self.now)
        candidates = recover_relative_deadlines(candidates, self.now, self.normalizer)
        candidates = recover_pinned_candidates(query, messages, candidates, self.now)
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
        # 查询只问某个任务时，同群其他 DDL 不进最终输出；没有明确任务时不过滤。
        # 置顶查询额外只保留证据落在带「置顶」标记消息上的记录。
        message_order = {
            message.message_id: index for index, message in enumerate(messages)
        }
        records, focus_decision = focus_records(
            query, records,
            pinned_ids=pinned_message_ids(messages),
            message_order=message_order,
        )
        stages["resolve"] = round(perf_counter() - begin, 4)

        # 读取为空/读取失败与「确实读了但没有目标」必须分开：
        # 前者交人工复核（needs_review），后者才是 no_match。
        read_empty = not messages
        needs_review = False
        review_reason = ""
        if read_empty:
            needs_review = True
            review_reason = "未读到任何消息（读取为空或来源失败），无法解析，需人工复核"
            focus_decision["no_match"] = False
            focus_decision["no_match_reason"] = "read_empty"
        elif not records and not focus_decision.get("no_match"):
            needs_review = True
            review_reason = "已读到消息但未解析出可用的截止时间，需人工复核"

        # 置顶查询补不出置顶目标时，不能把同群其他 DDL 当答案放出去。
        if wants_pinned_query(query):
            pinned_filter = focus_decision.get("pinned_filter")
            if not records or pinned_filter in ("no_pinned_record", "no_marker"):
                records = []
                focus_decision["after"] = 0
                focus_decision["noise_suppressed"] = True
                if not read_empty:
                    needs_review = True
                    review_reason = "置顶消息未能解析出可用截止时间，需人工复核"
            elif all(not getattr(record, "deadline_iso", None) for record in records):
                focus_decision["noise_suppressed"] = True
                needs_review = True
                review_reason = "置顶消息未能解析出可用截止时间，需人工复核"

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
            # 相似群名的取舍依据写进归档，便于复现“为什么只扫了这个群”
            "source_selection": selection,
            # 内容粗扫选源与查询聚焦过滤的依据
            "content_probe": content_decision,
            "focus_filter": focus_decision,
            # 明确任务在语料里不存在时：records 为空且 no_match=true
            "no_match": bool(focus_decision.get("no_match")),
            # 读取为空/失败/读到但解析不出时间：交人工，不用 no_match 冒充
            "read_state": "empty" if read_empty else "ok",
            "read_failed": bool(self.orchestrator.warnings),
            "needs_review": needs_review,
            "review_reason": review_reason,
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
            needs_review=needs_review,
            review_reason=review_reason,
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
