"""手机读取链路：滚动观察文本、OCR 合并、timeout 部分结果。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import screen_agent
from ddl_agent.connectors.base import SourceError
from ddl_agent.connectors.mobile_chat import MobileChatConnector
from ddl_agent.connectors.mobile_reading import parse_time_token, split_observation
from ddl_agent.models import RawMessage, RetrievalPlan
from ddl_agent.monitor.config import MonitorSource
from ddl_agent.monitor.cursor import CursorStore
from ddl_agent.monitor.sources import MobileAppSource


class FakeAgent:
    def __init__(self, run_dir, observations: list[str], status: str = "done"):
        self.last_status = status
        self.last_memory = SimpleNamespace(
            steps=[
                {"step": index, "observation": observation}
                for index, observation in enumerate(observations, start=1)
            ]
        )
        self.run_dir = str(run_dir)
        self.tasks: list[str] = []

    def run(self, task: str):
        self.tasks.append(task)


def _plan() -> RetrievalPlan:
    return RetrievalPlan(query="最近消息里有没有 DDL")


def test_timeout_returns_partial_messages(tmp_path):
    for index in (1, 2):
        (tmp_path / f"step_{index:03d}.json").write_text("{}", encoding="utf-8")
    agent = FakeAgent(tmp_path, ["消息一：截止 11月20日", "消息二：提交时间 12月1日"], "timeout")
    connector = MobileChatConnector("wechat-self", "文件传输助手", agent=agent,
                                    use_device_lock=False)

    messages = connector.fetch(_plan())

    assert len(messages) == 2
    assert connector.partial is True
    assert connector.last_status == "timeout"
    assert connector.last_fetch_meta == {
        "partial": True, "last_status": "timeout", "message_count": 2,
    }
    assert messages[0].body.startswith("消息一")


def test_done_returns_messages_without_partial(tmp_path):
    (tmp_path / "step_001.json").write_text("{}", encoding="utf-8")
    agent = FakeAgent(tmp_path, ["消息一：截止 11月20日"], "done")
    connector = MobileChatConnector("wechat-self", "文件传输助手", agent=agent,
                                    use_device_lock=False)

    messages = connector.fetch(_plan())

    assert len(messages) == 1
    assert connector.partial is False
    assert connector.last_status == "done"


def test_failed_still_raises(tmp_path):
    agent = FakeAgent(tmp_path, ["消息一"], "failed")
    connector = MobileChatConnector("wechat-self", "文件传输助手", agent=agent,
                                    use_device_lock=False)

    with pytest.raises(SourceError):
        connector.fetch(_plan())
    assert connector.last_status == "failed"


def test_swipe_observe_prompt_requires_full_message_list():
    question = screen_agent.build_observe_question(
        {"action_type": "swipe", "description": "向上滑动查看更早消息"},
        SimpleNamespace(steps=[]),
    )

    assert "逐条" in question
    assert "发送者" in question
    assert "消息原文" in question
    assert "忽略状态栏" in question


def test_format_ocr_observation_orders_and_dedupes():
    elements = [
        {"text": "第二条", "bbox": (10, 300, 200, 320)},
        {"text": "第一条", "bbox": (10, 100, 200, 120)},
        {"text": "第一条", "bbox": (10, 101, 200, 121)},
        {"text": "   ", "bbox": (0, 0, 0, 0)},
    ]

    assert screen_agent.format_ocr_observation(elements) == "第一条\n第二条"


class _FakeClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )))


class _FakePhone:
    def screenshot(self, _path: str):
        return None


class _FakeOcr:
    def __init__(self):
        self.calls = 0

    def detect_ocr(self, _path: str):
        self.calls += 1
        return [
            {"text": "模型概括之外的消息正文", "bbox": (10, 200, 300, 220)},
            {"text": "发送者：TA 11:59", "bbox": (10, 100, 300, 120)},
        ]


def _agent_for_observe(monkeypatch, ocr: _FakeOcr):
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.phone = _FakePhone()
    agent.client = _FakeClient("这是微信聊天界面")
    agent.model = "fake-model"
    agent.extra_body = None
    agent.ocr = ocr
    agent._clear_observe_shot = lambda: None
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        screen_agent.image_preprocess,
        "prepare_image_bytes",
        lambda *_: (b"image", {"mime": "image/jpeg"}),
    )
    return agent


def test_observe_appends_ocr_text_for_scroll(monkeypatch):
    ocr = _FakeOcr()
    agent = _agent_for_observe(monkeypatch, ocr)
    monkeypatch.setattr(screen_agent.config, "OBSERVE_OCR_ENABLED", True)
    monkeypatch.setattr(screen_agent.config, "OBSERVE_OCR_ACTIONS", {"swipe", "scroll"})

    observation = agent._observe({"action_type": "swipe", "description": "上滑"},
                                 SimpleNamespace(steps=[]))

    assert observation.startswith("这是微信聊天界面")
    assert "[OCR]" in observation
    assert "模型概括之外的消息正文" in observation
    assert ocr.calls == 1


def test_observe_keeps_model_only_when_ocr_disabled(monkeypatch):
    ocr = _FakeOcr()
    agent = _agent_for_observe(monkeypatch, ocr)
    monkeypatch.setattr(screen_agent.config, "OBSERVE_OCR_ENABLED", False)

    observation = agent._observe({"action_type": "swipe", "description": "上滑"},
                                 SimpleNamespace(steps=[]))

    assert observation == "这是微信聊天界面"
    assert ocr.calls == 0


def _monitor_source() -> MonitorSource:
    return MonitorSource(
        id="wechat-course", app="wechat", kind="group",
        target="COMP7608 课程群", source_type="instructor_chat",
    )


def test_monitor_source_timeout_returns_partial_messages(tmp_path):
    (tmp_path / "step_001.json").write_text("{}", encoding="utf-8")
    agent = FakeAgent(
        tmp_path,
        ["发送者：TA Chen 时间：2026-11-18 13:39 原文：HW1 截止 11月20日 23:59"],
        "timeout",
    )
    source = MobileAppSource(_monitor_source(), agent=agent, use_device_lock=False)

    messages = source.fetch()

    assert len(messages) == 1
    assert source.partial is True
    assert source.last_status == "timeout"
    assert messages[0].sender == "TA Chen"
    assert messages[0].published_at.isoformat() == "2026-11-18T13:39:00+08:00"
    assert messages[0].needs_review is False


def test_monitor_and_connector_parse_the_same_observation(tmp_path):
    observation = "发送者：TA Chen 时间：2026-11-18 13:39 原文：HW1 截止 11月20日 23:59"
    agent = FakeAgent(tmp_path, [observation], "done")
    source = MobileAppSource(_monitor_source(), agent=agent, use_device_lock=False)
    monitor_messages = source.fetch()

    connector = MobileChatConnector("wechat-course", "课程群", agent=agent,
                                    use_device_lock=False)
    connector_messages = connector.fetch(RetrievalPlan(query="ddl"))

    left, right = monitor_messages[0], connector_messages[0]
    assert (left.sender, left.body, left.published_at) == (
        right.sender, right.body, right.published_at
    )
    assert left.needs_review is False and right.needs_review is False


def test_unparseable_time_is_blank_and_flagged():
    parsed = split_observation(
        "发送者：某人 时间：13:39 原文：作业截止 11月20日",
        now=SimpleNamespace(year=2026),
    )
    assert len(parsed) == 1
    assert parsed[0].sender == "某人"
    assert parsed[0].published_at is None
    assert parsed[0].needs_review is True
    assert "日期" in parsed[0].review_reason


def test_time_parser_rejects_invalid_date():
    parsed, reason = parse_time_token("2026-11-31 23:59")
    assert parsed is None
    assert "无法解析" in reason


def test_cursor_does_not_advance_when_all_messages_have_no_time(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    message = RawMessage(
        message_id="m1", source_name="群", source_type="student_chat",
        sender="", published_at=None, subject="", body="作业截止 11月20日",
        needs_review=True,
    )

    cursor = store.advance("wechat-course", [message])

    assert cursor.last_timestamp is None
    assert cursor.last_message_id is None

