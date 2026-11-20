"""LLM 冲突裁决：只在候选时间互相矛盾时调用。

模型必须在已有候选时间中选一个；置信度不足或拒答一律返回 uncertain，由上层标人工复核。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import DeadlineCandidate, TaskCluster
from ..settings import LLM_MIN_CONFIDENCE
from .client import LLMClient, LLMUnavailable

_SYSTEM = (
    "你是截止时间冲突裁决器。只能从候选时间里选一个，或回答 uncertain。"
    "不得编造新时间；只输出 JSON。"
)

_PROMPT = """任务：{task}（课程：{course}）
下面是从不同来源取到的互相矛盾的截止时间：

{candidates}

可信度同权：不要因为来源是邮件或群聊就固定偏向某一侧。
优先看发布时间更新的公告；含「更正/延期/改为/以最新为准」的表述可覆盖旧值；
无法判断先后时回答 uncertain。

请输出 JSON：
{{"decision": "选中的 ISO-8601 时间，或 uncertain",
  "confidence": 0.0,
  "reason": "一句话理由"}}
只有在证据足够时才选时间；证据互相矛盾且无法判定时回答 uncertain，并给低置信度。"""


@dataclass
class Arbitration:
    deadline_iso: str | None
    confidence: float
    reason: str


class LLMConflictArbiter:
    def __init__(self, client: LLMClient, min_confidence: float = LLM_MIN_CONFIDENCE):
        self.client = client
        self.min_confidence = min_confidence

    def arbitrate(self, cluster: TaskCluster, candidates: list[DeadlineCandidate],
                  round_index: int = 0) -> Arbitration | None:
        dated = [candidate for candidate in candidates if candidate.deadline_iso]
        if len({candidate.deadline_iso for candidate in dated}) < 2:
            return None

        lines = []
        for position, candidate in enumerate(dated, start=1):
            lines.append(
                f"{position}. {candidate.deadline_iso} | 来源={candidate.source_type}"
                f" | 发布="
                f"{candidate.published_at.isoformat() if candidate.published_at else 'unknown'}"
                f" | 更正/延期={candidate.correction_marker or '-'}"
                f" | 原文={candidate.deadline_raw or '-'}"
                f" | 抽取置信度={candidate.extractor_confidence if candidate.extractor_confidence is not None else '-'}"
            )

        prompt = _PROMPT.format(
            task=cluster.task_name,
            course=cluster.course or "-",
            candidates="\n".join(lines),
        )
        if round_index:
            prompt += f"\n\n这是第 {round_index} 轮追加检索后的证据。"

        try:
            data = self.client.complete_json(prompt, system=_SYSTEM, kind="arbitrate")
        except LLMUnavailable:
            return None
        if not isinstance(data, dict):
            return None

        decision = str(data.get("decision") or "").strip()
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason") or "")

        valid = {candidate.deadline_iso for candidate in dated}
        if decision in valid and confidence >= self.min_confidence:
            return Arbitration(decision, confidence, reason)
        # 模型拒答 / 低置信度 / 编造了候选外的时间：明确交人工，不猜
        return Arbitration(None, min(confidence, self.min_confidence - 0.01),
                           reason or f"模型未给出可靠裁决（decision={decision or 'empty'}）")
