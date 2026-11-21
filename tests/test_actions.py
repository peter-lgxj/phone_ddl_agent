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
    obj.read_only = False
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


def test_swipe_accepts_model_x1_y1_fields(agent):
    agent._execute({"action_type": "swipe", "x1": 499, "y1": 100,
                    "x2": 499, "y2": 744})
    assert agent.phone.calls == [("swipe", (499, 100, 499, 744), {})]


def test_tap_parses_string_coordinates(agent):
    agent._execute({"action_type": "tap", "x": "135", "y": 720})
    assert agent.phone.calls == [("tap", (135, 720), {})]


def test_type_with_text_calls_controller(agent):
    agent._execute({"action_type": "type", "text": "测试消息123"})
    assert agent.phone.calls == [
        ("type_text", ("测试消息123",), {"allow_search": False})]


def test_unknown_action_returns_false(agent):
    assert agent._execute({"action_type": "teleport"}) is False


@pytest.mark.parametrize("action_type", ["send", "press_enter", "type"])
def test_read_only_blocks_mutating_actions(action_type):
    obj = object.__new__(screen_agent.ScreenAgent)
    obj.phone = DummyPhone()
    obj.read_only = True
    action = {"action_type": action_type, "text": "不要发送"}

    assert obj._execute(action) is False
    assert action["blocked"] is True
    assert "只读" in action["blocked_reason"]
    # type 会先 dump 判断是否搜索框，但绝不能真正输入或发送
    assert not any(call[0] == "type_text" for call in obj.phone.calls)
