"""按需 OCR：触发条件、坐标吸附、以及 _think_and_act 的两条分支。"""
import json
from types import SimpleNamespace

from PIL import Image

import config
import input_detector
import ocr_locator
import screen_agent
import ui_state
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


def test_exact_target_wins_over_nearer_partial_match():
    elements = [
        {"text": "确认", "center": (510, 1440), "source": "ocr"},
        {"text": "确认支付", "center": (610, 1440), "source": "ocr"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(
        {"action_type": "tap", "x": 510, "y": 1440, "target": "确认支付"},
        elements, 120)
    assert (snapped["x"], snapped["y"]) == (610, 1440)
    assert moves[0]["matched_target"] is True


def test_single_contact_match_ignores_model_coordinate_distance():
    action = {"action_type": "tap", "x": 201, "y": 208, "target": "昊",
              "description": "点击联系人'昊'以进入聊天界面"}
    elements = [
        {"text": "昊", "type": "input", "center": (567, 218),
         "bbox": (205, 154, 1030, 282), "source": "xml"},
        {"text": "昊", "type": "control", "center": (214, 505),
         "bbox": (190, 480, 250, 530), "source": "xml"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(action, elements, 80)
    assert (snapped["x"], snapped["y"]) == (214, 505)
    assert moves[0]["distance"] > 80


def test_nearby_contact_wins_over_same_text_in_search_box():
    action = {"action_type": "tap", "x": 203, "y": 500, "target": "昊",
              "description": "点击联系人'昊'以进入聊天界面"}
    elements = [
        {"text": "昊", "type": "input", "center": (567, 218),
         "bbox": (205, 154, 1030, 282), "source": "xml"},
        {"text": "昊", "type": "control", "center": (214, 505),
         "bbox": (190, 480, 250, 530), "source": "xml"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(action, elements, 80)
    assert (snapped["x"], snapped["y"]) == (214, 505)
    assert moves[0]["text"] == "昊"


def test_multiple_matching_results_use_nearest_model_coordinate():
    elements = [
        {"text": "昊", "type": "control", "center": (214, 505), "source": "xml"},
        {"text": "昊", "type": "control", "center": (220, 900), "source": "xml"},
    ]
    action = {"action_type": "tap", "x": 200, "y": 840,
              "target": "联系人 昊", "description": "点击联系人 昊"}
    snapped, _ = ocr_locator.snap_action_coords(action, elements, 80)
    assert (snapped["x"], snapped["y"]) == (220, 900)


def test_ocr_text_inside_search_input_is_tagged_as_input():
    tree = [{"type": "input", "bbox": (108, 154, 1030, 282)}]
    ocr = [{"text": "昊", "type": "text", "center": (225, 218)},
           {"text": "昊", "type": "text", "center": (214, 505)}]
    tagged = ocr_locator.mark_text_in_input_boxes(ocr, tree)
    assert [element["type"] for element in tagged] == ["input", "text"]
    assert ocr[0]["type"] == "text"


def test_target_without_match_never_snaps_to_unrelated_text():
    elements = [
        {"text": "取消", "center": (540, 1445), "bbox": (440, 1395, 640, 1495), "source": "xml"},
    ]
    snapped, moves = ocr_locator.snap_action_coords(
        {"action_type": "tap", "x": 520, "y": 1450, "target": "不存在"}, elements, 80)

    assert moves == []
    assert (snapped["x"], snapped["y"]) == (520, 1450)


def test_search_icon_target_does_not_snap_to_clock_text():
    action = {"action_type": "tap", "x": 66, "y": 77, "target": "搜索"}
    elements = [{"text": "17:25", "center": (132, 65),
                 "bbox": (40, 20, 150, 90), "source": "ocr"}]
    snapped, moves = ocr_locator.snap_action_coords(action, elements, 80)
    assert snapped is action and moves == []


# ── _think_and_act 分支 ───────────────────────────────────

class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content, usage=None):
        self.choices = [FakeChoice(content)]
        self.usage = usage


class FakeCompletions:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage
        self.calls = 0

    def create(self, **kwargs):
        content = self.content
        if isinstance(content, list):
            content = content[min(self.calls, len(content) - 1)]
        self.calls += 1
        return FakeResponse(content, self.usage)


class FakeClient:
    def __init__(self, content, usage=None):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content, usage)})()


class StubPhone:
    def __init__(self, path):
        self.path = path

    def screenshot(self, save_path="screen.png"):
        Image.new("RGB", (1080, 2374), (30, 30, 30)).save(save_path)


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
               snap_mode="tree_first", usage=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OCR_MODE", "on_demand")
    monkeypatch.setattr(config, "OCR_SNAP_MODE", snap_mode)
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", False)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    (tmp_path / "screen_current.png").write_bytes(b"")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = FakeClient(json.dumps(action, ensure_ascii=False), usage)
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
    agent.read_only = False
    return agent


def test_tap_uses_nearest_combined_candidate(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=ELEMENTS)
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert (action["x"], action["y"]) == (540, 1440)
    assert agent.ocr.tree_calls == 1 and agent.ocr.ocr_calls == 1
    timings = agent._last_decision_timings
    assert timings["snap_source"] == "ocr" and timings["ocr_triggered"] is True


def test_tree_miss_falls_back_to_ocr(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 520, "y": 1470, "description": "点确认"},
                       tree_elements=[],
                       ocr_elements=ELEMENTS)
    action = agent._think_and_act(TaskMemory("点击确认"))

    assert (action["x"], action["y"]) == (540, 1440)
    assert agent.ocr.tree_calls == 1 and agent.ocr.ocr_calls == 1
    assert agent._last_decision_timings["snap_source"] == "ocr"


def test_unmatched_named_target_is_not_clicked(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 66, "y": 77,
                        "target": "搜索", "description": "点击搜索图标"},
                       tree_elements=[],
                       ocr_elements=[{"text": "17:25", "center": (132, 65),
                                      "bbox": (40, 20, 150, 90), "source": "ocr"}])
    action = agent._think_and_act(TaskMemory("搜索聊天"))

    assert action["action_type"] == "wait"
    assert agent._last_decision_timings["target_unresolved"] == "搜索"
    assert agent._last_decision_timings["ocr_snap"]["moves"] == []


def test_search_icon_uses_yolo_when_tree_and_ocr_have_no_label(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 827, "y": 80,
                        "target": "搜索", "description": "点击搜索图标"},
                       tree_elements=[], ocr_elements=[])
    monkeypatch.setattr(config, "SEARCH_ICON_DETECTION", True)

    class StubDetector:
        def detect(self, image_path, conf):
            assert image_path == "screen_current.png" and conf == 0.45
            return [{"text": "搜索", "class_name": "search_icon", "type": "search_icon",
                     "center": (897, 193), "bbox": (856, 153, 939, 233),
                     "confidence": 0.614, "source": "yolo"}]

    agent._gui_detector = StubDetector()
    action = agent._think_and_act(TaskMemory("搜索联系人"))
    assert action["action_type"] == "tap"
    assert (action["x"], action["y"]) == (897, 193)
    assert agent._last_decision_timings["snap_source"] == "yolo"
    assert agent.ocr.ocr_calls == 1
    assert agent.client.chat.completions.calls == 1  # 定位只加本地候选，不增加 LLM 请求


def test_input_detector_does_not_choose_search_icon_as_input():
    detector = object.__new__(input_detector.InputDetector)
    detector.detect = lambda *_args, **_kwargs: [
        {"class_name": "search_icon", "confidence": 0.9, "center": (900, 190)},
        {"class_name": "input_box", "confidence": 0.6, "center": (500, 220)},
    ]
    assert detector.find_best("screen.png")["center"] == (500, 220)


def test_consecutive_unresolved_targets_counts_same_target():
    failed = {"action": {"action_type": "wait", "target": "搜索",
                         "localization_failed": True}}
    assert screen_agent.consecutive_unresolved_targets([failed], "搜索") == 1
    assert screen_agent.consecutive_unresolved_targets([failed, failed], "搜索") == 2
    other = {"action": {"action_type": "wait", "target": "联系人",
                        "localization_failed": True}}
    assert screen_agent.consecutive_unresolved_targets([failed, other], "搜索") == 0
    variant = {"action": {"action_type": "wait", "target": "联系人'昊'",
                          "localization_failed": True}}
    variant2 = {"action": {"action_type": "wait", "target": "联系人 昊",
                           "localization_failed": True}}
    assert screen_agent.consecutive_unresolved_targets([variant, variant2], "昊") == 2


def test_second_unresolved_icon_falls_back_to_model_coords(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 66, "y": 77,
                        "target": "搜索", "description": "点击搜索图标"},
                       tree_elements=[],
                       ocr_elements=[{"text": "17:25", "center": (132, 65),
                                      "bbox": (40, 20, 150, 90), "source": "ocr"}])
    memory = TaskMemory("搜索聊天")
    memory.add_step(thought="", action={"action_type": "wait", "target": "搜索",
                                        "localization_failed": True},
                    observation="未定位")
    action = agent._think_and_act(memory)

    assert action["action_type"] == "tap"
    assert (action["x"], action["y"]) == (66, 77)
    assert action["localization_fallback"] is True
    assert agent._last_decision_timings["localization_fallback"] is True


def test_unresolved_icon_without_coords_waits_instead_of_aborting(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "target": "搜索",
                        "description": "点击搜索图标"},
                       tree_elements=[], ocr_elements=[])
    memory = TaskMemory("搜索聊天")
    memory.add_step(thought="", action={"action_type": "wait", "target": "搜索",
                                        "localization_failed": True},
                    observation="未定位")
    action = agent._think_and_act(memory)

    assert action["action_type"] == "wait"
    assert action["localization_failed"] is True
    assert "localization_fallback" not in action


def test_single_contact_result_snaps_despite_wrong_model_coords(tmp_path, monkeypatch):
    tree = [{"text": "昊", "type": "input", "center": (567, 218),
             "bbox": (108, 154, 1037, 282), "source": "xml"}]
    ocr = [{"text": "昊", "type": "text", "center": (214, 506),
            "bbox": (186, 476, 242, 536), "source": "ocr"}]
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 201, "y": 208,
                        "target": "联系人'昊'", "description": "点击联系人'昊'"},
                       tree_elements=tree, ocr_elements=ocr)
    action = agent._think_and_act(TaskMemory("给昊发消息"))
    assert action["action_type"] == "tap"
    assert (action["x"], action["y"]) == (214, 506)
    assert agent._last_decision_timings["snap_source"] == "ocr"


def test_contact_result_is_selected_even_when_model_points_at_search_box(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "tap", "x": 201, "y": 208,
                        "target": "昊", "description": "点击联系人'昊'以进入聊天界面"},
                       tree_elements=[{"text": "昊", "type": "input",
                                       "center": (567, 218),
                                       "bbox": (205, 154, 1030, 282), "source": "xml"}],
                       ocr_elements=[{"text": "昊", "type": "text",
                                      "center": (225, 218), "source": "ocr"},
                                     {"text": "昊", "type": "text",
                                      "center": (214, 505), "source": "ocr"}])
    action = agent._think_and_act(TaskMemory("给昊发送消息"))
    assert action["action_type"] == "tap"
    assert (action["x"], action["y"]) == (214, 505)
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


def test_default_merged_step_makes_one_llm_call(tmp_path, monkeypatch):
    """默认 MERGED_OBSERVE=1：一次带图请求同时返回 observation 与 action。"""
    agent = make_agent(tmp_path, monkeypatch,
                       {"observation": "页面仍在加载",
                        "action_type": "wait", "description": "等待加载"},
                       tree_elements=[], ocr_elements=[])
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    ui_state.init_state("计数")

    memory = TaskMemory("等待页面")
    agent._execute_step(1, memory)

    assert agent.client.chat.completions.calls == 1
    assert memory.get_last_observation() == "页面仍在加载"


def test_old_two_stage_makes_two_llm_calls(tmp_path, monkeypatch):
    """关闭合并观察后回到旧行为：决策 1 次 + 观察 1 次。"""
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "wait", "description": "等待加载"},
                       tree_elements=[], ocr_elements=[])
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "MERGED_OBSERVE", False)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    ui_state.init_state("计数")

    agent._execute_step(1, TaskMemory("等待页面"))

    assert agent.client.chat.completions.calls == 2


def test_merged_invalid_json_falls_back_to_two_stage(tmp_path, monkeypatch):
    """合并响应不合法时回退两段式，动作只执行一次。"""
    action_json = json.dumps({"action_type": "wait", "description": "等待加载"},
                             ensure_ascii=False)
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "wait", "description": "等待加载"},
                       tree_elements=[], ocr_elements=[])
    agent.client = FakeClient(["不是 JSON", "页面已加载", action_json])
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    ui_state.init_state("计数")

    executed = []
    monkeypatch.setattr(agent, "_execute", lambda a: executed.append(a) or False)
    agent._execute_step(1, TaskMemory("等待页面"))

    # 1 次失败的合并请求 + 回退的 observe + decide
    assert agent.client.chat.completions.calls == 3
    assert len(executed) == 1
    assert agent._last_step_timings["merged_fallback"] is True


def test_every_step_completion_adds_one_llm_call(tmp_path, monkeypatch):
    """切回旧行为 every_step 时，每步会多一次带图的完成判断请求。"""
    agent = make_agent(tmp_path, monkeypatch,
                       {"action_type": "wait", "description": "等待加载"},
                       tree_elements=[], ocr_elements=[])
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "every_step")
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    ui_state.init_state("计数")

    agent._execute_step(3, TaskMemory("等待页面"))

    assert agent.client.chat.completions.calls == 3


def test_adaptive_settle_stops_when_screen_stable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 1.0)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_MODE", "adaptive")
    monkeypatch.setattr(config, "OBSERVE_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    agent = object.__new__(screen_agent.ScreenAgent)

    class Phone:
        calls = 0

        def screenshot(self, save_path="screen.png"):
            self.calls += 1
            # 前两帧不同，第三帧开始稳定
            shade = 0 if self.calls >= 3 else self.calls * 40
            Image.new("RGB", (32, 32), (shade, shade, shade)).save(save_path)

    agent.phone = Phone()
    elapsed = agent._settle_screen()

    assert agent.phone.calls >= 3
    assert elapsed < 1.0


def test_step_usage_and_task_summary(tmp_path, monkeypatch):
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15)
    agent = make_agent(tmp_path, monkeypatch,
                       {"observation": "页面已加载",
                        "action_type": "wait", "description": "等待"},
                       tree_elements=[], ocr_elements=[], usage=usage)
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    ui_state.init_state("计数")
    agent.run_dir = tmp_path / "run"
    agent.run_dir.mkdir()

    memory = TaskMemory("等待页面")
    agent._execute_step(1, memory)

    record = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    assert record["usage"]["prompt_tokens"] == 12
    assert record["usage"]["completion_tokens"] == 3
    assert record["usage"]["total_tokens"] == 15
    summary = agent._usage_summary(1)
    assert summary["total_tokens"] == 15
    assert summary["per_step_total_tokens"] == 15
    assert summary["by_provider"]["deepseek"]["total_tokens"] == 15


def test_missing_usage_writes_null(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch,
                       {"observation": "页面已加载",
                        "action_type": "wait", "description": "等待"},
                       tree_elements=[], ocr_elements=[])
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    ui_state.init_state("计数")
    agent.run_dir = tmp_path / "run"
    agent.run_dir.mkdir()

    agent._execute_step(1, TaskMemory("等待页面"))

    record = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    assert record["usage"] is None


def test_fixed_settle_waits_full_duration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.5)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_MODE", "fixed")
    sleeps = []
    monkeypatch.setattr(screen_agent.time, "sleep", lambda s: sleeps.append(s))
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.phone = SimpleNamespace()

    assert agent._settle_screen() == 0.5
    assert sleeps == [0.5]
