"""抽取结果的硬性证据校验测试。"""

from __future__ import annotations

from datetime import datetime

from ddl_agent.extract.evidence import validate_candidate_evidence
from ddl_agent.extract.llm_extractor import LLMDeadlineExtractor
from ddl_agent.models import DeadlineCandidate, RawMessage
from ddl_agent.settings import TZ


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, *_args, **_kwargs):
        return self.payload


def _candidate(course: str | None, deadline_iso: str | None,
               deadline_raw: str | None = None) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id="c1",
        task_name_raw="机器学习作业三",
        course=course,
        deadline_iso=deadline_iso,
        deadline_raw=deadline_raw,
        source_type="student_chat",
        source_name="文件传输助手",
        published_at=None,
        message_id="m1",
    )


def _message(body: str) -> RawMessage:
    return RawMessage(
        message_id="m1", source_name="文件传输助手", source_type="student_chat",
        sender="我", published_at=datetime(2026, 11, 18, 13, 39, tzinfo=TZ),
        subject="", body=body,
    )


def test_course_code_in_the_same_message_passes():
    message = _message("COMP7608 机器学习作业三，11.29 晚上 23.59 交到 Moodle")
    payload = [{
        "task_name": "机器学习作业三",
        "course": "COMP7608",
        "deadline_text": "11月29日 23:59",
        "deadline_iso": "2026-11-29T23:59:00+08:00",
        "confidence": 0.9,
    }]

    candidates = LLMDeadlineExtractor(_FakeClient(payload)).extract_message(message)

    assert candidates[0].course == "COMP7608"
    assert candidates[0].deadline_iso == "2026-11-29T23:59:00+08:00"
    assert candidates[0].needs_review is False


def test_course_code_from_another_message_is_rejected():
    target = _message("机器学习作业三，11.29 晚上 23.59 交到 Moodle")
    other = _message("COMP7503 多媒体 project 的截止日期是 10 月 21")
    payload = [{
        "task_name": "机器学习作业三",
        "course": "COMP7503",
        "deadline_text": "11月29日 23:59",
        "deadline_iso": "2026-11-29T23:59:00+08:00",
        "confidence": 0.9,
    }]
    extractor = LLMDeadlineExtractor(_FakeClient(payload))

    target_candidate = extractor.extract_message(target)[0]
    other_candidate = extractor.extract_message(other)[0]

    assert target_candidate.course is None
    assert target_candidate.needs_review is True
    assert "课程 COMP7503" in target_candidate.review_reason
    # 课程代码在它自己的消息里出现时仍然可以通过
    assert other_candidate.course == "COMP7503"


def test_equivalent_date_expression_passes():
    message = _message("机器学习作业三，11.29 晚上 23.59 交到 Moodle")
    candidate = _candidate(None, "2026-11-29T23:59:00+08:00", "11月29日 23:59")

    validated = validate_candidate_evidence(candidate, message.body)

    assert validated.deadline_iso == "2026-11-29T23:59:00+08:00"
    assert validated.needs_review is False


def test_deadline_not_in_message_is_blanked():
    message = _message("作业截止时间待定")
    candidate = _candidate(None, "2026-11-29T23:59:00+08:00", "11月29日 23:59")

    validated = validate_candidate_evidence(candidate, message.body)

    assert validated.deadline_iso is None
    assert validated.deadline_raw is None
    assert validated.needs_review is True
    assert "截止时间原文" in validated.review_reason
