"""单次直出 baseline：同一批原始消息一次性喂进去，不做规划/去重/冲突核查。"""

from __future__ import annotations

import time
from datetime import datetime

from ..extract.rules import RuleExtractor
from ..models import RawMessage, ResolvedRecord
from ..settings import (
    DEFAULT_INPUT_PRICE_PER_1K,
    DEFAULT_OUTPUT_PRICE_PER_1K,
    TZ,
)


class SinglePassBaseline:
    """与 agent 使用完全相同的原始消息，只做一次“抽取 + 直接输出”。"""

    def __init__(self, extractor: RuleExtractor | None = None,
                 input_price_per_1k: float = DEFAULT_INPUT_PRICE_PER_1K,
                 output_price_per_1k: float = DEFAULT_OUTPUT_PRICE_PER_1K):
        self.extractor = extractor or RuleExtractor()
        self.input_price_per_1k = input_price_per_1k
        self.output_price_per_1k = output_price_per_1k

    def run(self, messages: list[RawMessage],
            now: datetime | None = None) -> tuple[list[ResolvedRecord], dict]:
        started = time.perf_counter()
        now = now or datetime.now(TZ)
        candidates = self.extractor.extract(messages, now=now)

        records = [
            ResolvedRecord(
                task_name=candidate.task_name_raw,
                course=candidate.course,
                deadline_iso=candidate.deadline_iso,
                status="confirmed" if candidate.deadline_iso else "missing_deadline",
                source=f"{candidate.source_name} ({candidate.source_type})",
                evidence_ids=[candidate.candidate_id],
                confidence=0.6 if candidate.deadline_iso else 0.3,
                resolution_note="single-pass baseline：未去重、未做冲突核查",
            )
            for candidate in candidates
        ]

        input_chars = sum(len(message.subject) + len(message.body) for message in messages)
        output_chars = sum(len(record.task_name) + len(record.resolution_note) for record in records)
        tokens_in = max(input_chars // 4, 1)
        tokens_out = max(output_chars // 4, 1)
        cost = (tokens_in / 1000) * self.input_price_per_1k + \
               (tokens_out / 1000) * self.output_price_per_1k

        trace = {
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "llm_calls": 1,  # 单次直出：一次调用
            "tokens_estimate": tokens_in + tokens_out,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": round(cost, 6),
        }
        return records, trace
