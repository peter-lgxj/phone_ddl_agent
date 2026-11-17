"""DDL Agent：时间归一化、规则抽取、去重。"""

from datetime import datetime

from ddl_agent.dedupe import Deduplicator, normalize_task_name, task_similarity
from ddl_agent.extract.normalizer import TimeNormalizer
from ddl_agent.extract.rules import RuleExtractor, normalize_course
from ddl_agent.models import DeadlineCandidate, RawMessage
from ddl_agent.settings import TZ

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)  # 周一


def _message(body: str, subject: str = "", source_type: str = "official_email") -> RawMessage:
    return RawMessage(
        message_id="m1",
        source_name="outlook",
        source_type=source_type,
        sender="ta@cs.hku.hk",
        published_at=NOW,
        subject=subject,
        body=body,
    )


def _candidate(task: str, course: str | None, iso: str | None,
               source_type: str = "official_email", published_at: datetime = NOW,
               candidate_id: str = "c1") -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw=task,
        course=course,
        deadline_raw=None,
        deadline_iso=iso,
        source_type=source_type,
        source_name=source_type,
        published_at=published_at,
        message_id="m1",
    )


# ---------------------------------------------------------------- 归一化
def test_normalize_absolute_iso():
    result = TimeNormalizer().normalize("due on 2026-12-05 23:59", now=NOW)
    assert result.iso == "2026-12-05T23:59:00+08:00"
    assert result.unresolved is False


def test_normalize_chinese_date_with_time():
    result = TimeNormalizer().normalize("截止时间是 12月5日 23:59", now=NOW)
    assert result.iso == "2026-12-05T23:59:00+08:00"


def test_normalize_next_friday():
    # 2026-11-16 是周一，下周五是 2026-11-27
    result = TimeNormalizer().normalize("due next Friday at 23:59", now=NOW)
    assert result.iso == "2026-11-27T23:59:00+08:00"


def test_normalize_chinese_relative_weekday():
    result = TimeNormalizer().normalize("下周三交", now=NOW)
    assert result.iso == "2026-11-25T23:59:00+08:00"


def test_normalize_days_after():
    result = TimeNormalizer().normalize("3 天后截止，晚上 8 点", now=NOW)
    assert result.iso == "2026-11-19T20:00:00+08:00"


def test_normalize_only_date_defaults_to_2359():
    result = TimeNormalizer().normalize("截止 2026-12-05", now=NOW)
    assert result.iso == "2026-12-05T23:59:00+08:00"
    assert result.default_time_assumed is True


def test_normalize_unresolved_is_flagged():
    result = TimeNormalizer().normalize("截止 13月40日", now=NOW)
    assert result.iso is None
    assert result.unresolved is True


def test_normalize_without_date_is_not_unresolved():
    result = TimeNormalizer().normalize("请按时提交", now=NOW)
    assert result.iso is None
    assert result.unresolved is False


# ---------------------------------------------------------------- 抽取
def test_extract_candidate_from_email():
    message = _message(
        "Dear students,\nAssignment 2 for COMP7608 is due on 2026-12-05 23:59.",
        subject="[COMP7608] Assignment 2",
    )
    candidates = RuleExtractor().extract([message], now=NOW)
    assert len(candidates) == 1
    assert candidates[0].task_name_raw.lower().startswith("assignment 2")
    assert candidates[0].course == "COMP7608"
    assert candidates[0].deadline_iso == "2026-12-05T23:59:00+08:00"


def test_extract_missing_deadline():
    message = _message("COMP7608 Assignment 9 的截止时间还没定，下周通知。")
    candidates = RuleExtractor().extract([message], now=NOW)
    assert len(candidates) == 1
    assert candidates[0].deadline_iso is None
    assert "deadline" in candidates[0].missing_fields


def test_extract_skips_non_deadline_text():
    message = _message("Welcome to COMP7608. The syllabus is available on Moodle.")
    assert RuleExtractor().extract([message], now=NOW) == []


def test_normalize_course_code():
    assert normalize_course("comp 7608") == "COMP7608"
    assert normalize_course(None) is None


# ---------------------------------------------------------------- 去重
def test_task_name_synonyms_are_normalized():
    assert normalize_task_name("HW1") == normalize_task_name("Assignment 1")
    assert normalize_task_name("Homework 2") == normalize_task_name("Assignment 2")
    assert normalize_task_name("作业三") == normalize_task_name("Assignment 3")


def test_dedupe_merges_cross_source_duplicate():
    candidates = [
        _candidate("Assignment 1", "COMP7608", "2026-12-01T23:59:00+08:00",
                  candidate_id="a"),
        _candidate("HW1", "COMP7608", "2026-12-01T23:59:00+08:00",
                  source_type="instructor_chat", candidate_id="b"),
    ]
    clusters = Deduplicator().cluster(candidates)
    assert len(clusters) == 1
    assert len(clusters[0].candidates) == 2


def test_dedupe_keeps_different_courses_apart():
    candidates = [
        _candidate("Assignment 1", "COMP7608", "2026-12-01T23:59:00+08:00", candidate_id="a"),
        _candidate("Assignment 1", "COMP7404", "2026-12-01T23:59:00+08:00", candidate_id="b"),
    ]
    assert len(Deduplicator().cluster(candidates)) == 2


def test_similarity_orders_related_above_unrelated():
    related = task_similarity(normalize_task_name("assignment 1"), normalize_task_name("hw1"))
    unrelated = task_similarity(normalize_task_name("assignment 1"),
                                normalize_task_name("final exam"))
    assert related == 1.0
    assert unrelated < 0.4
