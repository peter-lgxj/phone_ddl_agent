"""QQ 群定位：列表滚动 + OCR，不依赖控件树、不输入中文。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

import rule_policy
import screen_agent
from memory import TaskMemory
from rule_policy import RuleContext

TARGET = "2026 计算机班级群"
DISTRACTOR = "2026 计算机班级二群"

ABSENT_XML = (
    '<hierarchy rotation="0">'
    '<node text="搜索" content-desc="搜索" class="android.widget.TextView" '
    'clickable="true" bounds="[0,0][200,100]" />'
    '<node text="张三" class="android.widget.TextView" clickable="true" '
    'bounds="[0,300][500,400]" />'
    "</hierarchy>"
)

VISIBLE_XML = (
    '<hierarchy rotation="0">'
    f'<node text="{TARGET}" class="android.widget.TextView" clickable="true" '
    'bounds="[0,700][500,800]" />'
    f'<node text="{DISTRACTOR}" class="android.widget.TextView" clickable="true" '
    'bounds="[0,900][500,1000]" />'
    "</hierarchy>"
)


def test_qq_list_only_scrolls_instead_of_opening_search():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=ABSENT_XML, list_only=True, list_max_scrolls=8,
    )

    action = rule_policy.decide(ctx)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert action["rule"]["name"] == "list_scroll"
    assert ctx.state["list_scrolls"] == 1


def test_qq_list_only_still_opens_visible_exact_group():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=VISIBLE_XML, list_only=True,
    )

    action = rule_policy.decide(ctx)

    assert action is not None
    assert action["action_type"] == "tap"
    assert action["rule"]["name"] == "open_group"
    assert (action["x"], action["y"]) == (250, 750)


def test_qq_list_only_stops_after_max_scrolls():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=ABSENT_XML, list_only=True, list_max_scrolls=2,
    )

    assert rule_policy.decide(ctx)["action_type"] == "swipe"
    assert rule_policy.decide(ctx)["action_type"] == "swipe"
    assert rule_policy.decide(ctx) is None
    assert ctx.state["list_failed"] == "max_scrolls"


class _FakePhone:
    def __init__(self, path: Path):
        self.path = path
        self.taps = []

    def screenshot(self, target):
        Image.new("RGB", (1080, 2374), "white").save(target)
        self.path = Path(target)

    def tap(self, x, y):
        self.taps.append((x, y))


class _FakeOCR:
    def __init__(self, entries):
        self.entries = entries

    def detect_ocr(self, path):
        return self.entries


def _agent(tmp_path, entries):
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.run_dir = tmp_path
    agent.phone = _FakePhone(tmp_path / "shot.png")
    agent.ocr = _FakeOCR(entries)
    agent._ocr_nav_state = {}
    return agent


def test_ocr_list_navigation_ignores_similar_group_name(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": DISTRACTOR, "center": (400, 900)},
        {"text": TARGET, "center": (400, 1200)},
    ])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "tap"
    assert action["ocr_list_hit"] is True
    assert (action["x"], action["y"]) == (400, 1200)


def test_ocr_list_navigation_scrolls_when_target_absent(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": DISTRACTOR, "center": (400, 900)},
        {"text": "文件传输助手", "center": (400, 1200)},
    ])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert action["ocr_list_direction"] == "down"
    assert action["y"] > action["y2"]
    assert agent._ocr_nav_state["scrolls"] == 1


def test_ocr_list_navigation_returns_back_from_chat_page(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "四人小组(5)", "center": (200, 100)},
        {"text": "发送", "center": (900, 2250)},
        {"text": "表情", "center": (800, 2250)},
    ])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "back"
    assert action["ocr_chat_back"] == 1


def test_ocr_list_navigation_never_scrolls_unknown_screen(tmp_path):
    agent = _agent(tmp_path, [{"text": "设置", "center": (400, 300)}])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    assert agent._ocr_list_navigate(memory)["action_type"] == "back"
    assert agent._ocr_list_navigate(memory)["action_type"] == "back"
    assert agent._ocr_list_navigate(memory) is None
    assert agent._ocr_nav_state["exhausted"] is True


def test_ocr_list_navigation_flips_up_after_reaching_bottom(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": "文件传输助手", "center": (400, 1200)},
    ])
    agent._ocr_nav_state = {
        "direction": "down", "scrolls": 8, "stagnant": 1,
        "last_text": "我创建的群聊10/12文件传输助手",
    }
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert action["ocr_list_direction"] == "up"
    assert action["y"] < action["y2"]
    assert agent._ocr_nav_state["scrolls"] == 9


def test_ocr_list_navigation_finds_target_after_up_scroll(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": TARGET, "center": (400, 500)},
    ])
    agent._ocr_nav_state = {"direction": "up", "scrolls": 9}
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["ocr_list_hit"] is True
    assert (action["x"], action["y"]) == (400, 500)


def test_archive_step_refreshes_stale_current_screenshot(tmp_path, monkeypatch):
    """规则/OCR 步没有新截图时，归档必须补拍当前界面，不能复制上一步的旧图。"""
    import os
    import time

    monkeypatch.chdir(tmp_path)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.run_dir = tmp_path
    agent._step_started = time.time()
    agent._last_step_usage = []
    agent._last_image_meta = None

    class _Phone:
        def screenshot(self, path):
            Path(path).write_bytes(b"FRESH")

    agent.phone = _Phone()
    old = time.time() - 3600
    Path("screen_current.png").write_bytes(b"STALE")
    os.utime("screen_current.png", (old, old))
    Path("screen_observe.png").write_bytes(b"STALE-OBS")
    os.utime("screen_observe.png", (old, old))

    agent._archive_step(1, {"action_type": "tap", "rule": {"name": "open_group"}}, "")

    assert (tmp_path / "step_001_current.png").read_bytes() == b"FRESH"
    assert not (tmp_path / "step_001_observe.png").exists()


def test_qq_type_action_is_blocked_without_calling_phone():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    agent._list_only_mode = True
    calls = []

    class _Phone:
        def type_text(self, text, allow_search=False):
            calls.append(text)

    agent.phone = _Phone()

    action = {"action_type": "type", "text": "计算机班级群"}
    assert agent._execute(action) is False
    assert action["blocked"] is True
    assert "列表滚动" in action["blocked_reason"]
    assert calls == []
