"""动作解析与字段容错测试（无设备、无重依赖）。"""

import pytest

import screen_agent


class DummyPhone:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return recorder


@pytest.fixture
def agent():
    obj = object.__new__(screen_agent.ScreenAgent)
    obj.phone = DummyPhone()
    return obj


def test_type_without_text_is_skipped(agent):
    assert agent._execute({"action_type": "type", "description": "输入"}) is False
    assert agent.phone.calls == []


def test_tap_without_coordinates_is_skipped(agent):
    assert agent._execute({"action_type": "tap", "x": 100}) is False
    assert agent.phone.calls == []


def test_swipe_without_coordinates_is_skipped(agent):
    assert agent._execute({"action_type": "swipe", "x": 1, "y": 2, "x2": 3}) is False
    assert agent.phone.calls == []


def test_tap_parses_string_coordinates(agent):
    agent._execute({"action_type": "tap", "x": "135", "y": 720})
    assert agent.phone.calls == [("tap", (135, 720), {})]


def test_type_with_text_calls_controller(agent):
    agent._execute({"action_type": "type", "text": "测试消息123"})
    assert agent.phone.calls == [("type_text", ("测试消息123",), {})]


def test_unknown_action_returns_false(agent):
    assert agent._execute({"action_type": "teleport"}) is False
