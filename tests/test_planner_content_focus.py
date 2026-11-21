"""选源粗扫 + 查询聚焦回归。

真机目标：
- Q01/Q02：只输出 G1 的实验报告二 11-22，不串 G2 的 11-24，不多报作业三；
- Q03：内容粗扫选中 G1（判分表答案 机器学习作业三 11-29）；
- Q04：内容粗扫选中 G4，输出更正后的 11-28；
- D08：只输出作业三 11-29，不多报作业二 11-15；
- D10/D15/D16/D17/D18：新过滤逻辑不误伤。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ddl_agent.dedupe import normalize_task_name
from ddl_agent.extract.message_context import detect_correction
from ddl_agent.models import DeadlineCandidate, RawMessage
from ddl_agent.pipeline import DdlAgent
from ddl_agent.planner import select_sources
from ddl_agent.query_focus import focus_records, task_matches
from ddl_agent.settings import TZ

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "real_device_20261121.json").read_text(encoding="utf-8")
)
QQ_NOW = datetime(2026, 11, 21, 0, 20, tzinfo=TZ)
_REL_TIME_RE = re.compile(r"昨天\s+(\d{1,2}):(\d{2})")


class _CapturedSource:
    def __init__(self, name: str, source_type: str, messages: list[RawMessage]):
        self.name = name
        self.source_type = source_type
        self.messages = messages

    def fetch(self, _plan=None) -> list[RawMessage]:
        return list(self.messages)


class _RecordedExtractor:
    """按 message_id 回放真机当次 LLM 候选，避免规则抽取的日期解析差异干扰判分。"""

    def __init__(self, candidates: dict[str, list[DeadlineCandidate]]):
        self.candidates = candidates

    def extract(self, messages: list[RawMessage], now=None) -> list[DeadlineCandidate]:
        return [
            candidate
            for message in messages
            for candidate in self.candidates.get(message.message_id, [])
        ]


def _qq_messages(group_id: str) -> tuple[str, list[RawMessage]]:
    spec = FIXTURE["qq"][group_id]
    messages = []
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


def _candidates(group_id: str, wx: bool = False) -> list[DeadlineCandidate]:
    spec = FIXTURE["wx"]["groups"][group_id] if wx else FIXTURE["qq"][group_id]
    messages = {
        message.message_id: message
        for message in (_wx_messages(group_id)[1] if wx else _qq_messages(group_id)[1])
    }
    course = (re.findall(r"COMP\d{4}", spec["group"]) or [None])[0]
    result = []
    for item in spec["candidates"]:
        message = messages[item["message_id"]]
        marker = detect_correction(message.body)
        result.append(
            DeadlineCandidate(
                candidate_id=item["candidate_id"],
                task_name_raw=item["task_name_raw"],
                course=item["course"] or course,
                deadline_raw=item["deadline_raw"],
                deadline_iso=item["deadline_iso"],
                source_type="student_chat",
                source_name=item["source_name"],
                published_at=message.published_at,
                message_id=item["message_id"],
                message_subject=item["message_subject"],
                unresolved_time=item["unresolved_time"],
                extractor_confidence=item["extractor_confidence"],
                needs_review=(item["extractor_confidence"] or 0) < 0.5,
                course_from="model" if item["course"] else "source_name",
                source_order=int(item["message_id"].split("-")[-1]),
                is_correction=marker is not None,
                correction_marker=marker,
            )
        )
    return result


def _extractor_for(groups: list[tuple[str, bool]]) -> _RecordedExtractor:
    by_message: dict[str, list[DeadlineCandidate]] = {}
    for group_id, wx in groups:
        for candidate in _candidates(group_id, wx):
            by_message.setdefault(candidate.message_id, []).append(candidate)
    return _RecordedExtractor(by_message)


def _sources_for(groups: list[tuple[str, bool]]) -> list[_CapturedSource]:
    sources = []
    for group_id, wx in groups:
        name, messages = _wx_messages(group_id) if wx else _qq_messages(group_id)
        sources.append(_CapturedSource(name, "student_chat", messages))
    return sources


def _run(query: str, groups: list[tuple[str, bool]], now=QQ_NOW):
    agent = DdlAgent(
        sources=_sources_for(groups),
        extractor=_extractor_for(groups),
        now=now,
    )
    return agent.run(query, write=False)


def _task(record_name: str) -> str:
    return normalize_task_name(record_name)


def _old(task_id: str) -> list[dict]:
    return FIXTURE["old_results"][task_id]


# ---------------------------------------------------------------- 端到端
def test_q01_focus_filters_same_group_noise():
    old = _old("Q01")
    assert len(old) == 2  # 旧结果：实验报告二 + 同群作业三，判 partial

    result = _run(
        "打开 QQ，进入「2026 计算机班级群」，找到实验报告二的截止时间，报告任务名和截止时间。",
        [("G1", False), ("G2", False), ("G3", False), ("G4", False)],
    )
    assert [source.name for source in result.plan.sources] == ["2026 计算机班级群"]
    assert len(result.records) == 1
    record = result.records[0]
    assert _task(record.task_name) == _task("实验报告二")
    assert record.deadline_iso == "2026-11-22T23:59:00+08:00"
    assert result.trace["focus_filter"]["task_filter"] == "applied"
    assert result.trace["content_probe"]["llm_calls"] == 0


def test_q02_similar_group_is_dropped_and_trap_date_absent():
    assert len(_old("Q02")) == 2  # 旧结果同样多报了作业三
    result = _run(
        "在 QQ 中搜索「2026 计算机班级群」，进入正确的群（注意有相似群名），"
        "找到实验报告二的截止时间。",
        [("G1", False), ("G2", False), ("G3", False), ("G4", False)],
    )
    assert [source.name for source in result.plan.sources] == ["2026 计算机班级群"]
    assert any(item["dropped"] == ["2026 计算机班级二群"]
               for item in result.trace["source_selection"])
    assert len(result.records) == 1
    assert result.records[0].deadline_iso == "2026-11-22T23:59:00+08:00"
    assert "2026-11-24T23:59:00+08:00" not in [r.deadline_iso for r in result.records]


def test_q03_content_probe_selects_g1_and_filters_other_courses():
    assert not any(r["task_name"] == "机器学习作业三" for r in _old("Q03"))
    result = _run(
        "在 QQ 中查找 COMP7503 机器学习作业三的截止时间；只报告 COMP7503 的这条，"
        "不要报告其他课程。",
        [("G1", False), ("G2", False), ("G3", False), ("G4", False)],
    )
    assert [source.name for source in result.plan.sources] == ["2026 计算机班级群"]
    assert result.trace["content_probe"]["enabled"] is True
    assert result.trace["content_probe"]["kept"] == ["2026 计算机班级群"]
    assert len(result.records) == 1
    record = result.records[0]
    assert _task(record.task_name) == _task("机器学习作业三")
    assert record.course == "COMP7503"
    assert record.deadline_iso == "2026-11-29T23:59:00+08:00"


def test_q04_content_probe_selects_g4_and_keeps_correction():
    assert not any(r["deadline_iso"] == "2026-11-28T23:59:00+08:00"
                   for r in _old("Q04"))
    result = _run(
        "在 QQ 中查找「操作系统作业一」的最新截止时间；群里有重复和更正通知。",
        [("G1", False), ("G2", False), ("G3", False), ("G4", False)],
    )
    assert [source.name for source in result.plan.sources] == ["COMP7503 作业通知群"]
    assert result.trace["content_probe"]["kept"] == ["COMP7503 作业通知群"]
    assert len(result.records) == 1
    record = result.records[0]
    assert _task(record.task_name) == _task("操作系统作业一")
    assert record.deadline_iso == "2026-11-28T23:59:00+08:00"
    assert record.superseded_by
    assert record.superseded_by[0]["deadline"] == "2026-11-26T23:59:00+08:00"


def test_d08_focus_drops_previous_assignment_in_same_group():
    assert not any(
        r["deadline_iso"] == "2026-11-29T23:59:00+08:00" for r in _old("W-D08")
    )
    result = _run(
        "打开微信，进入「COMP7503 课程群」，找到作业三的截止时间"
        "（同群还有作业二的旧时间，注意不要报错）",
        [("W1", True), ("W2", True), ("W3", True), ("W4", True)],
        now=datetime(2026, 11, 21, 0, 50, tzinfo=TZ),
    )
    assert [source.name for source in result.plan.sources] == ["COMP7503课程群"]
    assert len(result.records) == 1
    record = result.records[0]
    assert _task(record.task_name) == _task("作业三")
    assert record.deadline_iso == "2026-11-29T23:59:00+08:00"
    assert "2026-11-15T23:59:00+08:00" not in [r.deadline_iso for r in result.records]


# ---------------------------------------------------------------- 不误伤
def _plain_records(groups: list[tuple[str, bool]]):
    candidates = []
    for group_id, wx in groups:
        candidates.extend(_candidates(group_id, wx))
    clusters = DdlAgent(
        sources=_sources_for(groups), extractor=_extractor_for(groups), now=QQ_NOW
    ).deduplicator.cluster(candidates)
    return [
        record
        for record in DdlAgent(
            sources=_sources_for(groups), extractor=_extractor_for(groups), now=QQ_NOW
        ).resolver.resolve(clusters).records
    ]


def test_d10_course_restriction_keeps_target_and_drops_other_course():
    records = _plain_records([("W5", True), ("W3", True)])
    filtered, decision = focus_records(
        "打开微信，进入「LLM小组」，只报告 COMP7503 的 DDL，不要报 COMP7415 的",
        records,
    )
    assert decision["course_filter"] == "applied"
    assert any(_task(record.task_name) == _task("机器学习作业三") for record in filtered)
    assert not any(record.course == "COMP7415" for record in filtered)


def test_d15_course_ddl_picks_latest_homework_only():
    records = _plain_records([("W1", True)])
    filtered, decision = focus_records(
        "查找课程作业 DDL。实习内推群里「网申截止 11-21」、家人群「报名截止」都是诱饵，不要报",
        records,
    )
    # 泛称“课程作业 DDL”按最近一次作业通知聚焦，只留作业三，不多报作业二
    assert decision["task_filter"] == "latest_homework"
    assert len(filtered) == 1
    assert _task(filtered[0].task_name) == _task("作业三")
    assert filtered[0].deadline_iso == "2026-11-29T23:59:00+08:00"


def test_d16_similar_group_choice_and_no_task_filter():
    _, w1 = _wx_messages("W1")
    _, w2 = _wx_messages("W2")
    kept, decisions = select_sources(
        "搜索「COMP7503」时有两个相似群（「COMP7503」与「COMP7503-2026」），"
        "进入正确那个并找 DDL",
        [_CapturedSource("COMP7503课程群", "student_chat", w1),
         _CapturedSource("COMP7503-2026", "student_chat", w2)],
    )
    assert [source.name for source in kept] == ["COMP7503课程群"]
    assert decisions[0]["dropped"] == ["COMP7503-2026"]

    result = _run(
        "搜索「COMP7503」时有两个相似群（「COMP7503」与「COMP7503-2026」），"
        "进入正确那个并找 DDL",
        [("W1", True), ("W2", True)],
        now=datetime(2026, 11, 21, 0, 50, tzinfo=TZ),
    )
    assert [source.name for source in result.plan.sources] == ["COMP7503课程群"]
    assert result.trace["content_probe"]["enabled"] is False
    assert any(
        _task(record.task_name) == _task("机器学习作业三")
        and record.deadline_iso == "2026-11-29T23:59:00+08:00"
        for record in result.records
    )


def test_d17_invalid_time_keeps_records():
    w8 = _plain_records([("W8", True)])
    d17, decision17 = focus_records(
        "群里写「llm 的 ddl 是 11.31」，请报告该 DDL 的截止时间", w8
    )
    assert decision17["task_filter"] == "disabled"
    assert len(d17) == len(w8)
    assert any(_task(record.task_name) == _task("llm") for record in d17)


def test_d18_missing_task_returns_empty_with_no_match():
    w8 = _plain_records([("W8", True)])
    d18, decision18 = focus_records(
        "进入一个只有闲聊的群，查找「数据库作业」的截止时间（实际没有这条消息）", w8
    )
    # 明确点名「数据库作业」但语料没有：输出空结果并标 no_match，不拿 llm 记录凑数
    assert decision18["task_filter"] == "no_match"
    assert decision18["no_match"] is True
    assert d18 == []


def test_d01_latest_notice_keeps_one_record():
    records = _plain_records([("W1", True)])
    filtered, decision = focus_records("查看群消息，总结最近一条作业截止通知", records)

    assert decision["task_filter"] == "latest_homework"
    assert len(filtered) == 1
    assert _task(filtered[0].task_name) == _task("作业三")
    assert filtered[0].deadline_iso == "2026-11-29T23:59:00+08:00"


def test_d07_latest_homework_notice_from_last_week():
    records = _plain_records([("W5", True)])
    filtered, decision = focus_records(
        "向上翻阅到上周发的作业通知，报告最近的作业截止时间", records
    )

    assert decision["task_filter"] == "latest_homework"
    assert len(filtered) == 1
    assert _task(filtered[0].task_name) == _task("作业三")
    assert filtered[0].deadline_iso == "2026-11-29T23:59:00+08:00"


def test_d12_english_task_name_matches_chinese_alias():
    # 图片任务里写 Final Report / Final report，对应中文「期末报告」；
    # 只做已知映射，不认识的英文名保留原名（task_matches 返回 False 而不是硬猜）
    assert task_matches("期末报告", "Final Report")
    assert task_matches("Final report", "期末报告")
    assert not task_matches("期末报告", "Final Presentation")


def test_d09_month_end_stays_uncertain_and_null_deadline():
    records = _plain_records([("W3", True)])
    target = next(
        record for record in records
        if _task(record.task_name) == _task("小组课程报告")
    )
    # 口径：「月底前」不猜日期；uncertain + deadline=null 即正确，判分表按 hit
    assert target.status == "uncertain"
    assert target.deadline_iso is None
