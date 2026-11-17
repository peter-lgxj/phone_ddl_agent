"""LLM 抽取器：任务名、课程、截止时间（ISO-8601）与置信度。

失败或返回非法 JSON 时自动降级到规则抽取，保证离线/断网仍可跑。
"""

from __future__ import annotations

from datetime import datetime

from ..models import DeadlineCandidate, RawMessage
from ..settings import TZ
from ..llm.client import LLMClient, LLMUnavailable
from .normalizer import TimeNormalizer
from .rules import RuleExtractor

_SYSTEM = (
    "你是截止时间抽取器。只输出 JSON 数组，不要解释。"
    "引用链、转发、签名档、闲聊、别人的任务、考试/上课时间都不是当前任务的 DDL，必须忽略。"
)

_PROMPT = """当前时间：{now}

从下面这条消息中抽出属于本课程/项目的截止时间条目。只输出 JSON 数组：
[
  {{
    "task_name": "任务名（保留原文写法，如 HW1 / 作业一）",
    "course": "课程代码，没有就填 null",
    "deadline_text": "原文中的时间短语",
    "deadline_iso": "换算后的 ISO-8601（带 +08:00）；无法可靠解析填 null",
    "confidence": 0.0,
    "needs_review": false,
    "reason": "简短理由"
  }}
规则：
- 相对时间按上面的当前时间换算；有歧义（如“考完试那周”“月底前”）不要猜，填 null 并 needs_review=true。
- 如果消息明确在讨论某个任务的截止时间，但日期未定、互相矛盾或无法解析，仍然输出该任务，
  deadline_iso=null、needs_review=true；不要因为日期缺失就省略。
- 已过期的旧 DDL、他人课程任务、纯考试/上课时间不要输出。
- 没有截止时间语义就输出 []。

来源：{source_type} / {source_name}
发件人：{sender}
发布时间：{published}
标题：{subject}
正文：
{body}"""


class LLMDeadlineExtractor:
    def __init__(self, client: LLMClient, normalizer: TimeNormalizer | None = None,
                 fallback: RuleExtractor | None = None):
        self.client = client
        self.normalizer = normalizer or TimeNormalizer()
        self.fallback = fallback or RuleExtractor(self.normalizer)

    def extract(self, messages: list[RawMessage], now: datetime | None = None) -> list[DeadlineCandidate]:
        now = now or datetime.now(TZ)
        candidates: list[DeadlineCandidate] = []
        for message in messages:
            candidates.extend(self.extract_message(message, now))
        return candidates

    def extract_message(self, message: RawMessage, now: datetime | None = None) -> list[DeadlineCandidate]:
        now = now or datetime.now(TZ)
        prompt = _PROMPT.format(
            now=now.isoformat(),
            source_type=message.source_type,
            source_name=message.source_name,
            sender=message.sender,
            published=message.published_at.isoformat(),
            subject=message.subject,
            body=message.body,
        )
        try:
            data = self.client.complete_json(prompt, system=_SYSTEM, kind="extract")
        except LLMUnavailable:
            return self.fallback.extract_message(message, now)

        if not isinstance(data, list):
            # 模型输出非法时不能把消息当成“没有 DDL”，退回规则而不是静默丢弃
            return self.fallback.extract_message(message, now)

        results: list[DeadlineCandidate] = []
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                continue
            candidate = self._to_candidate(item, message, index, now)
            if candidate is not None:
                results.append(candidate)
        return results

    # ------------------------------------------------------------------ #
    def _to_candidate(self, item: dict, message: RawMessage, index: int,
                      now: datetime) -> DeadlineCandidate | None:
        task_name = str(item.get("task_name") or "").strip()
        if not task_name:
            return None

        deadline_text = str(item.get("deadline_text") or "").strip() or None
        iso = _validate_iso(item.get("deadline_iso"))
        if iso is None and deadline_text:
            iso = self.normalizer.normalize(deadline_text, now=now).iso

        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(max(confidence, 0.0), 1.0)
        needs_review = bool(item.get("needs_review")) or confidence < 0.5

        missing: list[str] = []
        course = str(item.get("course") or "").strip() or None
        if not course:
            missing.append("course")
        if iso is None:
            missing.append("deadline")

        return DeadlineCandidate(
            candidate_id=f"{message.message_id}:llm{index}",
            task_name_raw=task_name,
            course=course,
            deadline_raw=deadline_text,
            deadline_iso=iso,
            source_type=message.source_type,
            source_name=message.source_name,
            published_at=message.published_at,
            message_id=message.message_id,
            message_subject=message.subject,
            missing_fields=missing,
            unresolved_time=deadline_text is not None and iso is None,
            extractor_confidence=confidence,
            needs_review=needs_review,
        )


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
