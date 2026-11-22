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
        {"text": DISTRACTOR, "center": (400, 900)},
        {"text": "文件传输助手", "center": (400, 1200)},
    ])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert action["y"] > action["y2"]
    assert agent._ocr_nav_state["scrolls"] == 1


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
