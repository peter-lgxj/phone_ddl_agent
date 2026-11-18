"""按需 OCR：触发条件、坐标吸附、以及 _think_and_act 的两条分支。"""
import json

from PIL import Image

import config
import ocr_locator
import screen_agent
from memory import TaskMemory


# ── 触发条件 ──────────────────────────────────────────────

def test_trigger_only_for_tap_in_on_demand():
    assert ocr_locator.should_trigger_ocr("on_demand", "tap") is True
    for action_type in ("open_app", "back", "home", "wait", "swipe", "type", "send", "finish"):
        assert ocr_locator.should_trigger_ocr("on_demand", action_type) is False


def test_always_and_off_modes_do_not_use_on_demand_trigger():
    assert ocr_locator.should_trigger_ocr("always", "tap") is False
    assert ocr_locator.should_trigger_ocr("off", "tap") is False


# ── 吸附 ──────────────────────────────────────────────────

ELEMENTS = [
    {"text": "确认", "center": (540, 1440), "bbox": (440, 1390, 640, 1490), "source": "ocr"},
    {"text": "取消", "center": (200, 1440), "bbox": (100, 1390, 300, 1490), "source": "xml"},
]


def test_point_inside_bbox_has_zero_distance():
    found = ocr_locator.nearest_element((500, 1440), ELEMENTS, 80)
    assert found is not None and found[1] == 0.0


def test_nearest_within_distance_is_snapped():
    snapped, moves = ocr_locator.snap_action_coords(
        {"action_type": "tap", "x": 520, "y": 1470}, ELEMENTS, max_distance=80)
    assert (snapped["x"], snapped["y"]) == (540, 1440)
    assert moves[0]["text"] == "确认" and moves[0]["source"] == "ocr"


def test_far_point_is_not_snapped():
    action = {"action_type": "tap", "x": 900, "y": 300}
    snapped, moves = ocr_locator.snap_action_coords(action, ELEMENTS, max_distance=80)
    assert snapped is action and moves == []


def test_non_coordinate_action_is_untouched():
    action = {"action_type": "open_app", "app_name": "设置"}
    assert ocr_locator.snap_action_coords(action, ELEMENTS, 80) == (action, [])


def test_tap_without_coords_is_untouched():
    action = {"action_type": "tap", "description": "点按钮"}
    assert ocr_locator.snap_action_coords(action, ELEMENTS, 80) == (action, [])


# ── 按 text / resource-id / content-desc 匹配 ─────────────

def test_element_matches_text_resource_id_and_content_desc():
    el = {"text": "确认支付", "resource_id": "com.app:id/pay_btn", "content_desc": "支付按钮"}
    assert ocr_locator.element_matches_target(el, "确认") is True
    assert ocr_locator.element_matches_target(el, "pay_btn") is True
    assert ocr_locator.element_matches_target(el, "支付按钮") is True
    assert ocr_locator.element_matches_target(el, "取消") is False


def test_target_prefers_matching_element_over_closer_one():
    elements = [
        {"text": "取消", "center": (540, 1445), "bbox": (440, 1395, 640, 1495), "source": "xml"},
        {"text": "确认支付", "center": (540, 1500), "bbox": (440, 1450, 640, 1550), "source": "xml"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(
        {"action_type": "tap", "x": 540, "y": 1448, "target": "确认"}, elements, 120)

    assert moves[0]["text"] == "确认支付" and moves[0]["matched_target"] is True
    assert snapped["y"] == 1500


def test_target_without_match_falls_back_to_nearest():
    elements = [
        {"text": "取消", "center": (540, 1445), "bbox": (440, 1395, 640, 1495), "source": "xml"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(
        {"action_type": "tap", "x": 520, "y": 1450, "target": "不存在"}, elements, 80)

    assert moves[0]["text"] == "取消" and moves[0]["matched_target"] is False
    assert (snapped["x"], snapped["y"]) == (540, 1445)


# ── _think_and_act 分支 ───────────────────────────────────

class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(self.content)


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


class StubPhone:
    def __init__(self, path):
        self.path = path

    def screenshot(self, save_path="screen.png"):
        Image.new("RGB", (1080, 2374), (30, 30, 30)).save(self.path)


class StubOCR:
    def __init__(self, tree_elements=None, ocr_elements=None):
        self.tree_elements = tree_elements if tree_elements is not None else []
        self.ocr_elements = ocr_elements if ocr_elements is not None else []
        self.tree_calls = 0
        self.ocr_calls = 0
        self.format_calls = 0

    def detect_tree(self):
        self.tree_calls += 1
        return self.tree_elements

    def detect_ocr(self, path):
        self.ocr_calls += 1
        return self.ocr_elements

    def format_for_prompt(self, path):
        self.format_calls += 1
        return "确认(540,1440)"


def make_agent(tmp_path, monkeypatch, action, tree_elements=None, ocr_elements=None,
               snap_mode="tree_first"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OCR_MODE", "on_demand")
    monkeypatch.setattr(config, "OCR_SNAP_MODE", snap_mode)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    (tmp_path / "screen_current.png").write_bytes(b"")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = FakeClient(json.dumps(action, ensure_ascii=False))
    agent.model = "fake-model"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = StubPhone(str(tmp_path / "screen_current.png"))
    agent.ocr = StubOCR(tree_elements, ocr_elements)
    agent.run_dir = None
    agent._observe_valid = False
    agent._last_image_meta = None
    agent._last_model_action = None
    agent._last_decision_timings = {}
    agent._last_step_timings = {}
    return agent


def test_tap_hits_tree_first(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=ELEMENTS)
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert (action["x"], action["y"]) == (540, 1440)
    assert agent.ocr.tree_calls == 1 and agent.ocr.ocr_calls == 0
    timings = agent._last_decision_timings
    assert timings["snap_source"] == "tree" and timings["ocr_triggered"] is True


def test_tree_miss_falls_back_to_ocr(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=[],
                       ocr_elements=ELEMENTS)
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert (action["x"], action["y"]) == (540, 1440)
    assert agent.ocr.tree_calls == 1 and agent.ocr.ocr_calls == 1
    assert agent._last_decision_timings["snap_source"] == "ocr"


def test_both_miss_keeps_model_coords(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 900, "y": 300, "description": "点空白"},
                       tree_elements=[], ocr_elements=[])
    action = agent._think_and_act(TaskMemory("点击空白"))

    assert (action["x"], action["y"]) == (900, 300)
    timings = agent._last_decision_timings
    assert timings["snap_source"] == "none" and timings["ocr_snap"]["moves"] == []


def test_ocr_only_mode_skips_tree(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=ELEMENTS,
                       ocr_elements=ELEMENTS,
                       snap_mode="ocr_only")
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert agent.ocr.tree_calls == 0 and agent.ocr.ocr_calls == 1
    assert agent._last_decision_timings["snap_source"] == "ocr"
    assert (action["x"], action["y"]) == (540, 1440)


def test_tree_only_mode_skips_ocr(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=[], ocr_elements=ELEMENTS,
                       snap_mode="tree_only")
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert agent.ocr.tree_calls == 1 and agent.ocr.ocr_calls == 0
    assert agent._last_decision_timings["snap_source"] == "none"
    assert (action["x"], action["y"]) == (520, 1470)


def test_open_app_skips_ocr(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "open_app", "app_name": "设置", "description": "打开设置"},
                       tree_elements=ELEMENTS, ocr_elements=ELEMENTS)
    action = agent._think_and_act(TaskMemory("打开设置"))

    assert action["app_name"] == "设置"
    assert agent.ocr.tree_calls == 0 and agent.ocr.ocr_calls == 0
    assert agent.ocr.format_calls == 0
    timings = agent._last_decision_timings
    assert timings["ocr_triggered"] is False and timings["ocr"] == 0.0
