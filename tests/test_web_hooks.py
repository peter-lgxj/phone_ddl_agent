"""网页入口的实时推送包装必须跟随 ScreenAgent 的方法签名（含 merged）。"""

from __future__ import annotations

from memory import TaskMemory

import app as web_app


class _BaseAgent:
    def __init__(self):
        self.calls = []

    def _think_and_act(self, memory, merged: bool = False, **kwargs):
        self.calls.append(("think", merged, kwargs))
        return {"action_type": "wait"}

    def _observe(self, last_action, memory, **kwargs):
        self.calls.append(("observe", kwargs))
        return "观察结果"


class _HookedAgent(web_app._WebAgentHooks, _BaseAgent):
    pass


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        web_app.socketio, "emit", lambda name, payload: events.append((name, payload))
    )
    return events


def test_web_hooks_forward_merged_flag(monkeypatch):
    events = _capture_events(monkeypatch)
    agent = _HookedAgent()

    action = agent._think_and_act(TaskMemory("读群消息"), merged=True)

    assert agent.calls == [("think", True, {})]
    assert action == {"action_type": "wait"}
    assert events[0][0] == "update"
    assert events[0][1]["action"] == action


def test_web_hooks_default_merged_false(monkeypatch):
    _capture_events(monkeypatch)
    agent = _HookedAgent()

    agent._think_and_act(TaskMemory("点击确认"))

    assert agent.calls == [("think", False, {})]


def test_web_hooks_observe_still_emits(monkeypatch):
    events = _capture_events(monkeypatch)
    agent = _HookedAgent()

    observation = agent._observe({"action_type": "tap"}, TaskMemory("点击确认"))

    assert observation == "观察结果"
    assert agent.calls == [("observe", {})]
    assert events[0][0] == "observe"
    assert events[0][1]["observation"] == "观察结果"
