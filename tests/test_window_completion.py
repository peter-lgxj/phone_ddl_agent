"""时间窗完成条件：必须翻到窗口下界，不能看到一条 DDL 就收工。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import screen_agent
from memory import TaskMemory

TASK = "打开QQ，进入群聊后向上翻阅最近 14 天的消息，记录所有截止时间"


def _agent():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._observe_valid = True
    return agent


def _memory(steps=None, task: str = TASK) -> TaskMemory:
    memory = TaskMemory(task)
    memory.steps.extend(steps or [])
    return memory


def _step(observation: str, messages=None, action_type: str = "swipe") -> dict:
    return {
        "action_type": action_type,
        "observation": observation,
        "observation_messages": messages or [],
    }


def test_guard_rejects_early_finish_and_keeps_read_steps():
    steps = [_step("【操作系统作业一】截止时间已更正", action_type="observe")]
    memory = _memory(steps)
    agent = _agent()

    action = agent._guard_window_completion({"action_type": "finish", "summary": "完成"}, memory)

    assert action["action_type"] == "swipe"
    assert action["window_guard"] is True
    assert action["y"] < action["y2"]  # 手指下移 = 向上翻阅更早消息
    assert memory.steps == steps  # 已读记录不因拒绝收工而丢失


def test_guard_allows_finish_after_message_older_than_window():
    old = (datetime.now() - timedelta(days=20)).strftime("%m月%d日 10:00")
    memory = _memory([_step("继续向上", [{"time": old, "body": "更早的消息"}])])

    action = _agent()._guard_window_completion({"action_type": "finish"}, memory)

    assert action["action_type"] == "finish"


def test_guard_allows_finish_at_chat_top():
    memory = _memory([_step("已到聊天顶部，没有更早消息")])

    action = _agent()._guard_window_completion({"action_type": "finish"}, memory)

    assert action["action_type"] == "finish"


def test_guard_uses_scroll_stall_as_top_evidence():
    memory = _memory([_step("同一屏"), _step("同一屏")])

    assert _agent()._window_pending(memory) is False


def test_scroll_stall_uses_structured_messages_not_status_bar_clock():
    """观察正文每步都带不同的状态栏时间，但消息完全没变 → 仍算到顶。"""
    messages = [{"time": "星期五 23:45", "body": "你邀请如风加入了群聊"}]
    first = _step("[OCR] 01:15 46% 同一屏", messages)
    second = _step("[OCR] 01:16 45% 同一屏", messages)

    assert _agent()._window_pending(_memory([first, second])) is False


def test_scroll_stall_requires_messages_to_stay_identical():
    first = _step("新消息 A", [{"time": "11月20日", "body": "消息 A"}])
    second = _step("新消息 B", [{"time": "11月19日", "body": "消息 B"}])

    assert _agent()._window_pending(_memory([first, second])) is True


def test_no_lookback_task_is_not_guarded():
    memory = _memory([_step("读完了")], task="打开QQ，找到目标群并读出消息")

    action = _agent()._guard_window_completion({"action_type": "finish"}, memory)

    assert action["action_type"] == "finish"


def test_future_deadline_in_text_is_not_window_floor():
    future = (datetime.now() + timedelta(days=20)).strftime("%m月%d日")
    memory = _memory([_step(f"截止时间：{future}")])

    assert _agent()._window_pending(memory) is True


def test_check_task_done_returns_false_without_window_floor():
    memory = _memory([_step("看到一条 DDL", action_type="observe")])
    agent = _agent()

    assert agent._check_task_done(memory) is False


def test_parse_observed_date_formats():
    now = datetime(2026, 11, 24, 12, 0)

    assert screen_agent.ScreenAgent._parse_observed_date("10月20日 10:00", now) == date(2026, 10, 20)
    assert screen_agent.ScreenAgent._parse_observed_date("2026-10-20 10:00", now) == date(2026, 10, 20)
    assert screen_agent.ScreenAgent._parse_observed_date("昨天 12:00", now) == date(2026, 11, 23)
    assert screen_agent.ScreenAgent._parse_observed_date("星期一 09:00", now) == date(2026, 11, 23)
    assert screen_agent.ScreenAgent._parse_observed_date("未显示", now) is None
