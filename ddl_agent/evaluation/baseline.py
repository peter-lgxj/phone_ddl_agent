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
    """与 agent 使用完全相同的原始消息，只做一次“抽取 + 直接输出”（规则兜底版）。"""

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
                sources=[f"{candidate.source_name} ({candidate.source_type})"],
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


_SYSTEM = (
    "你是单次直出的截止时间汇总器。只输出 JSON 数组，不要解释；"
    "不要做去重或冲突核查，看到什么就输出什么。"
)

_PROMPT = """当前时间：{now}

下面是全部原始消息（不同来源、可能互相矛盾）：

{messages}

请一次性输出所有你认为属于本课程的截止时间记录，JSON 数组：
[
  {{"task_name": "...", "course": "...", "deadline_iso": "ISO-8601 或 null",
    "confidence": 0.0, "source_index": 1}}
]
source_index 指向消息编号。无法解析时间的条目 deadline_iso 填 null。"""


class LLMSinglePassBaseline:
    """真正的单次直出：一条 prompt 里塞入全部原始消息，只调用一次模型。"""

    def __init__(self, client):
        self.client = client

    def run(self, messages: list[RawMessage],
            now: datetime | None = None) -> tuple[list[ResolvedRecord], dict]:
        started = time.perf_counter()
        now = now or datetime.now(TZ)
        blocks = [
            f"[{index}] {message.source_type} | {message.source_name} | "
            f"{message.published_at.isoformat() if message.published_at else 'unknown'}\n"
            f"{message.subject}\n{message.body}"
            for index, message in enumerate(messages, start=1)
        ]
        prompt = _PROMPT.format(now=now.isoformat(), messages="\n\n".join(blocks))

        before = self.client.tracker.snapshot()
        data = None
        try:
            data = self.client.complete_json(prompt, system=_SYSTEM, kind="baseline")
        except Exception:  # noqa: BLE001 - baseline 失败时结果为空，评测会如实反映
            data = None
        usage = self.client.tracker.since(before)

        records: list[ResolvedRecord] = []
        if isinstance(data, list):
            for position, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    continue
                task_name = str(item.get("task_name") or "").strip()
                if not task_name:
                    continue
                deadline = _validate_iso(item.get("deadline_iso"))
                label = _source_label(messages, item.get("source_index"))
                records.append(
                    ResolvedRecord(
                        task_name=task_name,
                        course=str(item.get("course") or "").strip() or None,
                        deadline_iso=deadline,
                        status="confirmed" if deadline else "missing_deadline",
                        source=label,
                        sources=[label],
                        evidence_ids=[f"baseline:{position}"],
                        confidence=0.6 if deadline else 0.3,
                        resolution_note="single-pass baseline：一次直出，未去重、未核查冲突",
                    )
                )

        trace = {
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "llm_calls": usage["llm_calls"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "tokens_estimate": usage["tokens_estimate"],
            "llm_seconds": usage["llm_seconds"],
            "cost_usd": usage["cost_usd"],
        }
        return records, trace


def _source_label(messages: list[RawMessage], source_index) -> str:
    try:
        index = int(source_index) - 1
        message = messages[index]
    except (TypeError, ValueError, IndexError):
        return "raw messages (single pass)"
    return f"{message.source_name} ({message.source_type})"


def _validate_iso(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.isoformat(timespec="seconds")
