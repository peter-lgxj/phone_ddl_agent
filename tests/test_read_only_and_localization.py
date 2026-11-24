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


def test_read_only_allows_qq_connector_task():
    task = (
        "打开qq，在消息列表或“联系人 > 群聊”里上下滚动查找「COMP7503 作业通知群」并进入聊天窗口；"
        "不要使用搜索框、不要输入中文，不要点名字相似的群。"
        "向上翻阅最近消息，逐条读出包含截止时间（deadline / due / 截止 / 提交）的消息原文与发送者"
    )

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


# ---------------------------------------------------------------- 无进展刹车
def _progress_agent(elements):
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.ocr = SimpleNamespace(detect_ocr=lambda path: list(elements))
    agent._last_image_meta = {"orig_size": (1080, 2374)}
    agent._progress_key = None
    agent._progress_signature = None
    agent._progress_repeats = 0
    return agent


def _search_screen(placeholder: str = "搜索"):
    return [
        {"text": placeholder, "center": (540, 180), "bbox": (100, 150, 980, 210)},
        {"text": "通讯录", "center": (540, 2200), "bbox": (0, 2150, 1080, 2250)},
    ]


def test_no_progress_brakes_after_same_action_and_screen(monkeypatch):
    monkeypatch.setattr(screen_agent.config, "NO_PROGRESS_GUARD", True)
    monkeypatch.setattr(screen_agent.config, "NO_PROGRESS_LIMIT", 3)
    agent = _progress_agent(_search_screen())
    action = {"action_type": "tap_input_box", "description": "点击顶部搜索框"}

    results = [agent._detect_no_progress(action) for _ in range(5)]

    assert results[:4] == [None, None, None, None]
    assert results[4] is not None
    assert "同一动作" in results[4]
    assert "当前策略不可行" in results[4]
    assert "列表滚动" in results[4]


def test_no_progress_resets_when_screen_changes(monkeypatch):
    monkeypatch.setattr(screen_agent.config, "NO_PROGRESS_GUARD", True)
    monkeypatch.setattr(screen_agent.config, "NO_PROGRESS_LIMIT", 2)
    elements = _search_screen()
    agent = _progress_agent(elements)
    action = {"action_type": "tap_input_box"}

    agent._detect_no_progress(action)
    agent._detect_no_progress(action)      # 基线
    assert agent._detect_no_progress(action) is None
    assert agent._detect_no_progress(action) is not None  # 连续不变触发

    # 屏幕内容一变，计数立刻清零
    agent._progress_repeats = 0
    elements[0]["text"] = "课程群"
    agent._progress_signature = None
    agent._detect_no_progress(action)
    assert agent._detect_no_progress(action) is None


def test_screen_signature_ignores_status_bar_clock():
    from ocr_locator import structured_screen_signature

    morning = [
        {"text": "10:24", "center": (540, 30), "bbox": (0, 0, 1080, 60)},
        *_search_screen(),
    ]
    later = [
        {"text": "10:25", "center": (540, 30), "bbox": (0, 0, 1080, 60)},
        *_search_screen(),
    ]
    changed = [
        {"text": "10:25", "center": (540, 30), "bbox": (0, 0, 1080, 60)},
        *_search_screen("课程群"),
    ]

    assert (structured_screen_signature(morning, (1080, 2374))
            == structured_screen_signature(later, (1080, 2374)))
    assert (structured_screen_signature(morning, (1080, 2374))
            != structured_screen_signature(changed, (1080, 2374)))


def test_run_stops_with_readable_reason_on_no_progress(monkeypatch):
    monkeypatch.setattr(screen_agent.ui_state, "init_state", lambda task: None)
    monkeypatch.setattr(screen_agent.ui_state, "set_status", lambda *args: None)
    monkeypatch.setattr(screen_agent, "cleanup_old_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(screen_agent, "make_run_dir", lambda name: "runs/fake")
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = False
    agent.driver_kind = "u2"
    agent.max_steps = 5
    agent.last_status = None
    agent.phone = SimpleNamespace()
    agent._save_log = lambda memory: None
    agent._save_usage_summary = lambda memory: None

    def fake_step(step, memory):
        raise screen_agent.NoProgressDetected("屏幕内容没有实质变化，当前策略不可行")

    agent._execute_step = fake_step

    with pytest.raises(screen_agent.NoProgressDetected, match="当前策略不可行"):
        agent.run("读取当前屏幕")
    assert agent.last_status == "failed"
    assert "当前策略不可行" in agent.last_block_reason


# ---------------------------------------------------------------- OCR 输入框兜底
def test_tap_input_box_falls_back_to_ocr_when_yolo_misses(monkeypatch):
    controller = _Controller()
    controller.tree_unsupported = True
    controller.screenshot = lambda path: SimpleNamespace(size=(1080, 2374))
    controller.locator = SimpleNamespace(detect_ocr=lambda path: _search_screen())

    class FakeDetector:
        def find_best(self, path):
            return None

    monkeypatch.setattr(input_detector, "InputDetector", FakeDetector)

    assert controller.tap_input_box() is True
    assert controller.taps == [(540, 180)]


def test_tap_input_box_fails_only_after_tree_yolo_and_ocr(monkeypatch):
    controller = _Controller()
    controller.tree_unsupported = True
    controller.screenshot = lambda path: SimpleNamespace(size=(1080, 2374))
    controller.locator = SimpleNamespace(detect_ocr=lambda path: [
        {"text": "搜索", "center": (540, 2200), "bbox": (0, 2150, 1080, 2250)},
    ])

    class FakeDetector:
        def find_best(self, path):
            return None

    monkeypatch.setattr(input_detector, "InputDetector", FakeDetector)

    assert controller.tap_input_box() is False
    assert controller.taps == []


# ---------------------------------------------------------------- 任务级中文输入拒绝
def test_chinese_search_target_detects_wechat_search():
    task = "打开微信，通过搜索框输入「课程群」找到并进入该群聊"

    assert screen_agent.chinese_search_target(task) == "课程群"


def test_chinese_search_target_skips_scroll_and_ascii():
    assert screen_agent.chinese_search_target(
        "打开微信，在聊天列表里滚动查找「课程群」并进入"
    ) is None
    assert screen_agent.chinese_search_target(
        "打开微信，搜索「COMP7503 作业通知群」并进入"
    ) is None


def test_chinese_search_refusal_gives_install_and_retry_steps():
    reason = screen_agent.chinese_search_input_refusal("课程群")

    assert "ADBKeyboard" in reason
    assert "ime set com.android.adbkeyboard/.AdbIME" in reason
    assert "列表滚动" in reason


def test_run_refuses_chinese_search_before_opening_input_box(monkeypatch):
    monkeypatch.setattr(screen_agent.ui_state, "init_state", lambda task: None)
    monkeypatch.setattr(screen_agent.ui_state, "set_status", lambda *args: None)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent.driver_kind = "adb_dump"
    agent.last_status = None
    agent.phone = SimpleNamespace(chinese_input_available=lambda: False)

    with pytest.raises(screen_agent.ReadOnlyTaskRefused, match="ADBKeyboard"):
        agent.run("打开微信，通过搜索框输入「课程群」找到并进入该群聊")
    assert agent.last_status == "refused"


# ---------------------------------------------------------------- 搜索框放行 / 聊天框拒绝
_SEARCH_XML = (
    '<hierarchy rotation="0">'
    '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2374]" />'
    '<node class="android.widget.EditText" text="搜索" '
    'resource-id="com.tencent.mm:id/search_input" focused="true" '
    'bounds="[100,150][980,210]" />'
    "</hierarchy>"
)
_SEARCH_XML_UNFOCUSED = (
    '<hierarchy rotation="0">'
    '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2374]" />'
    '<node class="android.widget.EditText" text="搜索" '
    'resource-id="com.tencent.mm:id/search_input" focused="false" '
    'bounds="[100,150][980,210]" />'
    '<node class="android.widget.EditText" text="" '
    'resource-id="com.tencent.mm:id/chat_input" focused="true" '
    'bounds="[100,2100][980,2250]" />'
    "</hierarchy>"
)


def _input_policy_agent(xml: str = "", ocr_elements=None,
                        task: str = "打开微信搜索「课程群」"):
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.phone = SimpleNamespace(
        tree_unsupported=not xml,
        dump_hierarchy=lambda: xml,
        screenshot=lambda path: SimpleNamespace(size=(1080, 2374)),
    )
    agent.ocr = SimpleNamespace(detect_ocr=lambda path: list(ocr_elements or []))
    agent.last_memory = SimpleNamespace(task=task)
    return agent


def test_search_box_with_focus_is_allowed():
    agent = _input_policy_agent(_SEARCH_XML)

    allowed, reason, source = agent._search_input_permission({"action_type": "type"})

    assert allowed is True
    assert reason == ""
    assert source == "tree_search_focus"


def test_chat_input_focus_is_refused():
    agent = _input_policy_agent(_SEARCH_XML_UNFOCUSED)

    allowed, reason, source = agent._search_input_permission({"action_type": "type"})

    assert allowed is False
    assert "焦点不在搜索框" in reason
    assert source == "tree_chat_focus"


def test_unknown_focus_is_refused():
    agent = _input_policy_agent(_SEARCH_XML_UNFOCUSED.replace('focused="true"', 'focused="false"'),
                                task="打开微信读取消息")

    allowed, reason, source = agent._search_input_permission({"action_type": "type"})

    assert allowed is False
    assert "无法确认焦点" in reason
    assert source == "tree_unfocused"


def test_ocr_search_box_allows_typing_without_tree():
    agent = _input_policy_agent("", ocr_elements=_search_screen())

    allowed, reason, source = agent._search_input_permission({"action_type": "type"})

    assert allowed is True
    assert source == "ocr_search_box"


def test_ocr_without_search_box_is_refused():
    agent = _input_policy_agent("", ocr_elements=[
        {"text": "发送", "center": (900, 2200), "bbox": (800, 2150, 1000, 2250)},
    ])

    allowed, reason, source = agent._search_input_permission({"action_type": "type"})

    assert allowed is False
    assert "无法确认焦点归属" in reason
    assert source == "ocr_unknown"


def test_allowed_search_typing_is_logged_in_action():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent._search_input_active = False
    agent._input_failures = {}
    calls = []
    agent.phone = SimpleNamespace(
        type_text=lambda text, allow_search=False: calls.append((text, allow_search)),
    )
    agent._search_input_permission = lambda action: (True, "", "ocr_search_box")
    action = {"action_type": "type", "text": "课程群"}

    assert agent._execute(action) is False
    assert calls == [("课程群", True)]
    assert action["search_input"] is True
    assert action["input_policy"] == "search_box_only"
    assert action["input_policy_source"] == "ocr_search_box"
    assert agent._search_input_active is True


def test_type_into_chat_box_is_rejected_and_not_executed():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent._search_input_active = False
    agent._input_failures = {}
    agent.phone = _input_policy_agent(_SEARCH_XML_UNFOCUSED).phone
    agent.ocr = SimpleNamespace(detect_ocr=lambda path: [])
    agent.last_memory = SimpleNamespace(task="打开微信搜索「课程群」")
    action = {"action_type": "type", "text": "课程群"}

    assert agent._execute(action) is False
    assert action["blocked"] is True
    assert action["input_policy"] == "rejected"
    assert "焦点不在搜索框" in action["blocked_reason"]


def test_submit_after_search_typing_stops_safely():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent._search_input_active = True
    action = {"action_type": "press_enter"}

    with pytest.raises(screen_agent.NoProgressDetected, match="只检索不提交"):
        agent._execute(action)
    assert action["blocked"] is True
    assert action["input_policy"] == "search_submit_blocked"


# ---------------------------------------------------------------- 搜索结果精确名校验
def test_search_result_similar_name_is_not_tapped():
    agent, calls = _snap_agent()
    agent._search_input_active = True
    agent._search_target = "2026 计算机班级群"
    agent._search_name_misses = 0
    agent.ocr = SimpleNamespace(
        detect_tree=lambda: [],
        detect_ocr=lambda path: [{
            "text": "2026 计算机班级二群", "center": (400, 900),
            "bbox": (60, 860, 740, 940), "source": "ocr", "type": "text",
        }],
    )
    memory = TaskMemory("打开「2026 计算机班级群」")

    action = agent._snap_mapped_action({
        "action_type": "tap", "x": 400, "y": 900,
        "target": "2026 计算机班级二群", "description": "点击搜索结果",
    }, {}, memory)

    assert action["action_type"] == "wait"
    assert action["search_result_inexact"] == "2026 计算机班级二群"
    assert agent._search_name_misses == 1
    assert calls == []


def test_search_result_exact_name_is_tapped():
    agent, _ = _snap_agent()
    agent._search_input_active = True
    agent._search_target = "2026 计算机班级群"
    agent._search_name_misses = 0
    agent.ocr = SimpleNamespace(
        detect_tree=lambda: [],
        detect_ocr=lambda path: [{
            "text": "2026 计算机班级群", "center": (400, 900),
            "bbox": (60, 860, 740, 940), "source": "ocr", "type": "text",
        }],
    )
    memory = TaskMemory("打开「2026 计算机班级群」")

    action = agent._snap_mapped_action({
        "action_type": "tap", "x": 400, "y": 900,
        "target": "2026 计算机班级群", "description": "点击搜索结果",
    }, {}, memory)

    assert action["action_type"] == "tap"
    assert action["localization_source"] == "ocr"


# ---------------------------------------------------------------- 策略分级审计
def _strategy_agent(search_failed: str = "") -> screen_agent.ScreenAgent:
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._rule_state = {"search_failed": search_failed} if search_failed else {}
    agent._search_input_active = False
    return agent


def test_first_screen_tap_is_tagged_first_screen():
    agent = _strategy_agent()
    action = agent._tag_strategy(
        {"action_type": "tap", "target": "课程群"}, TaskMemory("打开微信「课程群」"))

    assert action["strategy"] == "first_screen"
    assert action["strategy_level"] == 1


def test_search_entry_and_input_are_tagged_search():
    agent = _strategy_agent()
    entry = agent._tag_strategy(
        {"action_type": "tap", "target": "搜索", "description": "点击顶部搜索框"},
        TaskMemory("打开微信搜索「课程群」"))
    typed = agent._tag_strategy(
        {"action_type": "type", "text": "课程群", "search_input": True},
        TaskMemory("打开微信搜索「课程群」"))

    assert entry["strategy"] == "search" and entry["strategy_level"] == 2
    assert typed["strategy"] == "search" and typed["strategy_level"] == 2


def test_dual_failure_swipe_is_tagged_scroll_fallback_with_reason():
    agent = _strategy_agent(search_failed="no_result")
    action = agent._tag_strategy(
        {"action_type": "swipe", "x": 540, "y": 1700, "x2": 540, "y2": 700},
        TaskMemory("打开微信搜索「课程群」"))

    assert action["strategy"] == "scroll_fallback"
    assert action["strategy_level"] == 3
    assert action["search_unavailable_reason"] == "no_result"
