"""一步多动作：一次响应按顺序执行多个连续动作，归档仍保持 step 语义。"""

from __future__ import annotations

import json

from PIL import Image

import config
import screen_agent
import ui_state
from memory import TaskMemory


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]
        self.usage = None


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        return FakeResponse(self.content)


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


class StubPhone:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def screenshot(self, save_path="screen.png"):
        Image.new("RGB", (1080, 2374), (20, 20, 20)).save(save_path)

    def swipe(self, x1, y1, x2, y2):
        self.calls.append(("swipe", x1, y1, x2, y2))

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def press_back(self):
        self.calls.append(("back",))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_enter(self):
        self.calls.append(("press_enter",))


class StubOCR:
    def __init__(self, tree_elements=None, ocr_elements=None):
        self.tree_elements = tree_elements or []
        self.ocr_elements = ocr_elements or []
        self.tree_calls = 0
        self.ocr_calls = 0

    def detect_tree(self):
        self.tree_calls += 1
        return self.tree_elements

    def detect_ocr(self, _path):
        self.ocr_calls += 1
        return self.ocr_elements


ELEMENTS = [
    {"text": "确认", "center": (540, 1440), "bbox": (440, 1390, 640, 1490), "source": "ocr"},
]


def _agent(tmp_path, monkeypatch, content, read_only=False):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OCR_MODE", "on_demand")
    monkeypatch.setattr(config, "OCR_SNAP_MODE", "tree_first")
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "RULE_FASTPATH", False)
    (tmp_path / "screen_current.png").write_bytes(b"")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = FakeClient(content)
    agent.model = "fake"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = StubPhone(str(tmp_path / "screen_current.png"))
    agent.ocr = StubOCR()
    agent.read_only = read_only
    agent.run_dir = None
    agent.driver_kind = "uiautomator2"
    agent._observe_valid = False
    agent._last_image_meta = None
    agent._last_model_action = None
    agent._last_decision_timings = {}
    agent._last_step_timings = {}
    agent._last_observe_ocr_seconds = 0.0
    agent._last_rule_seconds = 0.0
    agent._last_observation_scope = "pre_action"
    agent._rule_state = {}
    agent._rule_hits = []
    agent.usage_events = []
    agent._last_step_usage = []
    return agent


def test_merged_response_returns_action_sequence(tmp_path, monkeypatch):
    content = json.dumps({
        "observation": "列表还没到底",
        "thought": "连续向上翻两次",
        "actions": [
            {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700,
             "description": "上翻一次"},
            {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700,
             "description": "再上翻一次"},
        ],
        "task_done": False,
    }, ensure_ascii=False)
    agent = _agent(tmp_path, monkeypatch, content)

    action = agent._think_and_act(TaskMemory("连续滚动两次"))

    assert action["action_type"] == "sequence"
    assert [item["action_type"] for item in action["actions"]] == ["swipe", "swipe"]
    assert agent._last_decision_timings["sequence_len"] == 2
    assert agent.client.chat.completions.calls == 1


def test_sequence_is_truncated_by_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIONS_PER_STEP", 2)
    content = json.dumps({
        "observation": "连续动作",
        "actions": [
            {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
            {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
            {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
        ],
    }, ensure_ascii=False)
    agent = _agent(tmp_path, monkeypatch, content)

    action = agent._think_and_act(TaskMemory("滚动三次"))
    assert len(action["actions"]) == 2


def test_sequence_shares_one_tree_and_ocr_pass(tmp_path, monkeypatch):
    content = json.dumps({
        "observation": "点两次确认",
        "actions": [
            {"action_type": "tap", "x": 520, "y": 1470, "target": "确认"},
            {"action_type": "tap", "x": 520, "y": 1470, "target": "确认"},
        ],
    }, ensure_ascii=False)
    agent = _agent(tmp_path, monkeypatch, content)
    agent.ocr = StubOCR(tree_elements=ELEMENTS, ocr_elements=ELEMENTS)

    action = agent._think_and_act(TaskMemory("点确认两次"))

    assert agent.ocr.tree_calls == 1
    assert agent.ocr.ocr_calls == 1
    assert [(item["x"], item["y"]) for item in action["actions"]] == [(540, 1440)] * 2


def test_sequence_last_action_controls_observe_ocr(tmp_path, monkeypatch):
    content = json.dumps({"observation": "继续", "action_type": "wait",
                          "description": "等待"}, ensure_ascii=False)
    agent = _agent(tmp_path, monkeypatch, content)
    memory = TaskMemory("继续滚动")
    memory.add_step(thought="", action={
        "action_type": "sequence",
        "actions": [{"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3}],
    }, observation="上一屏")

    agent._think_and_act(memory, merged=True)

    assert agent.ocr.ocr_calls == 1


def test_execute_sequence_runs_in_order(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, "{}")
    actions = [
        {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700},
        {"action_type": "tap", "x": 100, "y": 200},
    ]

    assert agent._execute_sequence(actions) is False
    assert agent.phone.calls == [
        ("swipe", 540, 1700, 540, 700),
        ("tap", 100, 200),
    ]


def test_sequence_stops_after_read_only_block(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, "{}", read_only=True)
    actions = [
        {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700},
        {"action_type": "type", "text": "不要发送"},
        {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
    ]

    assert agent._execute_sequence(actions) is False
    assert agent.phone.calls == [("swipe", 540, 1700, 540, 700)]
    assert actions[1]["blocked"] is True


def test_sequence_finish_stops_later_actions(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, "{}")
    actions = [
        {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
        {"action_type": "finish", "summary": "完成"},
        {"action_type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 3},
    ]

    assert agent._execute_sequence(actions) is True
    assert len(agent.phone.calls) == 1


def test_step_archive_keeps_one_step_for_sequence(tmp_path, monkeypatch):
    content = json.dumps({
        "observation": "连续滚动",
        "actions": [
            {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700,
             "description": "翻一次"},
            {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700,
             "description": "再翻一次"},
        ],
        "task_done": False,
    }, ensure_ascii=False)
    agent = _agent(tmp_path, monkeypatch, content)
    agent.run_dir = tmp_path / "run"
    agent.run_dir.mkdir()
    ui_state.init_state("多动作")
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.0)

    agent._execute_step(1, TaskMemory("连续滚动两次"))

    files = sorted(agent.run_dir.glob("step_*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["action"]["action_type"] == "sequence"
    assert len(record["action"]["actions"]) == 2
    assert record["timings"]["actions"] == 2
