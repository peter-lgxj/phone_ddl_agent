"""Android 17 ADB 回退驱动：控件树、坐标映射、动作映射与只读拦截。

样本来自真机证据 ``runs/qq_real_device_20261121``（runs 不入库，已裁剪到
``tests/fixtures/android17``）；全部用例不连接手机、不调用模型。
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

import phone_driver
import screen_agent
from adb_controller import (
    AdbDumpController,
    InputMethodUnavailable,
    InputNotApplied,
    TreeUnavailable,
    bounds_center,
    interactive_elements,
    map_point,
    parse_bounds,
    screen_text,
)
from ocr_locator import (
    OCRLocator,
    STRONG_MATCH_THRESHOLD,
    enforce_tap_bounds,
    target_match_score,
)
from memory import TaskMemory, format_steps_compact
from phone_controller import Uiautomator2CompatibilityError, _compatibility_message

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "android17"
XML_TEXT = (FIXTURE / "qq_group_top.xml").read_text(encoding="utf-8")
PNG_BYTES = (FIXTURE / "qq_group_top_540.png").read_bytes()


class RecordingController(AdbDumpController):
    """记录 adb 命令，不真正执行。"""

    def __init__(self, read_only: bool = False):
        super().__init__(probe=False, read_only=read_only)
        self.commands: list[tuple] = []

    def _run(self, *args, timeout: float = 30.0):
        self.commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")


# ---------------------------------------------------------------- 控件树
def test_parse_real_dump_bounds_and_center():
    bounds = "[139,611][928,829]"
    assert parse_bounds(bounds) == (139, 611, 928, 829)
    assert bounds_center(bounds) == ((139 + 928) // 2, (611 + 829) // 2)


def test_interactive_elements_keep_text_and_bounds():
    elements = interactive_elements(XML_TEXT)
    assert elements, "真机 dump 应解析出可交互元素"
    target = next(
        item for item in elements if "机器学习作业三" in item["text"]
    )
    assert target["bbox"] == (139, 611, 928, 829)
    assert target["center"] == ((139 + 928) // 2, (611 + 829) // 2)
    assert target["type"] == "control"
    assert target["source"] == "xml"


def test_screen_text_preserves_order_and_dedupes():
    text = screen_text(XML_TEXT)
    lines = text.splitlines()
    assert "【COMP7503】机器学习作业三 11.29 23:59 交到 Moodle" in lines
    assert lines.index("如风") < lines.index(
        "【COMP7503】机器学习作业三 11.29 23:59 交到 Moodle"
    )
    assert len(lines) == len(set(lines))


# ---------------------------------------------------------------- 坐标映射
def test_map_point_scales_screenshot_to_device_coordinates():
    # 夹具截图缩到 540x1187，设备 dump 是 1080x2374
    assert map_point(100, 200, (540, 1187), (1080, 2374)) == (200, 400)
    assert map_point(540, 1187, (540, 1187), (1080, 2374)) == (1080 - 1, 2374 - 1)
    assert map_point(-5, -5, (540, 1187), (1080, 2374)) == (0, 0)


def test_map_point_is_identity_for_same_size():
    assert map_point(321, 654, (1080, 2374), (1080, 2374)) == (321, 654)


# ---------------------------------------------------------------- 坐标守卫
def _list_elements() -> list[dict]:
    return [
        {"text": "2026 计算机班级群", "center": (540, 900),
         "bbox": (100, 800, 980, 1000), "source": "xml", "type": "control"},
        {"text": "家人群", "center": (540, 1400),
         "bbox": (100, 1300, 980, 1500), "source": "xml", "type": "control"},
    ]


def test_tap_outside_screen_is_rejected():
    action = {"action_type": "tap", "x": 5000, "y": 5000,
              "target": "2026 计算机班级群", "description": "点击群聊"}
    guarded, decision = enforce_tap_bounds(action, _list_elements(), (1080, 2374))

    assert guarded is None
    assert decision["allowed"] is False
    assert decision["reason"] == "outside_screen"


def test_list_tap_uses_exact_bounds_not_scaled_coordinates():
    # 模型给的比例坐标落在两行之间；必须改成目标行的 bounds 中心
    action = {"action_type": "tap", "x": 540, "y": 1150,
              "target": "家人群", "description": "点击群聊家人群"}
    guarded, decision = enforce_tap_bounds(action, _list_elements(), (1080, 2374))

    assert decision["allowed"] is True
    assert decision["source"] == "bounds"
    assert (guarded["x"], guarded["y"]) == (540, 1400)
    assert decision["moved"] is True


def test_ambiguous_list_target_is_rejected():
    elements = [
        {"text": "群聊", "center": (540, 900), "bbox": (100, 800, 980, 1000),
         "source": "xml", "type": "control"},
        {"text": "群聊", "center": (540, 1700), "bbox": (100, 1600, 980, 1800),
         "source": "xml", "type": "control"},
    ]
    action = {"action_type": "tap", "x": 540, "y": 1200,
              "target": "群聊", "description": "打开群聊"}
    guarded, decision = enforce_tap_bounds(action, elements, (1080, 2374))

    assert guarded is None
    assert decision["reason"] == "ambiguous_list_target"


def test_non_list_tap_keeps_model_coords_when_target_unresolved():
    action = {"action_type": "tap", "x": 20, "y": 30,
              "target": "返回", "description": "点击返回图标"}
    guarded, decision = enforce_tap_bounds(action, _list_elements(), (1080, 2374))

    assert decision["allowed"] is True
    assert decision["source"] == "model_coords"
    assert (guarded["x"], guarded["y"]) == (20, 30)


def test_target_match_score_requires_identical_case():
    exact = {"text": "LLM小组", "center": (540, 900)}
    variant = {"text": "llm小组", "center": (540, 900)}

    assert target_match_score(exact, "LLM小组") == 1.0
    assert target_match_score(variant, "LLM小组") < STRONG_MATCH_THRESHOLD


def test_list_guard_rejects_case_variant_target():
    elements = [
        {"text": "llm小组", "center": (540, 900), "bbox": (100, 800, 980, 1000),
         "source": "xml", "type": "control"},
    ]
    action = {"action_type": "tap", "x": 540, "y": 900,
              "target": "LLM小组", "description": "点击群聊 LLM小组"}
    guarded, decision = enforce_tap_bounds(action, elements, (1080, 2374))

    # 只有大小写不同：不能吸附到「llm小组」上
    assert guarded is None
    assert decision["reason"] == "list_target_unresolved"


def test_list_guard_snaps_to_exact_case_not_variant():
    elements = [
        {"text": "llm小组", "center": (540, 900), "bbox": (100, 800, 980, 1000),
         "source": "xml", "type": "control"},
        {"text": "LLM小组", "center": (540, 1400), "bbox": (100, 1300, 980, 1500),
         "source": "xml", "type": "control"},
    ]
    action = {"action_type": "tap", "x": 540, "y": 900,
              "target": "LLM小组", "description": "点击群聊 LLM小组"}
    guarded, decision = enforce_tap_bounds(action, elements, (1080, 2374))

    assert decision["allowed"] is True
    assert decision["source"] == "bounds"
    assert (guarded["x"], guarded["y"]) == (540, 1400)


# ---------------------------------------------------------------- 截图
def test_capture_with_dump_issues_both_and_reports_parallel(tmp_path):
    controller = RecordingController()
    calls = []

    def fake_shot(path):
        calls.append("shot")
        return "image"

    def fake_dump():
        calls.append("dump")
        return "<hierarchy />"

    controller.screenshot = fake_shot
    controller.dump_hierarchy = fake_dump

    image, xml, report = controller.capture_with_dump(str(tmp_path / "screen.png"))

    assert image == "image"
    assert xml == "<hierarchy />"
    assert report["parallel"] is True
    assert sorted(calls) == ["dump", "shot"]


def test_capture_with_dump_degrades_when_dump_fails(tmp_path):
    controller = RecordingController()
    controller.screenshot = lambda path: "image"

    def failing_dump():
        raise RuntimeError("uiautomator dump failed")

    controller.dump_hierarchy = failing_dump

    image, xml, report = controller.capture_with_dump(str(tmp_path / "screen.png"))

    assert image == "image"
    assert xml is None
    assert "dump" in report["errors"]


def test_dump_reuses_cache_only_for_same_frame_fingerprint():
    controller = RecordingController()
    controller._exec_out = lambda *args, timeout=30.0: (
        b"<hierarchy><node bounds=\"[0,0][1080,2374]\" /></hierarchy>"
        if args and args[0] == "cat" else PNG_BYTES
    )

    controller.set_frame_fingerprint("frame-a")
    controller.dump_hierarchy()
    assert controller.dump_count == 1

    # 同一帧指纹：即使 TTL 已过也复用，不再 dump
    assert controller.dump_hierarchy(use_cache=True) == controller._last_dump
    assert controller.dump_count == 1
    assert controller.dump_reuse_count == 1

    # 指纹变化：视为界面已变，必须重新 dump
    controller.set_frame_fingerprint("frame-b")
    controller.dump_hierarchy(use_cache=True)
    assert controller.dump_count == 2


def test_invalidate_dump_forces_fresh_dump_after_action():
    controller = RecordingController()
    controller._exec_out = lambda *args, timeout=30.0: (
        b"<hierarchy><node bounds=\"[0,0][1080,2374]\" /></hierarchy>"
        if args and args[0] == "cat" else PNG_BYTES
    )
    controller.set_frame_fingerprint("frame-a")
    controller.dump_hierarchy()
    controller.tap(10, 20)  # 动作后界面可能已变
    controller.set_frame_fingerprint("frame-a")  # 即使截图哈希碰巧相同也不得复用

    controller.dump_hierarchy(use_cache=True)
    assert controller.dump_count == 2


def test_dump_failure_never_returns_stale_remote_file():
    """uiautomator dump 失败时不能继续读旧 XML，否则会把上一轮界面当当前界面。"""
    controller = RecordingController()
    controller._run = lambda *args, timeout=30.0: subprocess.CompletedProcess(
        args, 1, stdout=b"", stderr=b"ERROR: could not get idle state."
    )
    controller._exec_out = lambda *args, timeout=30.0: (
        '<hierarchy><node text="Outlook 收件箱" /></hierarchy>'.encode("utf-8")
    )

    with pytest.raises(RuntimeError, match="uiautomator dump"):
        controller.dump_hierarchy()


def test_wechat_package_skips_dump_without_touching_device():
    """微信已知无控件树：直接抛 TreeUnavailable，不应再执行 adb dump。"""
    controller = RecordingController()
    controller.tree_unsupported = True
    controller._run = lambda *args, **kwargs: pytest.fail("微信不应执行 adb dump")

    with pytest.raises(TreeUnavailable):
        controller.dump_hierarchy()


def test_dump_uses_unique_remote_path_and_cleans_up():
    controller = RecordingController()
    paths = []

    def fake_run(*args, timeout=30.0):
        if "uiautomator" in args:
            paths.append(args[3])
        return subprocess.CompletedProcess(
            args, 0, stdout=b'<hierarchy><node text="QQ" /></hierarchy>', stderr=b""
        )

    controller._run = fake_run
    controller._exec_out = lambda *args, timeout=30.0: (
        '<hierarchy><node text="QQ" /></hierarchy>'.encode("utf-8")
    )

    controller.dump_hierarchy()
    assert paths and paths[0].startswith("/sdcard/window_dump_")
    assert paths[0] != "/sdcard/window_dump.xml"


def test_dump_kills_stale_u2_owner_then_retries():
    """Android 17 上残留的 uiautomator2 服务会占着 UiAutomation，dump 被 SIGKILL。"""
    controller = RecordingController()
    calls = []
    dumps = {"count": 0}

    def fake_run(*args, timeout=30.0):
        calls.append(args)
        if "uiautomator" in args:
            dumps["count"] += 1
            if dumps["count"] == 1:
                return subprocess.CompletedProcess(args, 137, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(
                args, 0, stdout=b"UI hierchary dumped", stderr=b""
            )
        if args[:3] == ("shell", "ps", "-A"):
            return subprocess.CompletedProcess(
                args, 0,
                stdout=b"23851 app_process / com.wetest.uia2.Main -p 9008\n",
                stderr=b"",
            )
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    controller._run = fake_run
    controller._exec_out = lambda *args, timeout=30.0: (
        '<hierarchy><node text="QQ" /></hierarchy>'.encode("utf-8")
    )

    xml = controller.dump_hierarchy()

    assert "QQ" in xml
    assert dumps["count"] == 2
    assert any(call[:3] == ("shell", "kill", "23851") for call in calls)


def test_dump_failure_checks_stale_u2_even_with_stderr():
    """即使 dump 有错误输出，也要探测一次残留 u2，避免 already registered 分支漏清理。"""
    controller = RecordingController()
    checked = []
    dumps = {"count": 0}

    def fake_run(*args, timeout=30.0):
        if "uiautomator" in args:
            dumps["count"] += 1
            if dumps["count"] == 1:
                return subprocess.CompletedProcess(
                    args, 1, stdout=b"", stderr=b"ERROR: could not get idle state."
                )
            return subprocess.CompletedProcess(
                args, 0, stdout=b"UI hierchary dumped", stderr=b""
            )
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    controller._run = fake_run
    controller._exec_out = lambda *args, timeout=30.0: (
        '<hierarchy><node text="QQ" /></hierarchy>'.encode("utf-8")
    )
    controller._diagnose_dump_failure = lambda: (checked.append(True) or ("诊断", False))

    xml = controller.dump_hierarchy()

    assert "QQ" in xml
    assert checked == [True]


def test_dump_failure_reports_uiautomation_owner_hint():
    """两次都被 SIGKILL 且无输出时，错误信息要给出真实原因和可执行命令。"""
    controller = RecordingController()

    def fake_run(*args, timeout=30.0):
        if "uiautomator" in args:
            return subprocess.CompletedProcess(args, 137, stdout=b"", stderr=b"")
        if args[:2] == ("shell", "ps"):
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "logcat" in args:
            return subprocess.CompletedProcess(
                args, 0,
                stdout=(
                    b"E AndroidRuntime: java.lang.IllegalStateException: "
                    b"UiAutomationService already registered!\n"
                ),
                stderr=b"",
            )
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    controller._run = fake_run

    with pytest.raises(RuntimeError) as exc:
        controller.dump_hierarchy()

    message = str(exc.value)
    assert "already registered" in message
    assert "rc=137" in message
    assert "pkill -f com.wetest.uia2.Main" in message


def test_screenshot_uses_screencap_and_saves_png(tmp_path):
    controller = RecordingController()

    def fake_exec_out(*args, timeout=30.0):
        controller.commands.append(("exec-out", *args))
        return PNG_BYTES

    controller._exec_out = fake_exec_out
    path = tmp_path / "screen.png"
    image = controller.screenshot(str(path))

    assert ("exec-out", "screencap", "-p") in controller.commands
    assert image.size == (540, 1187)
    assert path.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------- 动作映射
def test_tap_swipe_back_home_map_to_adb_commands():
    controller = RecordingController()
    controller.tap(100, 200)
    controller.swipe(1, 2, 3, 4, duration=0.5)
    controller.press_back()
    controller.press_home()

    assert controller.commands[0] == ("shell", "input", "tap", "100", "200")
    assert controller.commands[1] == (
        "shell", "input", "swipe", "1", "2", "3", "4", "500"
    )
    assert controller.commands[2] == ("shell", "input", "keyevent", "4")
    assert controller.commands[3] == ("shell", "input", "keyevent", "3")


def test_open_app_uses_monkey_with_package():
    controller = RecordingController()
    controller.open_app("com.tencent.mm")

    assert controller.commands[0][:4] == (
        "shell", "monkey", "-p", "com.tencent.mm"
    )


def test_tap_input_box_clicks_edit_text_center():
    controller = RecordingController()
    controller.dump_hierarchy = lambda *args, **kwargs: (
        "<hierarchy rotation=\"0\"><node text=\"\" class=\"android.widget.EditText\" "
        "clickable=\"true\" focusable=\"true\" bounds=\"[100,200][300,260]\" /></hierarchy>"
    )
    assert controller.tap_input_box() is True
    assert controller.commands[0] == ("shell", "input", "tap", "200", "230")


# ---------------------------------------------------------------- 只读保护
@pytest.mark.parametrize("method", ["type_text", "press_enter", "send", "tap_send_button"])
def test_read_only_blocks_mutating_methods(method):
    controller = RecordingController(read_only=True)
    call = getattr(controller, method)
    args = ("测试消息",) if method == "type_text" else ()
    with pytest.raises(PermissionError):
        call(*args)
    assert controller.commands == []


def test_writable_controller_allows_type_and_enter():
    controller = RecordingController(read_only=False)
    controller.type_text("hello world")
    controller.press_enter()
    assert controller.commands[0] == ("shell", "input", "text", "hello%sworld")
    assert controller.commands[1] == ("shell", "input", "keyevent", "66")


class ChineseInputController(RecordingController):
    """模拟 ADBKeyboard 已启用/未启用，以及广播是否真的写入输入框。"""

    def __init__(self, *, installed: bool = True, applied: bool = True):
        super().__init__(read_only=False)
        self.installed = installed
        self.applied = applied
        self.input_value = ""

    def _run(self, *args, timeout: float = 30.0):
        self.commands.append(args)
        if args[:3] == ("shell", "ime", "list"):
            stdout = (b"com.android.adbkeyboard/.AdbIME\n" if self.installed
                      else b"com.android.inputmethod.latin/.LatinIME\n")
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")
        if args[:3] == ("shell", "settings", "get"):
            stdout = (b"com.android.adbkeyboard/.AdbIME\n" if self.installed
                      else b"com.android.inputmethod.latin/.LatinIME\n")
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")
        if args[:3] == ("shell", "am", "broadcast"):
            if self.applied:
                self.input_value = base64.b64decode(args[-1]).decode("utf-8")
            return subprocess.CompletedProcess(
                args, 0, stdout=b"Broadcast completed: result=0", stderr=b""
            )
        return super()._run(*args, timeout=timeout)

    def dump_hierarchy(self, use_cache: bool = False, ttl: float = 0.5) -> str:
        return (
            '<hierarchy><node text="' + self.input_value +
            '" class="android.widget.EditText" clickable="true" focusable="true" '
            'bounds="[0,0][100,100]" /></hierarchy>'
        )


def test_non_ascii_input_uses_adbkeyboard_base64_broadcast():
    controller = ChineseInputController()

    controller.type_text("LLM小组", allow_search=True)

    broadcast = next(
        command for command in controller.commands
        if command[:3] == ("shell", "am", "broadcast")
    )
    assert broadcast[3:5] == ("-a", "ADB_INPUT_B64")
    assert base64.b64decode(broadcast[-1]).decode("utf-8") == "LLM小组"
    assert not any(
        command[:3] == ("shell", "input", "text") for command in controller.commands
    )


def test_non_ascii_input_without_adbkeyboard_fails_with_actionable_reason():
    controller = ChineseInputController(installed=False)

    with pytest.raises(InputMethodUnavailable) as excinfo:
        controller.type_text("LLM小组", allow_search=True)

    message = str(excinfo.value)
    assert "ADBKeyboard" in message
    assert "列表滚动" in message


def test_non_ascii_input_not_applied_raises_instead_of_silently_retrying():
    controller = ChineseInputController(applied=False)

    with pytest.raises(InputNotApplied) as excinfo:
        controller.type_text("LLM小组", allow_search=True)

    assert "未变化" in str(excinfo.value)
    assert "不要重复" in str(excinfo.value)


class _FailingTypePhone:
    def __init__(self):
        self.calls = 0

    def type_text(self, text, allow_search=False):
        self.calls += 1
        raise InputMethodUnavailable("未安装 ADBKeyboard")


def test_repeated_ineffective_type_is_blocked_before_second_adb_call():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = False
    agent.phone = _FailingTypePhone()
    agent._input_failures = {}
    action = {"action_type": "type", "text": "LLM小组"}

    assert agent._execute(action) is False
    assert action["blocked"] is True
    assert "列表滚动" in action["blocked_reason"]

    with pytest.raises(RuntimeError, match="输入无效"):
        agent._execute(action)

    assert agent.phone.calls == 1


def test_blocked_input_reason_is_visible_in_history():
    """失败原因要带回后续 prompt，模型才知道别再重复同样的输入。"""
    memory = TaskMemory("在 QQ 搜索群")
    memory.add_step(
        thought="",
        action={
            "action_type": "type",
            "text": "LLM小组",
            "blocked": True,
            "blocked_reason": "未安装 ADBKeyboard，改用列表滚动定位",
        },
        observation="",
    )

    history = format_steps_compact(memory.steps)

    assert "已拦截" in history
    assert "列表滚动" in history


# ---------------------------------------------------------------- 驱动选择
def test_resolve_driver_mode_aliases_and_invalid():
    assert phone_driver.resolve_driver_mode("auto") == "auto"
    assert phone_driver.resolve_driver_mode("u2") == "uiautomator2"
    assert phone_driver.resolve_driver_mode("adb") == "adb_dump"
    with pytest.raises(ValueError):
        phone_driver.resolve_driver_mode("magic")


def test_auto_mode_falls_back_on_compatibility_error(monkeypatch):
    class BrokenU2:
        def __init__(self, locator):
            raise Uiautomator2CompatibilityError("ApplicationSharedMemory not initialized")

    class FakeAdb:
        kind = "adb_dump"

        def __init__(self, locator):
            self.locator = locator

    monkeypatch.setattr(phone_driver, "PhoneController", BrokenU2)
    monkeypatch.setattr(phone_driver, "AdbDumpController", FakeAdb)

    controller = phone_driver.create_phone_controller(object())
    assert controller.kind == "adb_dump"


def test_explicit_u2_mode_does_not_silently_fall_back(monkeypatch):
    class BrokenU2:
        def __init__(self, locator):
            raise Uiautomator2CompatibilityError("ApplicationSharedMemory not initialized")

    monkeypatch.setattr(phone_driver, "PhoneController", BrokenU2)
    with pytest.raises(Uiautomator2CompatibilityError):
        phone_driver.create_phone_controller(object(), mode="uiautomator2")


# ---------------------------------------------------------------- 报错文案
def test_android17_message_points_to_issue_and_fallback():
    message = _compatibility_message(
        "3.7.1", "37", RuntimeError("ApplicationSharedMemory not initialized")
    )
    assert "Android 17" in message
    assert "issues/1209" in message
    assert "SCREEN_DRIVER=adb_dump" in message


def test_old_version_message_mentions_upgrade_floor():
    message = _compatibility_message("3.3.1", "36", RuntimeError("rpc failed"))
    assert "3.7.1" in message
    assert "pip install -U" in message


# ---------------------------------------------------------------- 与 OCR 控件树联动
def test_ocr_tree_follows_driver_provider():
    locator = OCRLocator(device=None)
    locator.set_tree_provider(lambda: XML_TEXT)
    elements = locator.detect_tree()
    assert any("实验报告二" in item["text"] for item in elements)


# ---------------------------------------------------------------- 证据与汇总
def test_step_json_records_adb_dump_driver(tmp_path):
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.run_dir = tmp_path
    agent.driver_kind = "adb_dump"
    agent._last_observation_scope = "post_action"
    agent._last_image_meta = None
    agent._last_model_action = None
    agent._last_step_timings = {}
    agent._last_step_usage = []

    agent._archive_step(1, {"action_type": "tap", "x": 1, "y": 2}, "观察")

    record = json.loads((tmp_path / "step_001.json").read_text(encoding="utf-8"))
    assert record["driver"] == "adb_dump"


def test_summarize_timings_reads_adb_dump_step(tmp_path):
    from summarize_timings import summarize_run

    run = tmp_path / "adb_run"
    run.mkdir()
    (run / "step_001.json").write_text(
        json.dumps({
            "step": 1,
            "driver": "adb_dump",
            "action": {"action_type": "tap"},
            "observation": "观察",
            "timings": {"decision": 1.5, "execute": 0.8, "screenshot": 0.6},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = summarize_run(run)
    assert summary["steps"] == 1
    assert summary["stages"]["decision"]["samples"] == 1
    assert summary["stages"]["execute"]["total"] == 0.8


def test_usage_summary_marks_fallback_as_incomplete():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.driver_kind = "adb_dump"
    agent.model = "test-model"
    agent.usage_events = []

    summary = agent._usage_summary(1)
    assert summary["driver"] == "adb_dump"
    assert summary["full_pipeline"] is False
    assert "不是完整 uiautomator2 链路" in summary["driver_note"]
