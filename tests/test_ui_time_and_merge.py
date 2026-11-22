"""读屏 UI 时间格式解析与重复读取去重。"""

from __future__ import annotations

from datetime import datetime

from ddl_agent.conflict import ConflictResolver
from ddl_agent.connectors.mobile_reading import parse_time_details, split_observation
from ddl_agent.dedupe import duplicate_name_key, merge_duplicate_candidates
from ddl_agent.models import DeadlineCandidate, RawMessage, TaskCluster
from ddl_agent.pipeline import DdlAgent
from ddl_agent.settings import TZ

# 2026-11-22 是周日；2026-11-23 是周一
READ_SUNDAY = datetime(2026, 11, 22, 10, 0, tzinfo=TZ)
READ_MONDAY = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)


def _candidate(task: str, iso: str, *, candidate_id: str,
               source: str = "wechat:COMP7503作业通知群",
               source_type: str = "course_announcement",
               published_at: datetime | None = None,
               published_time: str = "",
               observed_at: datetime | None = None,
               source_order: int | None = 1) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw=task,
        course="COMP7503",
        deadline_raw="11月20日",
        deadline_iso=iso,
        source_type=source_type,
        source_name=source,
        published_at=published_at,
        message_id=f"msg-{candidate_id}",
        published_time=published_time,
        observed_at=observed_at,
        source_order=source_order,
    )


# ---------------------------------------------------------------- 时间格式
def test_ui_clock_only_keeps_time_without_date():
    """HH:MM 只保留时刻，不用读屏当天日期补成完整时间戳。"""
    details = parse_time_details("06:33", now=READ_SUNDAY)

    assert details.published_at is None
    assert details.published_time == "06:33"
    assert details.date_inferred is False
    assert "只有时刻" in details.reason


def test_ui_yesterday_with_clock_infers_date():
    details = parse_time_details("昨天 06:33", now=READ_SUNDAY)

    assert details.published_at == datetime(2026, 11, 21, 6, 33, tzinfo=TZ)
    assert details.published_time == "06:33"
    assert details.date_inferred is True
    assert "昨天" in details.date_inference_basis


def test_ui_weekday_with_clock_anchors_current_week():
    for token in ("周五 23:45", "星期五 23:45"):
        details = parse_time_details(token, now=READ_SUNDAY)

        assert details.published_at == datetime(2026, 11, 20, 23, 45, tzinfo=TZ)
        assert details.published_time == "23:45"
        assert details.date_inferred is True
        assert "周五" in details.date_inference_basis or "星期五" in details.date_inference_basis


def test_ui_weekday_cross_week_ambiguity_stays_unknown():
    """读屏日周一看到「周五」：无法判断上周还是本周，保持日期未知。"""
    details = parse_time_details("周五 23:45", now=READ_MONDAY)

    assert details.published_at is None
    assert details.published_time == "23:45"
    assert details.date_inferred is False
    assert "跨周" in details.reason


def test_ui_weekday_or_yesterday_without_clock_keeps_date_only():
    weekday = parse_time_details("周四", now=READ_SUNDAY)
    assert weekday.published_at == datetime(2026, 11, 19, tzinfo=TZ)
    assert weekday.published_time == ""
    assert weekday.date_inferred is True
    assert "只有日期" in weekday.reason

    yesterday = parse_time_details("昨天", now=READ_SUNDAY)
    assert yesterday.published_at == datetime(2026, 11, 21, tzinfo=TZ)
    assert yesterday.date_inferred is True
    assert "只有日期" in yesterday.reason


def test_structured_observation_clock_only_is_not_published_at():
    """结构化观察里的「时间：06:33」只留时刻并标注待复核。"""
    records = split_observation(
        "发送者：助教 时间：06:33 原文：11月20日 23:59 前提交 report",
        now=READ_SUNDAY,
    )

    assert len(records) == 1
    record = records[0]
    assert record.published_at is None
    assert record.published_time == "06:33"
    assert record.needs_review is True
    assert "只有时刻" in record.review_reason


# ---------------------------------------------------------------- 冲突标注
def test_conflict_sides_expose_clock_only_note():
    """只有时刻的一侧排到了最新，但 published_at 仍为空，并写明日期未知。"""
    older = _candidate(
        "小组作业 report", "2026-12-05T23:59:00+08:00",
        candidate_id="c-old", source="outlook:official",
        source_type="official_email",
        published_at=datetime(2026, 11, 19, 10, 0, tzinfo=TZ),
    )
    newer = _candidate(
        "小组作业 report", "2026-12-10T23:59:00+08:00",
        candidate_id="c-new", source="wechat:COMP7503作业通知群",
        published_time="06:33", observed_at=READ_SUNDAY,
    )

    outcome = ConflictResolver().resolve(
        [TaskCluster(cluster_id="c1", task_name="小组作业 report",
                     course="COMP7503", candidates=[older, newer])]
    )

    record = outcome.records[0]
    assert record.deadline_iso == "2026-12-10T23:59:00+08:00"
    assert "只有时刻" in record.resolution_note

    sides = {side["evidence_id"]: side for side in record.conflicts[0]["sides"]}
    assert sides["c-new"]["published_at"] is None
    assert sides["c-new"]["published_time"] == "06:33"
    assert sides["c-new"]["published_at_note"] == "只有时刻、日期未知"
    assert sides["c-new"]["date_inferred"] is False
    assert sides["c-old"]["published_at_note"] == ""


# ---------------------------------------------------------------- 重复读取
def test_merge_duplicate_candidates_marks_merged_duplicate_of():
    first = _candidate("小组作业 report", "2026-11-20T23:59:00+08:00",
                       candidate_id="dup-1", source_order=1)
    second = _candidate("小组作业提交（report）", "2026-11-20T23:59:00+08:00",
                        candidate_id="dup-2", source_order=2)

    assert duplicate_name_key(first.task_name_raw) == duplicate_name_key(second.task_name_raw)

    kept, merged = merge_duplicate_candidates([first, second])

    assert len(kept) == 1
    assert len(merged) == 1
    keeper = kept[0]
    dropped = second if keeper is first else first
    assert keeper.candidate_id == "dup-1"
    assert dropped.merged_duplicate_of == "dup-1"
    assert merged[0]["kept"] == "dup-1"
    assert merged[0]["merged"] == "dup-2"
    assert merged[0]["merged_duplicate_of"] == "dup-1"


class _StaticSource:
    name = "wechat:COMP7503作业通知群"
    source_type = "course_announcement"

    def __init__(self, messages: list[RawMessage]):
        self.messages = messages

    def fetch(self, plan):
        return list(self.messages)


class _StubExtractor:
    def __init__(self, candidates: list[DeadlineCandidate]):
        self.candidates = candidates

    def extract(self, messages, now=None):
        return list(self.candidates)


def test_pipeline_merges_duplicate_timeout_reads():
    """超时重读同一置顶消息：只产出一条 DDL，另一条记 merged_duplicate_of。"""
    messages = [
        RawMessage(
            message_id="m1", source_name=_StaticSource.name,
            source_type="course_announcement", sender="助教",
            published_at=datetime(2026, 11, 18, 9, 0, tzinfo=TZ),
            subject="COMP7503", body="小组作业 report 11月20日截止",
        )
    ]
    candidates = [
        _candidate("小组作业 report", "2026-11-20T23:59:00+08:00",
                   candidate_id="dup-1", source_order=1),
        _candidate("小组作业提交（report）", "2026-11-20T23:59:00+08:00",
                   candidate_id="dup-2", source_order=2),
    ]
    agent = DdlAgent(
        sources=[_StaticSource(messages)],
        extractor=_StubExtractor(candidates),
        now=datetime(2026, 11, 18, 9, 0, tzinfo=TZ),
        fixed_sources=True,
    )

    result = agent.run("COMP7503 有什么 DDL", write=False)

    assert len(result.records) == 1
    assert result.records[0].deadline_iso == "2026-11-20T23:59:00+08:00"
    assert len(result.trace["merged_duplicates"]) == 1
    assert result.trace["merged_duplicates"][0]["kept"] == "dup-1"
    assert result.trace["merged_duplicates"][0]["merged_duplicate_of"] == "dup-1"
