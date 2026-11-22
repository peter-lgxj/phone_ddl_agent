"""基于规则的 DDL 抽取。

离线默认实现，确定性、不调用模型；LLM 抽取器见 ``llm_extractor.py``，两者实现同一接口。
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import DeadlineCandidate, RawMessage
from .courses import COURSE_RE, find_courses, normalize_course
from .evidence import validate_candidate_evidence
from .message_context import (
    course_from_message,
    detect_correction,
    effective_published_at,
)
from .normalizer import TimeNormalizer

_TASK_PATTERNS = [
    re.compile(
        r"(?P<name>(?:HW|Homework|Assign(?:ment)?|Project|Lab|Quiz|Report|Essay|Exam|Presentation)"
        r"\s*[-_ ]?(?:No\.?\s*)?\d+)",
        re.IGNORECASE,
    ),
    # 【机器学习作业三】这类通知标题：保留中文科目前缀，避免只抽到「作业三」
    re.compile(
        r"【[^】]{0,20}】\s*(?P<name>(?:[\u4e00-\u9fff]{2,8})?"
        r"(?:作业|实验报告|报告|项目|论文|考试|课程设计)\s*[一二三四五六七八九十\d]+)"
    ),
    re.compile(r"(?P<name>(?:大作业|作业|实验|报告|项目|论文|考试)\s*[一二三四五六七八九十\d]+)"),
    # 「小组课程报告，月底前交」这类没有序号的固定任务名
    re.compile(r"(?P<name>小组课程报告)"),
    re.compile(r"(?P<name>(?:Midterm|Final)\s+(?:Exam|Project|Report|Presentation))", re.IGNORECASE),
]

_DEADLINE_HINT_RE = re.compile(
    r"(deadline|due date|\bdue\b|\bddl\b|submit(?:ted)?\s+by|before|no later than|"
    r"截止|提交|不晚于|之前|到期)",
    re.IGNORECASE,
)

_COURSE_RE = COURSE_RE

_TRAILING_VERB_RE = re.compile(
    r"\s*(?:must\s+be\s+submitted(?:\s+by)?|should\s+be\s+submitted(?:\s+by)?|is\s+due|are\s+due|due|"
    r"需要提交|请提交|提交|截止|之前|前)\s*$",
    re.IGNORECASE,
)
_LEADING_NOISE_RE = re.compile(
    r"^(?:dear\s+(?:all|students)[,，:]?\s*|hi\s+(?:all|everyone)[,，:]\s*|"
    r"各位同学[,，:：]?\s*|同学们[,，:：]?\s*|提醒[:：]\s*|reminder[:：]\s*|"
    r"the\s+|a\s+|an\s+|your\s+)",
    re.IGNORECASE,
)
_TASK_NOUN_RE = re.compile(
    r"(hw|homework|assign(?:ment)?|project|proposal|lab|quiz|report|essay|exam|presentation|"
    r"作业|实验|报告|项目|论文|考试)",
    re.IGNORECASE,
)


def _segments(text: str):
    """把正文切成句子级片段，避免一句话里的日期影响另一句的任务。"""
    for line in (text or "").splitlines():
        for chunk in re.split(r"[。！？!?；;]+", line):
            chunk = chunk.strip()
            if len(chunk) >= 4:
                yield chunk


def _clean_task_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" ,，.:：;；-—")
    name = _LEADING_NOISE_RE.sub("", name).strip()
    name = _TRAILING_VERB_RE.sub("", name).strip(" ,，.:：;；-—")
    # 回退出来的名称常带课程代码，去掉后再与 ground truth 比对
    name = _COURSE_RE.sub("", name)
    name = re.sub(r"\b(?:for|of|in)\s*$", "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip(" ,，.:：;；-—")


def _fallback_task(segment: str, hint: re.Match | None) -> str:
    if hint is None:
        return ""
    prefix = segment[: hint.start()]
    if not _TASK_NOUN_RE.search(prefix):
        # 「Please submit your form」这类没有任务名词的句子不当成 DDL
        return ""
    return _clean_task_name(prefix)


class RuleExtractor:
    """从原始消息里抽出 ``DeadlineCandidate``。"""

    def __init__(self, normalizer: TimeNormalizer | None = None):
        self.normalizer = normalizer or TimeNormalizer()

    def extract(self, messages: list[RawMessage], now: datetime | None = None) -> list[DeadlineCandidate]:
        candidates: list[DeadlineCandidate] = []
        order_by_source: dict[str, int] = {}
        for message in messages:
            order_by_source[message.source_name] = order_by_source.get(message.source_name, 0) + 1
            candidates.extend(
                self.extract_message(
                    message, now, source_order=order_by_source[message.source_name]
                )
            )
        return candidates

    def extract_message(self, message: RawMessage, now: datetime | None = None,
                        source_order: int | None = None) -> list[DeadlineCandidate]:
        results: list[DeadlineCandidate] = []
        seen: set[tuple[str, str | None]] = set()
        published_at = effective_published_at(message)
        correction_marker = detect_correction(message.body)
        source_course, source_course_origin = course_from_message(message)

        for index, segment in enumerate(_segments(message.body), start=1):
            hint = _DEADLINE_HINT_RE.search(segment)
            task_match = self._first_task_match(segment)
            if hint is None and task_match is None:
                continue

            normalized = self.normalizer.normalize(segment, now=now)
            if normalized.iso is None and not normalized.unresolved and hint is None:
                # 只有任务名、没有任何截止语义的句子不算 DDL
                continue

            task_name = _clean_task_name(task_match.group("name")) if task_match else ""
            if not task_name:
                task_name = _fallback_task(segment, hint)
            if not task_name:
                continue

            course = (find_courses(segment) or [None])[0]
            course_origin = "body" if course else None
            if not course:
                course = (find_courses(message.subject) or [None])[0]
                course_origin = "subject" if course else None
            if not course:
                # 不再把「正文里没写课程」当成错误：群名/主题里的代码是有效上下文
                course, course_origin = source_course, source_course_origin

            missing: list[str] = []
            if not course:
                missing.append("course")
            if normalized.iso is None:
                missing.append("deadline")
            if normalized.unresolved:
                missing.append("deadline")

            key = (task_name.lower(), normalized.iso)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                validate_candidate_evidence(
                    DeadlineCandidate(
                        candidate_id=f"{message.message_id}:{index}",
                        task_name_raw=task_name,
                        course=normalize_course(course),
                        deadline_raw=normalized.matched_text,
                        deadline_iso=normalized.iso,
                        source_type=message.source_type,
                        source_name=message.source_name,
                        published_at=published_at,
                        message_id=message.message_id,
                        message_subject=message.subject,
                        missing_fields=sorted(set(missing)),
                        unresolved_time=normalized.unresolved,
                        needs_review=message.needs_review,
                        review_reason=message.review_reason,
                        course_from=course_origin,
                        source_order=source_order,
                        is_correction=correction_marker is not None,
                        correction_marker=correction_marker,
                        inferred=normalized.inferred,
                        inference_basis=normalized.inference_basis,
                        pinned=bool(getattr(message, "pinned", False)),
                        published_time=getattr(message, "published_time", ""),
                        published_at_inferred=bool(
                            getattr(message, "published_at_inferred", False)
                        ),
                        published_at_basis=getattr(message, "published_at_basis", ""),
                        observed_at=getattr(message, "observed_at", None),
                    ),
                    message.body,
                    course_context=f"{message.subject}\n{message.source_name}",
                )
            )
        return results

    @staticmethod
    def _first_task_match(segment: str) -> re.Match | None:
        for pattern in _TASK_PATTERNS:
            match = pattern.search(segment)
            if match:
                return match
        return None
