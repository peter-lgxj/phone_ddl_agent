"""deadlines.ics 产物：严格解析、CRLF、TZID、VALARM 与冲突生效值。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from icalendar import Calendar

from ddl_agent.models import ResolvedRecord
from ddl_agent.output import write_data_json, write_ics
from ddl_agent.settings import TZ

TZ8 = timezone(timedelta(hours=8))


def _records() -> list[ResolvedRecord]:
    return [
        ResolvedRecord(
            task_name="作业三", course="COMP7503",
            deadline_iso="2026-12-06T23:59:00+08:00", status="confirmed",
            source="微信-COMP7503课程群", sources=["微信-COMP7503课程群"],
            superseded_by=[{"deadline": "2026-11-29T23:59:00+08:00"}],
            resolution_note="同源更正/延期：采用最新一条生效值",
        ),
        ResolvedRecord(
            task_name="Final Report", course=None,
            deadline_iso="2026-12-05T23:59:00+08:00", status="confirmed",
            source="QQ-LLM小组", sources=["QQ-LLM小组"],
        ),
        ResolvedRecord(
            task_name="小组作业", course=None,
            deadline_iso="2026-11-20T23:59:00+08:00", status="confirmed",
            source="QQ-LLM小组", sources=["QQ-LLM小组"],
        ),
    ]


def test_ics_has_single_crlf_and_parses_strictly(tmp_path: Path):
    path = write_ics(_records(), tmp_path / "deadlines.ics")

    raw = path.read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert not re.search(rb"\r(?!\n)", raw)

    calendar = Calendar.from_ical(raw)  # 严格解析：不应抛异常
    assert calendar.name == "VCALENDAR"


def test_ics_events_have_required_fields_and_valarm(tmp_path: Path):
    path = write_ics(_records(), tmp_path / "deadlines.ics")
    calendar = Calendar.from_ical(path.read_bytes())
    events = [item for item in calendar.walk() if item.name == "VEVENT"]

    assert len(events) == 3
    for event in events:
        assert str(event.get("UID"))
        assert str(event.get("DTSTAMP"))
        assert str(event.get("SUMMARY"))
        assert event.get("DTSTART") is not None
        alarms = [item for item in event.walk() if item.name == "VALARM"]
        assert len(alarms) == 1
        assert str(alarms[0].get("TRIGGER").to_ical().decode()) == "-P1D"
        assert str(alarms[0].get("ACTION")) == "DISPLAY"


def test_ics_uses_shanghai_tzid_and_keeps_local_time(tmp_path: Path):
    path = write_ics(_records(), tmp_path / "deadlines.ics")
    calendar = Calendar.from_ical(path.read_bytes())
    homework = next(
        event for event in calendar.walk("VEVENT")
        if "作业三" in str(event.get("SUMMARY"))
    )

    assert str(homework.get("DTSTART").params.get("TZID")) == "Asia/Shanghai"
    start = homework.decoded("DTSTART")
    assert start.astimezone(TZ8).strftime("%Y-%m-%d %H:%M") == "2026-12-06 23:59"
    assert start.utcoffset() == timedelta(hours=8)


def test_ics_conflict_only_emits_effective_value(tmp_path: Path):
    path = write_ics(_records(), tmp_path / "deadlines.ics")
    calendar = Calendar.from_ical(path.read_bytes())
    homework = [
        event for event in calendar.walk("VEVENT")
        if "作业三" in str(event.get("SUMMARY"))
    ]

    assert len(homework) == 1
    assert homework[0].decoded("DTSTART").astimezone(TZ8).day == 6
    assert "11-29" not in path.read_text(encoding="utf-8")


def test_data_json_count_matches_vevent_count(tmp_path: Path):
    records = _records()
    data_path = write_data_json(records, tmp_path / "data.json")
    ics_path = write_ics(records, tmp_path / "deadlines.ics")

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    events = [item for item in Calendar.from_ical(ics_path.read_bytes()).walk()
              if item.name == "VEVENT"]
    assert payload["counts"]["total"] == len(events) == 3
    assert payload["counts"]["confirmed"] == len(events)


def test_generated_at_is_utc_dtstamp(tmp_path: Path):
    path = write_ics(
        _records(), tmp_path / "deadlines.ics",
        generated_at=datetime(2026, 11, 22, 10, 0, tzinfo=TZ),
    )
    event = next(iter(Calendar.from_ical(path.read_bytes()).walk("VEVENT")))
    assert str(event.get("DTSTAMP").to_ical().decode()) == "20261122T020000Z"
