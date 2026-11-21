"""真机报告（QQ/微信/Outlook）复现的回归用例。

``tests/fixtures/real_device_20261121.json`` 是从
``runs/qq_real_device_20261121`` 与 ``runs/wx_real_device_20261121`` 裁剪出的
真实消息、当次 LLM 抽取结果与旧判分；runs 目录不进库，夹具保证 CI 可复现。
不联网、不碰手机。
每个 P0 修复都断言「真机旧判分」与「修复后判分」的差异。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ddl_agent.conflict import ConflictResolver
from ddl_agent.dedupe import Deduplicator, normalize_task_name
from ddl_agent.extract.courses import find_courses
from ddl_agent.extract.llm_extractor import LLMDeadlineExtractor
from ddl_agent.models import DeadlineCandidate, RawMessage, RetrievalPlan, SourceSpec
from ddl_agent.planner import (
    SourceConfigParser,
    select_sources,
    source_names_equivalent,
)
from ddl_agent.pipeline import DdlAgent
from ddl_agent.settings import TZ

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "real_device_20261121.json").read_text(encoding="utf-8")
)
QQ_NOW = datetime(2026, 11, 21, 0, 20, tzinfo=TZ)
WX_NOW = datetime(2026, 11, 21, 0, 50, tzinfo=TZ)

_REL_TIME_RE = re.compile(r"昨天\s+(\d{1,2}):(\d{2})")


class _CapturedSource:
    def __init__(self, name: str, source_type: str, messages: list[RawMessage]):
        self.name = name
        self.source_type = source_type
        self.messages = messages

    def fetch(self, _plan=None) -> list[RawMessage]:
        return list(self.messages)


class _RecordedClient:
    """把真机当次 LLM 输出喂回抽取器，并记录 prompt 以便断言课程上下文。"""

    def __init__(self, items: list[dict]):
        self.items = items
        self.prompts: list[str] = []

    def complete_json(self, prompt, system=None, kind="json"):
        self.prompts.append(prompt)
        return self.items


def _qq_messages(group_id: str) -> tuple[str, list[RawMessage]]:
    spec = FIXTURE["qq"][group_id]
    messages: list[RawMessage] = []
    for index, (sender, visible_time, body) in enumerate(spec["messages"], start=1):
        match = _REL_TIME_RE.search(visible_time)
        published = QQ_NOW
        if match:
            published = datetime(2026, 11, 20, int(match.group(1)), int(match.group(2)),
                                 tzinfo=TZ)
        messages.append(
            RawMessage(
                message_id=f"{group_id}-{index}",
                source_name=spec["group"],
                source_type="student_chat",
                sender=sender,
                published_at=published,
                subject=f"QQ / {spec['group']}",
                body=body,
            )
        )
    return spec["group"], messages


def _wx_messages(group_id: str) -> tuple[str, list[RawMessage]]:
    spec = FIXTURE["wx"]["groups"][group_id]
    published = datetime.fromisoformat(spec["published_at"])
    return spec["group"], [
        RawMessage(
            message_id=f"{group_id}-{index}",
            source_name=spec["group"],
            source_type="student_chat",
            sender=sender,
            published_at=published,
            subject=f"微信 / {spec['group']}",
            body=body,
        )
        for index, (sender, body) in enumerate(spec["messages"], start=1)
    ]


def _saved_candidates(group_id: str, wx: bool = False) -> list[DeadlineCandidate]:
    """用归档的当次抽取结果构造候选，并按修复后的语义补齐上下文字段。"""
    spec = FIXTURE["wx"]["groups"][group_id] if wx else FIXTURE["qq"][group_id]
    messages = {message.message_id: message for message in (
        _wx_messages(group_id)[1] if wx else _qq_messages(group_id)[1]
    )}
    source_course = (find_courses(spec["group"]) or [None])[0]
    candidates: list[DeadlineCandidate] = []
    for item in spec["candidates"]:
        message = messages[item["message_id"]]
        body = message.body
        marker = None
        for keyword in ("更正", "延期", "改为", "改到", "以最新为准"):
            if keyword in body:
                marker = keyword
                break
        course = item["course"] or source_course
        missing = []
        if not course:
            missing.append("course")
        if not item["deadline_iso"]:
            missing.append("deadline")
        candidates.append(
            DeadlineCandidate(
                candidate_id=item["candidate_id"],
                task_name_raw=item["task_name_raw"],
                course=course,
                deadline_raw=item["deadline_raw"],
                deadline_iso=item["deadline_iso"],
                source_type="student_chat",
                source_name=item["source_name"],
                published_at=message.published_at,
                message_id=item["message_id"],
                message_subject=item["message_subject"],
                missing_fields=missing,
                unresolved_time=item["unresolved_time"],
                extractor_confidence=item["extractor_confidence"],
                needs_review=(item["extractor_confidence"] or 0) < 0.5,
                course_from="model" if item["course"] else "source_name",
                source_order=int(item["message_id"].split("-")[-1]),
                is_correction=marker is not None,
                correction_marker=marker,
            )
        )
    return candidates


def _email_candidates() -> dict[str, DeadlineCandidate]:
    data = FIXTURE["wx"]["emails"]
    expected = {
        "O1": ("Final project proposal", "COMP7503", "2026-12-05T23:59:00+08:00"),
        "O2": ("Assignment 3", "COMP7503", "2026-11-30T23:59:00+08:00"),
        "O3": ("Final report", "COMP7415", "2026-12-12T23:59:00+08:00"),
    }
    result = {}
    for mid, (task, course, iso) in expected.items():
        spec = data[mid]
        published = datetime.fromisoformat(spec["published_at"])
        sent = re.search(r"Sent:\s*(20\d{2}-\d{2}-\d{2})", spec["body"])
        if sent:
            published = datetime.fromisoformat(sent.group(1)).replace(tzinfo=TZ)
        result[mid] = DeadlineCandidate(
            candidate_id=f"{mid}:llm1",
            task_name_raw=task,
            course=course,
            deadline_raw=iso[:10],
            deadline_iso=iso,
            source_type="official_email",
            source_name="Outlook/收件箱",
            published_at=published,
            message_id=mid,
            message_subject=spec["subject"],
            course_from="subject",
            extractor_confidence=0.95,
            source_order=1,
        )
    return result


def _resolve(candidates: list[DeadlineCandidate]):
    clusters = Deduplicator().cluster(candidates)
    return ConflictResolver().resolve(clusters).records


def _get(record, key):
    return record.get(key) if isinstance(record, dict) else getattr(record, key)


def _old_task_result(task_id: str, wx: bool = True) -> list[dict]:
    return FIXTURE["old_results"][task_id]


# ---------------------------------------------------------------- P0-1 课程代码
def test_source_course_is_injected_and_used_as_fallback():
    _, messages = _wx_messages("W1")
    message = next(item for item in messages if item.message_id == "W1-14")
    client = _RecordedClient([{
        "task_name": "机器学习作业三",
        "course": None,
        "deadline_text": "11.29 23:59",
        "deadline_iso": "2026-11-29T23:59:00+08:00",
        "confidence": 0.95,
        "needs_review": False,
    }])
    candidates = LLMDeadlineExtractor(client).extract([message], now=WX_NOW)

    assert "来源课程代码：COMP7503" in client.prompts[0]
    assert candidates[0].course == "COMP7503"
    assert candidates[0].course_from == "source_name"
    assert candidates[0].needs_review is False
    assert "course" not in candidates[0].missing_fields

    record = _resolve(candidates)[0]
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-11-29T23:59:00+08:00"


def test_missing_course_alone_does_not_demote_deadline():
    message = RawMessage(
        message_id="self-1",
        source_name="文件传输助手",
        source_type="student_chat",
        sender="我",
        published_at=WX_NOW,
        subject="微信 / 文件传输助手",
        body="机器学习作业三，11.29 晚上 23:59 交到 Moodle",
    )
    client = _RecordedClient([{
        "task_name": "机器学习作业三",
        "course": None,
        "deadline_text": "11.29 晚上 23:59",
        "deadline_iso": "2026-11-29T23:59:00+08:00",
        "confidence": 0.95,
        "needs_review": False,
    }])
    candidates = LLMDeadlineExtractor(client).extract([message], now=WX_NOW)
    assert candidates[0].course is None
    record = _resolve(candidates)[0]
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-11-29T23:59:00+08:00"


# ---------------------------------------------------------------- P0-2 同源更正
def test_wx_d11_extension_supersedes_old_value():
    candidates = _saved_candidates("W4", wx=True)
    records = _resolve(candidates)
    assert len(records) == 1
    record = records[0]
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-12-03T23:59:00+08:00"
    assert record.alternatives == []
    assert record.superseded_by
    assert record.superseded_by[0]["deadline"] == "2026-11-22T23:59:00+08:00"
    assert "延期" in record.superseded_by[0]["reason"]


def test_qq_g4_correction_wins_and_duplicate_old_value_is_merged():
    candidates = _saved_candidates("G4")
    records = _resolve(candidates)
    assert len(records) == 1
    record = records[0]
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-11-28T23:59:00+08:00"
    assert record.alternatives == []
    # 11-26 的两条重复消息只留一条旧值记录，且不单独成行
    assert len(record.superseded_by) == 1
    assert record.superseded_by[0]["deadline"] == "2026-11-26T23:59:00+08:00"
    assert len(record.superseded_by[0]["evidence_ids"]) == 2


def test_same_source_without_order_marks_review_instead_of_listing_both():
    candidates = _saved_candidates("W4", wx=True)
    for candidate in candidates:
        candidate.source_order = None
        candidate.published_at = WX_NOW
    records = _resolve(candidates)
    assert records[0].status == "uncertain"
    assert records[0].deadline_iso is None
    assert records[0].alternatives == []
    assert "无法判断" in records[0].resolution_note


# ---------------------------------------------------------------- P0-3 跨来源
def test_wx_d13_group_correction_beats_older_email():
    candidates = _saved_candidates("W1", wx=True) + [_email_candidates()["O2"]]
    record = next(
        item for item in _resolve(candidates)
        if normalize_task_name(item.task_name) == normalize_task_name("机器学习作业三")
    )
    assert record.status == "confirmed"
    assert record.deadline_iso == "2026-11-29T23:59:00+08:00"
    assert "2026-11-30T23:59:00+08:00" not in [record.deadline_iso]
    assert record.alternatives == []
    assert len(record.conflicts) == 1
    sides = {side["deadline"] for side in record.conflicts[0]["sides"]}
    assert sides == {"2026-11-29T23:59:00+08:00", "2026-11-30T23:59:00+08:00"}
    assert "发布时间" in record.conflicts[0]["basis"]


def test_wx_d14_email_only_target_is_not_replaced_by_group_ddl():
    candidates = _saved_candidates("W3", wx=True) + [_email_candidates()["O3"]]
    records = _resolve(candidates)
    target = next(
        item for item in records
        if item.course == "COMP7415"
        and item.deadline_iso == "2026-12-12T23:59:00+08:00"
    )
    assert target.status == "confirmed"
    assert "Outlook" in target.source
    # 群里的 Final Report 属于 COMP7503，不能顶替 COMP7415 的目标
    group_record = next(
        item for item in records
        if normalize_task_name(item.task_name) == normalize_task_name("Final Report")
        and item.course == "COMP7503"
    )
    assert group_record.deadline_iso == "2026-12-08T23:59:00+08:00"


# ---------------------------------------------------------------- 群名归一化
def test_similar_group_names_select_only_best_match():
    _, g1 = _qq_messages("G1")
    _, g2 = _qq_messages("G2")
    query = "在 QQ 中查找「2026 计算机班级群」（注意有相似群名）里实验报告二的截止时间。"
    kept, decisions = select_sources(query, [
        _CapturedSource("2026 计算机班级群", "student_chat", g1),
        _CapturedSource("2026 计算机班级二群", "student_chat", g2),
    ])
    assert [source.name for source in kept] == ["2026 计算机班级群"]
    assert decisions and decisions[0]["dropped"] == ["2026 计算机班级二群"]

    _, w1 = _wx_messages("W1")
    _, w2 = _wx_messages("W2")
    kept, decisions = select_sources(
        "搜索「COMP7503」时有两个相似群（「COMP7503」与「COMP7503-2026」），"
        "进入正确那个并找 DDL", [
            _CapturedSource("COMP7503课程群", "student_chat", w1),
            _CapturedSource("COMP7503-2026", "student_chat", w2),
        ],
    )
    assert [source.name for source in kept] == ["COMP7503课程群"]
    assert decisions[0]["dropped"] == ["COMP7503-2026"]


def test_group_name_space_and_order_differences_are_same_group():
    assert source_names_equivalent("COMP7503课程群", "COMP7503 课程群")
    assert source_names_equivalent("COMP7503课程群", "课程群 COMP7503")
    assert source_names_equivalent("2026 计算机班级群二群", "2026 计算机班级二群")
    assert not source_names_equivalent("COMP7503课程群", "COMP7415课程群")
    # 一字之差（二群）是相似群，不是同一个群
    assert not source_names_equivalent("2026 计算机班级群", "2026 计算机班级群二群")


def test_source_config_parser_matches_normalized_group_name():
    spec = SourceSpec(name="COMP7503课程群", kind="chat", provider="mobile")
    selected = SourceConfigParser().parse("监控「COMP7503 课程群」的消息", [spec])
    assert [item.name for item in selected] == ["COMP7503课程群"]


def test_source_selection_reason_is_archived_in_trace():
    _, g1 = _qq_messages("G1")
    _, g2 = _qq_messages("G2")
    sources = [
        _CapturedSource("2026 计算机班级群", "student_chat", g1),
        _CapturedSource("2026 计算机班级二群", "student_chat", g2),
    ]
    agent = DdlAgent(sources=sources, now=QQ_NOW)
    result = agent.run(
        "在 QQ 中查找「2026 计算机班级群」里实验报告二的截止时间", write=False
    )
    assert [source.name for source in result.plan.sources] == ["2026 计算机班级群"]
    assert result.trace["source_selection"][0]["dropped"] == ["2026 计算机班级二群"]


# ---------------------------------------------------------------- 修复前后判分
def test_q04_before_partial_after_hit():
    old = _old_task_result("Q04", wx=False)
    assert not any(
        _get(item, "deadline_iso") == "2026-11-28T23:59:00+08:00" for item in old
    )

    records = _resolve(_saved_candidates("G4"))
    new = [item for item in records if item.deadline_iso == "2026-11-28T23:59:00+08:00"]
    assert len(new) == 1
    assert new[0].task_name == "操作系统作业一"


def test_d11_before_fail_after_hit():
    old = _old_task_result("W-D11")
    assert not any(
        _get(item, "deadline_iso") == "2026-12-03T23:59:00+08:00" for item in old
    )

    records = _resolve(_saved_candidates("W4", wx=True))
    assert records[0].deadline_iso == "2026-12-03T23:59:00+08:00"


def test_d13_before_fail_after_hit():
    old = _old_task_result("W-D13")
    assert not any(
        _get(item, "status") == "confirmed"
        and _get(item, "deadline_iso") == "2026-11-29T23:59:00+08:00"
        for item in old
    )

    records = _resolve(_saved_candidates("W1", wx=True) + [_email_candidates()["O2"]])
    target = next(
        item for item in records
        if normalize_task_name(item.task_name) == normalize_task_name("机器学习作业三")
    )
    assert target.status == "confirmed"
    assert target.deadline_iso == "2026-11-29T23:59:00+08:00"


class _BothSourcesPlanner:
    """模拟修复后的规划器：查询点名邮件时，群聊与邮件都在候选里。"""

    def plan(self, query, sources, now=None):
        return RetrievalPlan(query=query, sources=list(sources))


class _StaticExtractor:
    def __init__(self, candidates):
        self.candidates = candidates

    def extract(self, messages, now=None):
        ids = {message.message_id for message in messages}
        return [item for item in self.candidates if item.message_id in ids]


def test_d13_pipeline_reads_email_source_and_records_conflict():
    """D13 端到端：规划保留邮件源 → 读到 11-30 → 与群里 11-29 形成冲突记录。"""
    _, messages = _wx_messages("W1")
    chat = _CapturedSource("微信/COMP7503课程群", "student_chat", messages)
    email_spec = FIXTURE["wx"]["emails"]["O2"]
    mail_message = RawMessage(
        message_id="O2",
        source_name="Outlook/收件箱",
        source_type="email",
        sender=email_spec["sender"],
        published_at=datetime.fromisoformat(email_spec["published_at"]),
        subject=email_spec["subject"],
        body=email_spec["body"],
    )
    mail = _CapturedSource(
        "Outlook/[COMP7503] Assignment 3 ", "email", [mail_message]
    )
    candidates = _saved_candidates("W1", wx=True) + [_email_candidates()["O2"]]
    agent = DdlAgent(
        sources=[chat, mail],
        extractor=_StaticExtractor(candidates),
        planner=_BothSourcesPlanner(),
        now=WX_NOW,
    )

    result = agent.run(
        "同一作业群里说 11-29、邮件说 11-30，分别读取两个来源，报告冲突并给出结论",
        write=False,
    )

    assert [source.name for source in result.plan.sources] == [chat.name, mail.name]
    target = next(
        record for record in result.records
        if normalize_task_name(record.task_name) == normalize_task_name("机器学习作业三")
    )
    assert target.deadline_iso == "2026-11-29T23:59:00+08:00"
    assert any("2026-11-30" in json.dumps(item, ensure_ascii=False, default=str)
               for item in target.conflicts)


def test_d08_before_fail_after_hit():
    old = _old_task_result("W-D08")
    assert not any(
        _get(item, "status") == "confirmed"
        and _get(item, "deadline_iso") == "2026-11-29T23:59:00+08:00"
        for item in old
    )

    records = _resolve(_saved_candidates("W1", wx=True))
    target = next(
        item for item in records
        if normalize_task_name(item.task_name) == normalize_task_name("机器学习作业三")
    )
    assert target.status == "confirmed"
    assert target.course == "COMP7503"
    assert target.deadline_iso == "2026-11-29T23:59:00+08:00"


def test_d12_image_table_deadline_stays_confirmed():
    records = _resolve(_saved_candidates("W3", wx=True))
    target = next(
        item for item in records
        if normalize_task_name(item.task_name) == normalize_task_name("Final Report")
    )
    assert target.status == "confirmed"
    assert target.deadline_iso == "2026-12-08T23:59:00+08:00"
