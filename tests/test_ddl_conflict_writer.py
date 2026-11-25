"""DDL Agent：冲突解决与文档写入。"""

from datetime import datetime, timedelta
from pathlib import Path

import ddl_agent.settings as settings
from ddl_agent.conflict import ConflictResolver
from ddl_agent.models import DeadlineCandidate, ResolvedRecord, TaskCluster
from ddl_agent.settings import TZ
from ddl_agent.writer import CsvWriter, MarkdownWriter, XlsxWriter

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)


def _candidate(task: str, iso: str | None, source_type: str, published_at: datetime,
               candidate_id: str) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw=task,
        course="COMP7608",
        deadline_raw=None,
        deadline_iso=iso,
        source_type=source_type,
        source_name=source_type,
        published_at=published_at,
        message_id=candidate_id,
    )


def _cluster(candidates: list[DeadlineCandidate]) -> TaskCluster:
    return TaskCluster(cluster_id="c1", task_name="Assignment 1", course="COMP7608",
                       candidates=candidates)


def _record(deadline: str | None = "2026-12-05T23:59:00+08:00", status: str = "confirmed",
            task: str = "Assignment 1", source: str = "outlook (official_email)",
            confidence: float = 0.95) -> ResolvedRecord:
    return ResolvedRecord(
        task_name=task,
        course="COMP7608",
        deadline_iso=deadline,
        status=status,
        source=source,
        evidence_ids=["m1:1"],
        confidence=confidence,
        resolution_note="test",
    )


# ---------------------------------------------------------------- 冲突
def test_conflict_has_no_fixed_source_prior():
    """邮件不再固定压过群聊：同权时按发布时间裁决。"""
    cluster = _cluster([
        _candidate("Assignment 1", "2026-12-10T23:59:00+08:00", "student_chat",
                   NOW - timedelta(days=1), "s1"),
        _candidate("Assignment 1", "2026-12-08T23:59:00+08:00", "official_email",
                   NOW - timedelta(days=5), "e1"),
    ])
    record = ConflictResolver().resolve([cluster]).records[0]
    assert record.status == "confirmed"
    # 群消息更新 → 采用群里的时间，而不是因为来源是 official_email 就固定胜出
    assert record.deadline_iso == "2026-12-10T23:59:00+08:00"
    assert "发布时间更新" in record.resolution_note
    assert record.conflicts and len(record.conflicts[0]["sides"]) == 2


def test_source_credibility_constant_removed_and_docs_match():
    """四级可信度常量已不参与裁决：代码与文档都不能再声称固定来源等级。"""
    assert not hasattr(settings, "SOURCE_CREDIBILITY")
    assert not hasattr(settings, "CONFIDENCE_SOURCE_PRIORITY")

    doc = (Path(__file__).resolve().parents[1] / "docs" / "proposal-compliance.md")
    text = doc.read_text(encoding="utf-8")
    assert "SOURCE_CREDIBILITY" in text          # only in the "not implemented as fixed four levels" note
    assert "official_email=4" not in text
    assert "highest credibility group agrees, use it" not in text
    assert "not implemented as fixed four levels" in text
    assert "source-level prior" in text


def test_conflict_same_time_same_weight_is_uncertain():
    cluster = _cluster([
        _candidate("Assignment 1", "2026-12-10T23:59:00+08:00", "student_chat", NOW, "s1"),
        _candidate("Assignment 1", "2026-12-08T23:59:00+08:00", "official_email", NOW, "e1"),
    ])
    record = ConflictResolver().resolve([cluster]).records[0]
    assert record.status == "uncertain"
    assert record.deadline_iso is None


def test_conflict_newer_announcement_wins():
    cluster = _cluster([
        _candidate("Assignment 1", "2026-12-01T23:59:00+08:00", "official_email",
                   NOW - timedelta(days=40), "e1"),
        _candidate("Assignment 1", "2026-12-08T23:59:00+08:00", "official_email",
                   NOW - timedelta(days=5), "e2"),
    ])
    record = ConflictResolver().resolve([cluster]).records[0]
    assert record.deadline_iso == "2026-12-08T23:59:00+08:00"
    assert "发布时间更新" in record.resolution_note


def test_conflict_uncertain_after_two_rounds():
    published = NOW - timedelta(hours=3)
    cluster = _cluster([
        _candidate("Assignment 1", "2026-12-09T23:59:00+08:00", "student_chat", published, "s1"),
        _candidate("Assignment 1", "2026-12-11T23:59:00+08:00", "student_chat", published, "s2"),
    ])
    calls: list[int] = []

    def retrieval(_plan, _cluster, round_index):
        calls.append(round_index)
        return []  # 没有新证据

    outcome = ConflictResolver().resolve([cluster], plan=None, retrieval=retrieval)
    assert outcome.supplementary_rounds == 2
    assert calls == [1, 2]
    record = outcome.records[0]
    assert record.status == "uncertain"
    assert record.deadline_iso is None
    assert len(record.alternatives) == 2


def test_conflict_resolves_with_supplementary_evidence():
    cluster = _cluster([
        _candidate("Assignment 1", "2026-12-09T23:59:00+08:00", "student_chat", NOW, "s1"),
        _candidate("Assignment 1", "2026-12-11T23:59:00+08:00", "student_chat", NOW, "s2"),
    ])

    def retrieval(_plan, _cluster, _round_index):
        return [_candidate("Assignment 1", "2026-12-11T23:59:00+08:00", "official_email",
                           NOW, "e1")]

    outcome = ConflictResolver().resolve([cluster], plan=None, retrieval=retrieval)
    record = outcome.records[0]
    assert outcome.supplementary_rounds == 1
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-12-11T23:59:00+08:00"


def test_missing_deadline_record():
    cluster = _cluster([_candidate("Assignment 1", None, "student_chat", NOW, "s1")])
    record = ConflictResolver().resolve([cluster]).records[0]
    assert record.status == "missing_deadline"
    assert record.deadline_iso is None


# ---------------------------------------------------------------- 写入
def test_xlsx_insert_then_update(tmp_path):
    writer = XlsxWriter(tmp_path / "deadlines.xlsx")
    first = writer.upsert([_record()], timestamp=NOW)
    assert first.ok and first.inserted == 1

    second = writer.upsert([_record(deadline="2026-12-08T23:59:00+08:00")], timestamp=NOW)
    assert second.ok and second.updated == 1

    rows = writer.read()
    assert len(rows) == 1
    assert rows[0].deadline_iso == "2026-12-08T23:59:00+08:00"
    actions = [entry["Action"] for entry in writer.audit.entries()]
    assert actions == ["insert", "update"]


def test_writer_retries_once_then_rolls_back(tmp_path, monkeypatch):
    writer = CsvWriter(tmp_path / "deadlines.csv")
    assert writer.upsert([_record()], timestamp=NOW).ok
    original = (tmp_path / "deadlines.csv").read_text(encoding="utf-8-sig")

    attempts = {"count": 0}

    def boom(*_args, **_kwargs):
        attempts["count"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_write", boom)
    result = writer.upsert([_record(deadline="2026-12-08T23:59:00+08:00")], timestamp=NOW)

    assert result.ok is False
    assert result.rolled_back is True
    assert attempts["count"] == 2  # 首次 + 重试一次
    assert (tmp_path / "deadlines.csv").read_text(encoding="utf-8-sig") == original


def test_uncertain_does_not_overwrite_confirmed(tmp_path):
    writer = CsvWriter(tmp_path / "deadlines.csv")
    writer.upsert([_record()], timestamp=NOW)
    result = writer.upsert([_record(deadline=None, status="uncertain", confidence=0.4)],
                           timestamp=NOW)
    assert result.skipped == 1
    assert writer.read()[0].status == "confirmed"


def test_csv_writer_roundtrip(tmp_path):
    writer = CsvWriter(tmp_path / "deadlines.csv")
    writer.upsert([_record(), _record(task="Assignment 2", deadline=None,
                                      status="missing_deadline")], timestamp=NOW)
    rows = writer.read()
    assert len(rows) == 2
    assert {row.task_name for row in rows} == {"Assignment 1", "Assignment 2"}


def test_markdown_writer_roundtrip(tmp_path):
    writer = MarkdownWriter(tmp_path / "deadlines.md")
    writer.upsert([_record(task="Quiz | 1")], timestamp=NOW)
    rows = writer.read()
    assert len(rows) == 1
    assert rows[0].task_name == "Quiz | 1"
    assert "Audit" in (tmp_path / "deadlines.md").read_text(encoding="utf-8")


def test_audit_log_is_created_for_every_write(tmp_path):
    writer = XlsxWriter(tmp_path / "deadlines.xlsx")
    writer.upsert([_record()], timestamp=NOW)
    assert (tmp_path / "deadlines.xlsx.audit.csv").exists()
    assert len(writer.audit.entries()) == 1
