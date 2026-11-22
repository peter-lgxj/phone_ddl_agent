"""上一轮真机全量判分暴露的五条缺陷的离线回归。

数据来自 ``runs/_verify/live_20261122``（真机读屏文本裁剪到
``tests/fixtures/live_20261122.json``），不联网、不碰手机。
覆盖 D16 选源、D17 读取状态、D09 月末换算、D06 无标记置顶、坐标越界 tap 路径。
"""

from __future__ import annotations

import json
import types
from datetime import datetime
from pathlib import Path

import pytest

import config
import screen_agent
from ddl_agent.dedupe import normalize_task_name
from ddl_agent.extract.normalizer import TimeNormalizer
from ddl_agent.extract.rules import RuleExtractor
from ddl_agent.connectors.graph_mail import GraphMailConnector
from ddl_agent.llm.planner import LLMQueryPlanner
from ddl_agent.models import RawMessage, RetrievalPlan, SourceSpec
from ddl_agent.pipeline import DdlAgent
from ddl_agent.query_focus import select_sources_by_content
from ddl_agent.settings import TZ

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "live_20261122.json").read_text(encoding="utf-8")
)
NOW = datetime.fromisoformat(FIXTURE["now"])


class _CapturedSource:
    def __init__(self, name: str, messages: list[RawMessage]):
        self.name = name
        self.source_type = "student_chat"
        self.kind = "chat"
        self.messages = messages

    def fetch(self, _plan=None) -> list[RawMessage]:
        return list(self.messages)


class _PlannerClient:
    """模拟真机当次 LLM 规划输出（D16 只回了带年份后缀的诱饵群）。"""

    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, *_args, **_kwargs):
        return self.payload


def _messages(group: str) -> list[RawMessage]:
    return [
        RawMessage(
            message_id=f"{group}-{index}",
            source_name=group,
            source_type="student_chat",
            sender=item.get("sender", ""),
            published_at=NOW,
            subject=f"微信 / {group}",
            body=item["text"],
            pinned=bool(item.get("pinned")),
        )
        for index, item in enumerate(FIXTURE["wx"][group], start=1)
    ]


def _source(group: str) -> _CapturedSource:
    return _CapturedSource(group, _messages(group))


def _email_source(name: str, subject: str, body: str,
                  published_at: datetime) -> _CapturedSource:
    message = RawMessage(
        message_id=f"email:{name}",
        source_name=name,
        source_type="email",
        sender="XIAOFAN YE",
        published_at=published_at,
        subject=subject,
        body=f"{subject}\n{body}",
    )
    return _CapturedSource(name, [message])


class _DroppingExtractor:
    """模拟 LLM 完全漏抽某条消息（D06 的 1/3 失败样本）。"""

    def __init__(self, drop_message_ids):
        self.drop = set(drop_message_ids)
        self.base = RuleExtractor()

    def extract(self, messages, now=None):
        return [
            candidate for candidate in self.base.extract(messages, now=now)
            if candidate.message_id not in self.drop
        ]


def _wx_names(result) -> set[tuple[str, str | None]]:
    return {
        (normalize_task_name(record.task_name), record.deadline_iso)
        for record in result.records
    }


# ---------------------------------------------------------------- D16 相似群
def test_d16_planner_picks_decoy_but_pipeline_restores_base_group():
    """LLM 只回了 COMP7503-2026；补齐相似候选后应按基名优先回到课程群。"""
    sources = [_source("COMP7503课程群"), _source("COMP7503-2026")]
    planner = LLMQueryPlanner(_PlannerClient({
        "sources": ["COMP7503-2026"],
        "reason": "只选与查询最匹配的一个群",
    }))
    agent = DdlAgent(
        sources=sources,
        extractor=RuleExtractor(),
        planner=planner,
        now=NOW,
    )
    result = agent.run(
        "搜索「COMP7503」时有两个相似群（「COMP7503」与「COMP7503-2026」），"
        "进入正确那个并找 DDL",
        write=False,
    )

    assert [source.name for source in result.plan.sources] == ["COMP7503课程群"]
    # 真机数据里正确群只有作业三 11-29；诱饵群的作业二 11-15 不得出现
    assert _wx_names(result) == {("assignment 3", "2026-11-29T23:59:00+08:00")}
    assert any(item["dropped"] == ["COMP7503-2026"] for item in result.trace["source_selection"])


def test_similar_name_candidate_without_query_task_is_not_selected():
    query = "在「COMP7503课程群」和「COMP7503-2026」里找作业三的截止时间"
    chosen, decision = select_sources_by_content(
        query,
        [
            ("COMP7503课程群", _messages("COMP7503课程群")),
            ("COMP7503-2026", _messages("COMP7503-2026")),
        ],
    )
    assert chosen == ["COMP7503课程群"]
    assert decision["dropped"] == ["COMP7503-2026"]


# ---------------------------------------------------------------- D17 读取状态
def test_read_empty_is_needs_review_not_no_match():
    agent = DdlAgent(
        sources=[_CapturedSource("COMP7503课程群", [])],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入「COMP7503 课程群」，找到作业三的截止时间", write=False
    )

    assert result.needs_review is True
    assert "读取为空" in result.review_reason
    assert result.trace["read_state"] == "empty"
    assert result.trace["no_match"] is False
    assert result.trace["needs_review"] is True


def test_read_ok_without_target_is_no_match_not_needs_review():
    agent = DdlAgent(
        sources=[_source("闲聊群")],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "进入一个只有闲聊的群，查找「数据库作业」的截止时间（实际没有这条消息）",
        write=False,
    )

    assert result.records == []
    assert result.trace["read_state"] == "ok"
    assert result.trace["no_match"] is True
    assert result.trace["needs_review"] is False
    assert result.needs_review is False


def test_d17_unparseable_deadline_goes_to_human_review():
    agent = DdlAgent(
        sources=[_source("闲聊群")],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "群里写「llm 的 ddl 是 11.31」，请报告该 DDL 的截止时间", write=False
    )

    assert result.records == []
    assert result.trace["read_state"] == "ok"
    assert result.trace["no_match"] is False
    assert result.trace["needs_review"] is True
    assert result.needs_review is True
    assert "未解析出" in result.review_reason


# ---------------------------------------------------------------- D09 月末
@pytest.mark.parametrize(
    ("now", "text", "expected"),
    [
        (datetime(2026, 11, 22, 4, 0, tzinfo=TZ), "月底前交", "2026-11-30T23:59:00+08:00"),
        (datetime(2026, 1, 15, 9, 0, tzinfo=TZ), "本月底前交", "2026-01-31T23:59:00+08:00"),
        (datetime(2026, 4, 10, 9, 0, tzinfo=TZ), "月底前交", "2026-04-30T23:59:00+08:00"),
        (datetime(2026, 12, 15, 9, 0, tzinfo=TZ), "月底前交", "2026-12-31T23:59:00+08:00"),
        (datetime(2026, 12, 15, 9, 0, tzinfo=TZ), "下月初交", "2027-01-01T23:59:00+08:00"),
    ],
)
def test_month_boundary_is_inferred_with_basis(now, text, expected):
    result = TimeNormalizer().normalize(text, now=now)

    assert result.iso == expected
    assert result.unresolved is False
    assert result.inferred is True
    assert result.inference_basis
    assert "+08:00" in result.iso


def test_d09_group_notice_is_converted_to_month_end():
    agent = DdlAgent(
        sources=[_source("COMP7415课程群")],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入课程群，找到「月底前交」那条通知，换算成具体日期",
        write=False,
    )

    target = next(
        record for record in result.records
        if normalize_task_name(record.task_name) == normalize_task_name("小组课程报告")
    )
    assert target.deadline_iso == "2026-11-30T23:59:00+08:00"
    assert target.status == "confirmed"
    assert target.inferred is True
    assert target.inference_basis


# ---------------------------------------------------------------- D06 无标记置顶
def test_d06_pinned_metadata_keeps_only_1120():
    agent = DdlAgent(
        sources=[_source("LLM小组(5)")],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert result.trace["focus_filter"]["pinned_filter"] in ("applied", "heuristic")
    assert _wx_names(result) == {("小组assignment", "2026-11-20T23:59:00+08:00")}


def test_d06_without_marker_or_banner_uses_top_earliest_heuristic():
    messages = [message for message in _messages("LLM小组(5)")
                if "置顶了一条消息" not in message.body]
    for message in messages:
        message.pinned = False
    agent = DdlAgent(
        sources=[_CapturedSource("LLM小组(5)", messages)],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert result.trace["focus_filter"]["pinned_filter"] == "heuristic"
    assert _wx_names(result) == {("小组assignment", "2026-11-20T23:59:00+08:00")}


# ---------------------------------------------------------------- 坐标 tap 路径
class _FakeOcr:
    def __init__(self, elements):
        self.elements = elements

    def detect_tree(self):
        return list(self.elements)

    def detect_ocr(self, _path):
        return []


def _tap_agent(elements) -> screen_agent.ScreenAgent:
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.ocr = _FakeOcr(elements)
    agent._last_image_meta = {"orig_size": (1080, 2374)}
    agent._gui_detector = None
    return agent


def _run_tap(agent, action):
    timings: dict = {}
    memory = types.SimpleNamespace(steps=[], task="")
    result = agent._snap_mapped_action(action, timings, memory)
    return result, timings


def test_tap_outside_screen_is_rejected_and_logged(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    agent = _tap_agent([])
    action, timings = _run_tap(agent, {
        "action_type": "tap", "x": 5000, "y": 5000,
        "target": "2026 计算机班级群", "description": "点击群聊",
    })

    assert action["action_type"] == "wait"
    assert action["coordinate_rejected"] == "outside_screen"
    assert timings["coordinate_guard"]["reason"] == "outside_screen"

    agent.run_dir = tmp_path
    agent._last_step_timings = timings
    agent._archive_step(1, action, "当前界面")
    record = json.loads((tmp_path / "step_001.json").read_text(encoding="utf-8"))
    assert record["action"]["coordinate_rejected"] == "outside_screen"
    assert record["timings"]["coordinate_guard"]["reason"] == "outside_screen"


def test_tap_ambiguous_list_target_is_rejected_and_logged(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    agent = _tap_agent([
        {"text": "群聊", "center": (540, 900), "bbox": (100, 800, 980, 1000),
         "source": "xml", "type": "control"},
        {"text": "群聊", "center": (540, 1700), "bbox": (100, 1600, 980, 1800),
         "source": "xml", "type": "control"},
    ])
    action, timings = _run_tap(agent, {
        "action_type": "tap", "x": 540, "y": 1200,
        "target": "群聊", "description": "打开群聊",
    })

    assert action["action_type"] == "wait"
    assert action["coordinate_rejected"] == "ambiguous_list_target"
    assert timings["coordinate_guard"]["candidates"] == 2


def test_misaligned_tap_snaps_to_bounds_center(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    agent = _tap_agent([
        {"text": "家人群", "center": (540, 1400), "bbox": (100, 1300, 980, 1500),
         "source": "xml", "type": "control"},
    ])
    action, timings = _run_tap(agent, {
        "action_type": "tap", "x": 540, "y": 1150,
        "target": "家人群", "description": "点击群聊家人群",
    })

    assert action["action_type"] == "tap"
    assert (action["x"], action["y"]) == (540, 1400)
    assert timings["coordinate_guard"]["source"] == "bounds"
    assert timings["coordinate_guard"]["target"] == "家人群"


def test_tap_outside_screen_with_matching_target_is_rejected_not_snapped(monkeypatch):
    """屏幕外坐标即使能匹配目标文本也必须拒绝，不能被吸附洗成合法坐标。"""
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    agent = _tap_agent([
        {"text": "家人群", "center": (540, 1400), "bbox": (100, 1300, 980, 1500),
         "source": "xml", "type": "control"},
    ])
    action, timings = _run_tap(agent, {
        "action_type": "tap", "x": 5000, "y": 5000,
        "target": "家人群", "description": "点击会话家人群",
    })

    assert action["action_type"] == "wait"
    assert action["coordinate_rejected"] == "outside_screen"
    assert timings["coordinate_guard"]["reason"] == "outside_screen"
    assert (action.get("x"), action.get("y")) == (5000, 5000)


# ---------------------------------------------------------------- D06 抽取兜底
def test_d06_rule_fallback_recovers_pinned_when_llm_omits_it():
    messages = _messages("LLM小组(5)")
    drop_id = messages[0].message_id  # 置顶消息
    agent = DdlAgent(
        sources=[_CapturedSource("LLM小组(5)", messages)],
        extractor=_DroppingExtractor({drop_id}),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert result.trace["focus_filter"]["pinned_filter"] in ("applied", "heuristic")
    assert _wx_names(result) == {("小组assignment", "2026-11-20T23:59:00+08:00")}


def test_d06_unrecoverable_pinned_suppresses_noise_and_needs_review():
    messages = [
        RawMessage("m1", "LLM小组(5)", "student_chat", "叶小凡", NOW,
                   "微信 / LLM小组(5)", "小组作业提交：截止时间待定，先传到共享文件夹",
                   pinned=True),
        RawMessage("m2", "LLM小组(5)", "student_chat", "", NOW,
                   "微信 / LLM小组(5)", "“叶小凡”置顶了一条消息"),
        RawMessage("m3", "LLM小组(5)", "student_chat", "叶小凡", NOW,
                   "微信 / LLM小组(5)", "作业三再提醒一次，11.29 23:59，别拖"),
        RawMessage("m4", "LLM小组(5)", "student_chat", "叶小凡", NOW,
                   "微信 / LLM小组(5)", "课程报告 12.05 23:59，跟上面那个不是一回事，别混"),
    ]
    agent = DdlAgent(
        sources=[_CapturedSource("LLM小组(5)", messages)],
        extractor=RuleExtractor(),
        now=NOW,
    )
    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert result.trace["focus_filter"].get("noise_suppressed") is True
    # 只允许留下置顶那条（无截止时间）的记录，11-29/12-05 不得冒充结果
    assert all(record.deadline_iso is None for record in result.records)
    assert result.needs_review is True
    assert "置顶" in result.review_reason


# ---------------------------------------------------------------- D13 发布时间
def test_d13_conflicts_use_real_published_at_not_read_time():
    group_time = datetime(2026, 11, 18, 20, 0, tzinfo=TZ)
    email_time = datetime(2026, 11, 15, 9, 0, tzinfo=TZ)
    group = _CapturedSource("COMP7503课程群", [
        RawMessage("g1", "COMP7503课程群", "student_chat", "叶小凡", group_time,
                   "微信 / COMP7503课程群", "机器学习作业三，11.29 23:59 前交到 Moodle"),
    ])
    email = _email_source(
        "Outlook / COMP7503 Assignment 3",
        "[COMP7503] Assignment 3 — submission window",
        "Assignment 3 deadline: 2026-11-30 23:59 (HKT).\nSent: 2026-11-15",
        email_time,
    )
    agent = DdlAgent(
        sources=[group, email], extractor=RuleExtractor(), now=NOW,
    )
    result = agent.run(
        "同一作业，群里说 11-29，邮件说 11-30。分别读取两个来源，报告冲突并给出结论",
        write=False,
    )

    record = next(record for record in result.records
                  if normalize_task_name(record.task_name) == "assignment 3")
    conflict = record.conflicts[0]
    sides = {side["source_type"]: side for side in conflict["sides"]}
    assert sides["student_chat"]["published_at"].startswith("2026-11-18")
    assert sides["student_chat"]["published_at_note"] == ""
    assert sides["email"]["published_at"].startswith("2026-11-15")
    assert "2026-11-18" in conflict["basis"]
    assert "2026-11-22" not in json.dumps(record.conflicts, ensure_ascii=False)


def test_d13_unknown_published_at_stays_null_and_annotated():
    group = _CapturedSource("COMP7503课程群", [
        RawMessage("g1", "COMP7503课程群", "student_chat", "叶小凡", None,
                   "微信 / COMP7503课程群", "机器学习作业三，11.29 23:59 前交到 Moodle"),
    ])
    email = _email_source(
        "Outlook / COMP7503 Assignment 3",
        "[COMP7503] Assignment 3 — submission window",
        "Assignment 3 deadline: 2026-11-30 23:59 (HKT).",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    agent = DdlAgent(sources=[group, email], extractor=RuleExtractor(), now=NOW)
    result = agent.run(
        "同一作业，群里说 11-29，邮件说 11-30。分别读取两个来源，报告冲突并给出结论",
        write=False,
    )

    record = next(record for record in result.records
                  if record.conflicts)
    sides = {side["source_type"]: side for side in record.conflicts[0]["sides"]}
    assert sides["student_chat"]["published_at"] is None
    assert sides["student_chat"]["published_at_note"] == "发布时间未知"
    assert "2026-11-22" not in json.dumps(record.conflicts, ensure_ascii=False)


def test_graph_mail_invalid_received_time_is_null_and_marked(monkeypatch):
    import ddl_agent.connectors.graph_mail as graph_mail

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"value": [{
                "id": "m1",
                "subject": "subject",
                "from": {"emailAddress": {"address": "a@b.com"}},
                "receivedDateTime": "not-a-date",
                "bodyPreview": "body",
                "conversationId": "c1",
            }]}).encode("utf-8")

    monkeypatch.setattr(graph_mail.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    connector = GraphMailConnector(name="outlook", token_provider=lambda: "token")
    plan = RetrievalPlan(
        query="q",
        sources=[SourceSpec(name="outlook", kind="email", provider="graph")],
    )
    messages = connector.fetch(plan)

    assert messages[0].published_at is None
    assert messages[0].needs_review is True
    assert "发布时间未知" in messages[0].review_reason


# ---------------------------------------------------------------- D16 规划器先验
def test_d16_planner_prior_prefers_base_group_on_first_pick():
    sources = [_source("COMP7503课程群"), _source("COMP7503-2026")]
    planner = LLMQueryPlanner(_PlannerClient({
        "sources": ["COMP7503-2026"],
        "reason": "带年份后缀更像当前学期",
    }))
    plan = planner.plan(
        "搜索「COMP7503」时有两个相似群（「COMP7503」与「COMP7503-2026」），"
        "进入正确那个并找 DDL",
        sources,
        NOW,
    )

    assert [source.name for source in plan.sources] == ["COMP7503课程群"]
