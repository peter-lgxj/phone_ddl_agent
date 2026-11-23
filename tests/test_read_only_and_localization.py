"""只读前置拒绝、无控件树降级、无证据不点坐标的回归测试（不连手机）。"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import input_detector
import screen_agent
from adb_controller import AdbDumpController
from memory import TaskMemory


# ---------------------------------------------------------------- 只读前置拒绝
def test_read_only_refuses_send_task_before_touching_phone(monkeypatch):
    monkeypatch.setattr(screen_agent.ui_state, "init_state", lambda task: None)
    monkeypatch.setattr(screen_agent.ui_state, "set_status", lambda *args: None)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent.last_status = None

    with pytest.raises(screen_agent.ReadOnlyTaskRefused, match="只读模式不支持输入/发送"):
        agent.run("在微信文件传输助手中发你好")
    assert agent.last_status == "refused"


def test_read_only_refusal_names_alternative():
    reason = screen_agent.read_only_task_refusal("在微信文件传输助手中发你好", True)

    assert reason
    assert "只读模式不支持输入/发送" in reason
    assert "最近14天" in reason


def test_read_only_allows_search_box_typing():
    task = "打开微信，通过搜索框输入「课程群」找到并进入该群聊；不要修改任何内容"

    assert screen_agent.read_only_task_refusal(task, True) is None


def test_read_only_allows_negated_input_clause():
    task = (
        "打开QQ，在消息列表里滚动查找「COMP7503 作业通知群」并进入；"
        "不要使用搜索框、不要输入中文。列表里没有就继续滚动。"
        "进入后向上翻阅最近 14 天的消息。"
    )

    assert screen_agent.read_only_task_refusal(task, True) is None


def test_read_only_allows_pure_read_task():
    task = "读取当前屏幕上的聊天列表内容（只读，不输入、不发送、不点开会话）"

    assert screen_agent.read_only_task_refusal(task, True) is None


def test_refusal_disabled_when_not_read_only():
    assert screen_agent.read_only_task_refusal("发送日报", False) is None


# ---------------------------------------------------------------- u2 驱动层也只读
def test_u2_driver_blocks_send_actions_in_read_only():
    from phone_controller import PhoneController

    controller = object.__new__(PhoneController)
    controller.read_only = True
    controller.device = SimpleNamespace(
        press=lambda key: pytest.fail("只读模式不应按键"),
        __call__=lambda *args, **kwargs: pytest.fail("只读模式不应查找控件"),
    )

    for action in ("send", "press_enter", "tap_send_button", "test_send"):
        with pytest.raises(PermissionError, match="只读模式拒绝动作"):
            getattr(controller, action)()


# ---------------------------------------------------------------- 无树时输入框降级
class _Controller(AdbDumpController):
    def __init__(self):
        super().__init__(probe=False, read_only=False)
        self.taps: list[tuple[int, int]] = []

    def _run(self, *args, timeout: float = 30.0):
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    def tap(self, x, y):
        self.taps.append((x, y))

    def screenshot(self, path):
        return path


def test_tap_input_box_degrades_to_yolo_when_tree_unavailable(monkeypatch):
    controller = _Controller()
    controller.tree_unsupported = True

    class FakeDetector:
        def find_best(self, path):
            return {"center": (500, 1800), "confidence": 0.88}

    monkeypatch.setattr(input_detector, "InputDetector", FakeDetector)

    assert controller.tap_input_box() is True
    assert controller.taps == [(500, 1800)]


def test_tap_input_box_returns_false_without_tree_or_yolo(monkeypatch):
    controller = _Controller()
    controller.tree_unsupported = True

    class FakeDetector:
        def find_best(self, path):
            return None

    monkeypatch.setattr(input_detector, "InputDetector", FakeDetector)

    assert controller.tap_input_box() is False
    assert controller.taps == []


# ---------------------------------------------------------------- 无证据不点坐标
class _Detector:
    def detect(self, path, conf=0.45):
        return []


def _snap_agent() -> tuple[screen_agent.ScreenAgent, list]:
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.tree_unsupported = True
    agent._last_image_meta = {"orig_size": (1080, 2374)}
    agent._gui_detector = _Detector()
    calls = []

    def forbidden_tree():
        calls.append("tree")
        raise AssertionError("tree_unsupported 时不应读取控件树")

    agent.ocr = SimpleNamespace(detect_tree=forbidden_tree, detect_ocr=lambda path: [])
    return agent, calls


def _tap(target: str = "搜索框") -> dict:
    return {
        "action_type": "tap",
        "x": 890,
        "y": 150,
        "target": target,
        "description": "点击顶部搜索框",
        "thought": "打开搜索",
    }


def test_unresolved_tap_becomes_wait_instead_of_coordinate_click():
    agent, calls = _snap_agent()
    memory = TaskMemory("打开微信并读取聊天")

    action = agent._snap_mapped_action(_tap(), {}, memory)

    assert action["action_type"] == "wait"
    assert action["localization_failed"] is True
    assert "未执行点击" in action["description"]
    assert calls == [], "微信无树时不应再调用 detect_tree"


def test_second_unresolved_tap_fails_with_explicit_message():
    agent, _ = _snap_agent()
    memory = TaskMemory("打开微信并读取聊天")
    first = agent._snap_mapped_action(_tap(), {}, memory)
    memory.steps.append({"step": 1, "action": first})

    with pytest.raises(screen_agent.TargetNotLocated, match="目标未定位，未执行点击"):
        agent._snap_mapped_action(_tap(), {}, memory)


def test_tap_without_target_and_no_evidence_also_waits():
    agent, _ = _snap_agent()
    memory = TaskMemory("打开微信并读取聊天")

    action = agent._snap_mapped_action(_tap(target=""), {}, memory)

    assert action["action_type"] == "wait"
    assert "点击区域" in action["description"]


def test_matched_tap_keeps_source_for_logging():
    agent, _ = _snap_agent()
    memory = TaskMemory("打开微信并读取聊天")

    def fake_ocr(path):
        return [{"text": "搜索", "center": (890, 150),
                 "bbox": (820, 110, 980, 190), "source": "ocr", "type": "text"}]

    agent.ocr = SimpleNamespace(detect_tree=lambda: [], detect_ocr=fake_ocr)
    action = agent._snap_mapped_action(_tap("搜索"), {}, memory)

    assert action["action_type"] == "tap"
    assert action["localization_source"] == "ocr"
