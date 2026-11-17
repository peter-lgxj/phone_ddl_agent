"""LLM 组件离线测试（不发网络请求）+ 可选真实调用。"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from ddl_agent.conflict import ConflictResolver
from ddl_agent.extract.llm_extractor import LLMDeadlineExtractor
from ddl_agent.llm.arbiter import LLMConflictArbiter
from ddl_agent.llm.client import LLMClient, UsageTracker, parse_json
from ddl_agent.llm.planner import LLMQueryPlanner
from ddl_agent.models import DeadlineCandidate, RawMessage, TaskCluster
from ddl_agent.settings import TZ

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
ISO_A = "2026-12-08T23:59:00+08:00"
ISO_B = "2026-12-10T23:59:00+08:00"


class FakeClient:
    """模拟 LLMClient 的 complete_json，按顺序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, prompt, system=None, kind="json"):
        self.calls.append(kind)
        return self.responses.pop(0)


def _message(body: str) -> RawMessage:
    return RawMessage(
        message_id="m1",
        source_name="outlook",
        source_type="official_email",
        sender="ta@cs.hku.hk",
        published_at=NOW,
        subject="[COMP7608] Assignment 2",
        body=body,
    )


def _candidate(iso: str, source_type: str, candidate_id: str) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw="Assignment 2",
        course="COMP7608",
        deadline_raw=None,
        deadline_iso=iso,
        source_type=source_type,
        source_name=source_type,
        published_at=NOW,
        message_id=candidate_id,
    )


# ---------------------------------------------------------------- 客户端工具
def test_parse_json_handles_fenced_and_wrapped_output():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure, here it is: [{"x": 2}] done') == [{"x": 2}]
    assert parse_json("not json") is None


def test_usage_tracker_since_reports_delta():
    tracker = UsageTracker()
    tracker.record("t", 0.5, 100, 20)
    before = tracker.snapshot()
    tracker.record("t", 0.25, 50, 10)
    delta = tracker.since(before)
    assert delta["llm_calls"] == 1
    assert delta["tokens_estimate"] == 60
    assert tracker.snapshot()["cost_usd"] > 0


# ---------------------------------------------------------------- 规划
def test_llm_planner_selects_sources_and_window():
    class Source:
        def __init__(self, name, source_type):
            self.name = name
            self.source_type = source_type

    sources = [Source("outlook", "official_email"), Source("course-chat", "student_chat")]
    client = FakeClient([{
        "sources": ["course-chat"],
        "course_filters": ["COMP7608"],
        "keywords": ["HW2"],
        "date_from": "2026-11-23T00:00:00+08:00",
        "date_to": "2026-11-30T00:00:00+08:00",
        "allow_supplementary": False,
        "reason": "用户只关心群聊里的作业",
    }])
    plan = LLMQueryPlanner(client).plan("COMP7608 下周的 HW2", sources, NOW)
    assert [source.name for source in plan.sources] == ["course-chat"]
    assert plan.course_filters == ["COMP7608"]
    assert plan.allow_supplementary is False
    assert plan.reason.startswith("用户只关心")


def test_llm_planner_falls_back_on_bad_output():
    client = FakeClient([None])
    plan = LLMQueryPlanner(client).plan("COMP7608 的作业", [], NOW)
    assert plan.course_filters == ["COMP7608"]


# ---------------------------------------------------------------- 抽取
def test_llm_extractor_normalizes_and_flags_review():
    client = FakeClient([[
        {"task_name": "HW2", "course": "COMP7608", "deadline_text": "Dec 8 23:59",
         "deadline_iso": ISO_A, "confidence": 0.9, "needs_review": False},
        {"task_name": "Assignment 3", "course": "COMP7608", "deadline_text": "月底前",
         "deadline_iso": None, "confidence": 0.3, "needs_review": True},
    ]])
    candidates = LLMDeadlineExtractor(client).extract([_message("...")], now=NOW)
    assert len(candidates) == 2
    assert candidates[0].deadline_iso == ISO_A
    assert candidates[0].needs_review is False
    assert candidates[1].deadline_iso is None
    assert candidates[1].needs_review is True
    assert candidates[1].unresolved_time is True


def test_llm_extractor_falls_back_on_broken_json():
    client = FakeClient(["not-a-list"])
    candidates = LLMDeadlineExtractor(client).extract(
        [_message("Assignment 2 for COMP7608 is due on 2026-12-08 23:59.")], now=NOW
    )
    assert len(candidates) == 1
    assert candidates[0].deadline_iso == ISO_A


# ---------------------------------------------------------------- 裁决
def test_llm_arbiter_selects_candidate():
    cluster = TaskCluster("c1", "Assignment 2", "COMP7608", [
        _candidate(ISO_A, "official_email", "e1"),
        _candidate(ISO_B, "student_chat", "s1"),
    ])
    client = FakeClient([{"decision": ISO_A, "confidence": 0.9, "reason": "官方邮件"}])
    arbitration = LLMConflictArbiter(client).arbitrate(cluster, cluster.candidates)
    assert arbitration.deadline_iso == ISO_A
    assert arbitration.confidence == 0.9


def test_llm_arbiter_low_confidence_becomes_uncertain():
    cluster = TaskCluster("c1", "Assignment 2", "COMP7608", [
        _candidate(ISO_A, "student_chat", "s1"),
        _candidate(ISO_B, "student_chat", "s2"),
    ])
    client = FakeClient([{"decision": ISO_A, "confidence": 0.2, "reason": "不确定"}])
    arbitration = LLMConflictArbiter(client).arbitrate(cluster, cluster.candidates)
    assert arbitration.deadline_iso is None

    outcome = ConflictResolver(arbiter=LLMConflictArbiter(FakeClient([
        {"decision": ISO_A, "confidence": 0.2, "reason": "不确定"},
        {"decision": ISO_B, "confidence": 0.2, "reason": "仍不确定"},
        {"decision": "uncertain", "confidence": 0.1, "reason": "证据不足"},
    ]))).resolve([cluster], plan=None, retrieval=lambda *_: [])
    assert outcome.records[0].status == "uncertain"


def test_llm_arbiter_rejects_invented_deadline():
    cluster = TaskCluster("c1", "Assignment 2", "COMP7608", [
        _candidate(ISO_A, "official_email", "e1"),
        _candidate(ISO_B, "student_chat", "s1"),
    ])
    client = FakeClient([{"decision": "2026-12-20T23:59:00+08:00",
                          "confidence": 0.95, "reason": "invented"}])
    arbitration = LLMConflictArbiter(client).arbitrate(cluster, cluster.candidates)
    assert arbitration.deadline_iso is None


# ---------------------------------------------------------------- 真实调用（可选）
@pytest.mark.skipif(os.getenv("RUN_LLM_TESTS") != "1",
                    reason="需要联网并消耗额度，设置 RUN_LLM_TESTS=1 才运行")
def test_real_deepseek_call():
    client = LLMClient.from_config()
    if client is None:
        pytest.skip("未配置 DeepSeek key")
    content = client.complete("只回复两个字母：OK", kind="smoke")
    assert content
    assert client.tracker.snapshot()["llm_calls"] == 1
