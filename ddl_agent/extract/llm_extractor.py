"""LLM 抽取器：与 ``RuleExtractor`` 同一接口，离线默认不启用。

真实模型路径未接入任何 API key，也未经真实数据验证；这里只提供可注入的适配层。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from ..models import DeadlineCandidate, RawMessage
from .normalizer import TimeNormalizer

_PROMPT = """你是信息抽取器。请从下面这封邮件/聊天中抽出所有截止时间相关条目。
只输出 JSON 数组，每个元素包含字段：task_name, course, deadline_text, source_sender。
没有截止时间的条目不要输出。不要编造。
当前时间：{now}
来源类型：{source_type}
发件人：{sender}
标题：{subject}
正文：
{body}
"""


class LLMDeadlineExtractor:
    """用一个 ``complete(prompt) -> str`` 的可调用对象完成抽取。"""

    def __init__(self, complete: Callable[[str], str], normalizer: TimeNormalizer | None = None):
        self.complete = complete
        self.normalizer = normalizer or TimeNormalizer()

    def extract(self, messages: list[RawMessage], now: datetime | None = None) -> list[DeadlineCandidate]:
        candidates: list[DeadlineCandidate] = []
        for message in messages:
            prompt = _PROMPT.format(
                now=(now or message.published_at).isoformat(),
                source_type=message.source_type,
                sender=message.sender,
                subject=message.subject,
                body=message.body,
            )
            raw = self.complete(prompt)
            for index, item in enumerate(_load_json_array(raw), start=1):
                normalized = self.normalizer.normalize(item.get("deadline_text"), now=now)
                candidates.append(
                    DeadlineCandidate(
                        candidate_id=f"{message.message_id}:llm{index}",
                        task_name_raw=str(item.get("task_name") or "").strip() or "unnamed task",
                        course=item.get("course"),
                        deadline_raw=item.get("deadline_text"),
                        deadline_iso=normalized.iso,
                        source_type=message.source_type,
                        source_name=message.source_name,
                        published_at=message.published_at,
                        message_id=message.message_id,
                        message_subject=message.subject,
                        missing_fields=[] if normalized.iso else ["deadline"],
                        unresolved_time=normalized.unresolved,
                    )
                )
        return candidates


def _load_json_array(raw: str) -> list[dict]:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
