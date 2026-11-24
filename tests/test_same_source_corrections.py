"""同源更正的持久化与裁决方向：11.26 → 11.28 更正必须生效 11.28。"""

from __future__ import annotations

from datetime import datetime

from ddl_agent.conflict import ConflictResolver
from ddl_agent.models import DeadlineCandidate, RawMessage
from ddl_agent.monitor.cursor import (
    CandidateStore,
    MessageStore,
    backfill_correction_flags,
)
from ddl_agent.settings import TZ


def _candidate(
    candidate_id: str,
    deadline_iso: str,
    *,
    correction: bool = False,
    published_at: datetime | None = None,
    source_order: int | None = None,
) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw="操作系统作业一",
        course="COMP7503",
        deadline_raw=deadline_iso[5:10].replace("-", "."),
        deadline_iso=deadline_iso,
        source_type="student_chat",
        source_name="COMP7503 作业通知群",
        published_at=published_at,
        message_id=f"msg-{candidate_id}",
        source_order=source_order,
        is_correction=correction,
        correction_marker="更正" if correction else None,
    )


def test_correction_later_wins_with_published_times():
    original = _candidate(
        "orig", "2026-11-26T23:59:00+08:00",
        published_at=datetime(2026, 11, 24, 10, 0, tzinfo=TZ),
        source_order=1,
    )
    correction = _candidate(
        "fix", "2026-11-28T23:59:00+08:00", correction=True,
        published_at=datetime(2026, 11, 24, 11, 0, tzinfo=TZ),
        source_order=2,
    )

    kept, superseded, ambiguous = ConflictResolver()._apply_same_source_updates(
        [original, correction]
    )

    assert ambiguous is None
    assert [item.deadline_iso for item in kept] == ["2026-11-28T23:59:00+08:00"]
    assert superseded[0]["deadline"] == "2026-11-26T23:59:00+08:00"


def test_reverse_correction_also_follows_later_message():
    later_revert = _candidate(
        "fix", "2026-11-26T23:59:00+08:00", correction=True,
        published_at=datetime(2026, 11, 24, 11, 0, tzinfo=TZ),
        source_order=2,
    )
    earlier = _candidate(
        "orig", "2026-11-28T23:59:00+08:00",
        published_at=datetime(2026, 11, 24, 10, 0, tzinfo=TZ),
        source_order=1,
    )

    kept, superseded, ambiguous = ConflictResolver()._apply_same_source_updates(
        [earlier, later_revert]
    )

    assert ambiguous is None
    assert [item.deadline_iso for item in kept] == ["2026-11-26T23:59:00+08:00"]
    assert superseded[0]["deadline"] == "2026-11-28T23:59:00+08:00"


def test_mobile_unknown_times_still_prefers_unique_correction():
    original = _candidate("orig", "2026-11-26T23:59:00+08:00")
    correction = _candidate("fix", "2026-11-28T23:59:00+08:00", correction=True)

    kept, superseded, ambiguous = ConflictResolver()._apply_same_source_updates(
        [original, correction]
    )

    assert ambiguous is None
    assert [item.deadline_iso for item in kept] == ["2026-11-28T23:59:00+08:00"]
    assert superseded[0]["deadline"] == "2026-11-26T23:59:00+08:00"


def test_conflicting_corrections_without_time_need_review():
    first = _candidate("a", "2026-11-28T23:59:00+08:00", correction=True)
    second = _candidate("b", "2026-11-26T23:59:00+08:00", correction=True)

    _, _, ambiguous = ConflictResolver()._apply_same_source_updates([first, second])

    assert ambiguous is not None
    assert "多条不同截止值的「更正」" in ambiguous


def test_candidate_store_keeps_audit_fields(tmp_path):
    candidate = _candidate(
        "c1", "2026-11-28T23:59:00+08:00", correction=True,
        published_at=datetime(2026, 11, 24, 11, 0, tzinfo=TZ),
        source_order=7,
    )
    candidate.published_time = "11:00"
    candidate.observed_at = datetime(2026, 11, 24, 11, 5, tzinfo=TZ)
    candidate.pinned = True
    path = tmp_path / "candidates.jsonl"

    CandidateStore(path).add([candidate])
    restored = CandidateStore(path).all()[0]

    assert restored.is_correction is True
    assert restored.correction_marker == "更正"
    assert restored.source_order == 7
    assert restored.published_time == "11:00"
    assert restored.observed_at == candidate.observed_at
    assert restored.pinned is True


def test_backfill_correction_flags_from_message_body(tmp_path):
    message = RawMessage(
        message_id="m1",
        source_name="COMP7503 作业通知群",
        source_type="student_chat",
        sender="",
        published_at=None,
        subject="QQ / COMP7503 作业通知群",
        body="【更正】操作系统作业一已改到 11.28 23:59，其他要求不变",
    )
    messages = MessageStore(tmp_path / "messages.jsonl")
    messages.add([message])
    candidate = _candidate("c1", "2026-11-28T23:59:00+08:00")
    candidate.message_id = "m1"

    changed = backfill_correction_flags([candidate], messages)

    assert changed == 1
    assert candidate.is_correction is True
    assert candidate.correction_marker == "更正"
