"""QQ 群定位：列表滚动 + OCR，不依赖控件树、不输入中文。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

import rule_policy
import screen_agent
from memory import TaskMemory
from rule_policy import RuleContext

TARGET = "2026 计算机班级群"
DISTRACTOR = "2026 计算机班级二群"

ABSENT_XML = (
    '<hierarchy rotation="0"><node bounds="[0,0][1080,2374]">'
    '<node text="搜索" content-desc="搜索" class="android.widget.TextView" '
    'clickable="true" bounds="[0,0][200,100]" />'
    '<node text="张三" class="android.widget.TextView" clickable="true" '
    'bounds="[0,300][500,400]" />'
    "</node></hierarchy>"
)

VISIBLE_XML = (
    '<hierarchy rotation="0">'
    f'<node text="{TARGET}" class="android.widget.TextView" clickable="true" '
    'bounds="[0,700][500,800]" />'
    f'<node text="{DISTRACTOR}" class="android.widget.TextView" clickable="true" '
    'bounds="[0,900][500,1000]" />'
    "</hierarchy>"
)


def test_qq_without_search_scrolls_list_instead_of_opening_search():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=ABSENT_XML, search_allowed=False, list_max_scrolls=8,
    )

    action = rule_policy.decide(ctx)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert action["rule"]["name"] == "list_scroll"
    assert ctx.state["list_scrolls"] == 1


def test_qq_prefers_search_when_target_not_on_first_screen():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=ABSENT_XML,
    )

    action = rule_policy.decide(ctx)

    assert action is not None
    assert action["action_type"] == "tap"
    assert action["rule"]["name"] == "search_entry"
    assert ctx.state["search_stage"] == "entry"


def test_qq_list_only_still_opens_visible_exact_group():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=VISIBLE_XML,
    )

    action = rule_policy.decide(ctx)

    assert action is not None
    assert action["action_type"] == "tap"
    assert action["rule"]["name"] == "open_group"
    assert (action["x"], action["y"]) == (250, 750)


def test_qq_list_only_stops_after_max_scrolls():
    ctx = RuleContext(
        task=f"打开QQ，滚动列表找到「{TARGET}」", step=1, read_only=True,
        xml=ABSENT_XML, search_allowed=False, list_max_scrolls=2,
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


def test_ocr_list_navigation_ignores_case_variant_group(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": "llm小组", "center": (400, 900)},
    ])
    memory = TaskMemory("打开QQ，滚动列表找到「LLM小组」")

    action = agent._ocr_list_navigate(memory)

    # 只有大小写不同的群名不算命中，继续滚动找完全一致的
    assert action is not None
    assert action["action_type"] == "swipe"


def test_ocr_list_navigation_prefers_exact_case_group(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": "llm小组", "center": (400, 900)},
        {"text": "LLM小组", "center": (400, 1200)},
    ])
    memory = TaskMemory("打开QQ，滚动列表找到「LLM小组」")

    action = agent._ocr_list_navigate(memory)

    assert action["action_type"] == "tap"
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

    first = agent._ocr_list_navigate(memory)
    assert first["action_type"] == "wait"
    assert first["ocr_unknown_wait"] == 1
    assert agent._ocr_list_navigate(memory) is None
    assert agent._ocr_nav_state["exhausted"] is True


class _PagedPhone:
    """按页滚动的假手机：滑动手势会改变当前页，页面图像用于哈希比对。"""

    def __init__(self, pages_count: int, start_page: int = 0):
        self.pages_count = pages_count
        self.page = start_page
        self.taps = []
        self.path: Path | None = None

    def screenshot(self, target):
        color = (20 + self.page * 35, 60, 120)
        Image.new("RGB", (1080, 2374), color).save(target)
        self.path = Path(target)

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, action: dict) -> None:
        if action["y"] > action["y2"]:      # 手指上移 = 向下翻页
            self.page = min(self.pages_count - 1, self.page + 1)
        else:                               # 手指下移 = 向上翻页
            self.page = max(0, self.page - 1)


class _PagedOCR:
    def __init__(self, phone: _PagedPhone, pages: list[list[dict]]):
        self.phone = phone
        self.pages = pages

    def detect_ocr(self, path):
        return self.pages[self.phone.page]


def _list_page(rows: list[str]) -> list[dict]:
    entries = [{"text": "我创建的群聊 10/12", "center": (100, 200)}]
    entries += [
        {"text": row, "center": (400, 450 + index * 160)}
        for index, row in enumerate(rows)
    ]
    return entries


def _paged_agent(tmp_path, pages, start_page=0):
    phone = _PagedPhone(len(pages), start_page=start_page)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.run_dir = tmp_path
    agent.phone = phone
    agent.ocr = _PagedOCR(phone, pages)
    agent._ocr_nav_state = {}
    return agent, phone


def _drive_until_done(agent, phone, memory, limit=20):
    actions = []
    for _ in range(limit):
        action = agent._ocr_list_navigate(memory)
        actions.append(action)
        if action["action_type"] == "swipe":
            phone.swipe(action)
        elif action["action_type"] == "tap":
            return actions
    raise AssertionError(f"未在 {limit} 步内命中目标：{actions}")


def test_ocr_list_navigation_finds_target_below_after_scrolling_down(tmp_path):
    pages = [
        _list_page(["群A", "群B"]),
        _list_page(["群C", "群D"]),
        _list_page([TARGET, "群E"]),
    ]
    agent, phone = _paged_agent(tmp_path, pages, start_page=0)
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    actions = _drive_until_done(agent, phone, memory)

    assert actions[-1]["action_type"] == "tap"
    assert actions[-1]["ocr_list_hit"] is True
    directions = [
        action.get("ocr_list_direction")
        for action in actions if action["action_type"] == "swipe"
    ]
    assert directions == ["down", "down"]


def test_ocr_list_navigation_recovers_target_above_multiple_up_scrolls(tmp_path):
    pages = [
        _list_page([TARGET, "群B"]),
        _list_page(["群C", "群D"]),
        _list_page(["群E", "群F"]),
        _list_page(["群G", "群H"]),
    ]
    agent, phone = _paged_agent(tmp_path, pages, start_page=3)
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    actions = _drive_until_done(agent, phone, memory)

    assert actions[-1]["action_type"] == "tap"
    assert actions[-1]["ocr_list_hit"] is True
    directions = [
        action.get("ocr_list_direction")
        for action in actions if action["action_type"] == "swipe"
    ]
    assert "down" in directions and "up" in directions
    first_up = directions.index("up")
    # 方向一旦切到向上找回就锁定，后续不能翻回向下
    assert all(direction == "up" for direction in directions[first_up:])
    assert directions.count("up") >= 3


def test_ocr_list_navigation_fails_at_real_top_without_structural_taps(tmp_path):
    pages = [
        _list_page(["群A", "群B"]),
        _list_page(["群C", "群D"]),
    ]
    agent, phone = _paged_agent(tmp_path, pages, start_page=0)
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    try:
        _drive_until_done(agent, phone, memory)
    except screen_agent.QQListNavigationError as exc:
        message = str(exc)
    else:
        raise AssertionError("到顶后仍未找到目标，应显式失败")

    assert "向上找回失败" in message
    assert "到达顶部" in message
    assert "已向下" in message and "向上" in message
    # 找回期间只滑动，不点任何入口（包括“展示最近不活跃的群”）
    assert phone.taps == []
    assert agent._ocr_nav_state.get("direction") == "up"


def test_ocr_list_navigation_reports_ambiguity_for_case_variant_only(tmp_path):
    pages = [
        _list_page(["llm小组", "群B"]),
        _list_page(["群C", "llm小组"]),
    ]
    agent, phone = _paged_agent(tmp_path, pages, start_page=0)
    memory = TaskMemory("打开QQ，滚动列表找到「LLM小组」")

    with pytest.raises(rule_policy.AmbiguousGroupName) as excinfo:
        _drive_until_done(agent, phone, memory)

    # 翻遍列表只有大小写不同的群：列出候选、不点任何行
    assert "llm小组" in excinfo.value.candidates
    assert phone.taps == []


def test_ocr_list_navigation_never_taps_structural_expander(tmp_path):
    agent = _agent(tmp_path, [
        {"text": "我创建的群聊 10/12", "center": (100, 200)},
        {"text": "展示最近不活跃的群(13)", "center": (332, 2108)},
        {"text": "群A", "center": (400, 600)},
    ])
    memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    action = agent._ocr_list_navigate(memory)

    assert action is not None
    assert action["action_type"] == "swipe"
    assert agent.phone.taps == []


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


def test_update_in_target_group_tracks_entry_and_exit():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = False
    agent.last_memory = TaskMemory(f"打开QQ，滚动列表找到「{TARGET}」")

    agent._update_in_target_group(
        {"action_type": "tap", "rule": {"name": "open_group"}, "target": TARGET})
    assert agent._in_target_group is True

    agent._update_in_target_group({"action_type": "back"})
    assert agent._in_target_group is False

    agent._update_in_target_group({"action_type": "tap", "target": DISTRACTOR})
    assert agent._in_target_group is False

    agent._update_in_target_group({"action_type": "tap", "target": TARGET})
    assert agent._in_target_group is True

    agent._update_in_target_group({"action_type": "open_app", "app_name": "QQ"})
    assert agent._in_target_group is False


def test_case_variant_model_tap_is_blocked_before_entry():
    """进群前模型按不区分大小写选了另一个群：必须拦下，不能替用户猜。"""
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = False
    memory = TaskMemory("打开微信，进入「LLM小组」读消息")

    assert agent._case_variant_conflict({"target": "llm小组"}, memory) == "llm小组"
    assert agent._case_variant_conflict({"target": "LLM小组"}, memory) == ""


def test_case_variant_tap_allowed_after_entering_group():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = True
    memory = TaskMemory("打开微信，进入「LLM小组」读消息")

    assert agent._case_variant_conflict({"target": "llm小组"}, memory) == ""


def test_verify_group_entry_rejects_wrong_chat_title():
    """点击记录说进了群，但标题不是目标群：该步观察不得计入来源。"""
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = True
    agent._last_rule_xml = (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "android17"
        / "qq_group_top.xml"
    ).read_text(encoding="utf-8")
    agent.tree_unsupported = False
    memory = TaskMemory(f"打开QQ，进入「{DISTRACTOR}」并读消息")

    assert agent._confirm_group_title(memory) is False
    assert agent._in_target_group is False
    assert agent._title_mismatch == "2026 计算机班级群"


def test_verify_group_entry_accepts_matching_chat_title():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = True
    agent._last_rule_xml = (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "android17"
        / "qq_group_top.xml"
    ).read_text(encoding="utf-8")
    agent.tree_unsupported = False
    memory = TaskMemory("打开QQ，进入「2026 计算机班级群」并读消息")

    assert agent._confirm_group_title(memory) is True
    assert agent._in_target_group is True
    assert agent._title_mismatch == ""


def test_verify_group_entry_keeps_behavior_when_title_unreadable():
    agent = object.__new__(screen_agent.ScreenAgent)
    agent._in_target_group = True
    agent._last_rule_xml = None
    agent.tree_unsupported = False

    assert agent._confirm_group_title(TaskMemory(f"进入「{TARGET}」")) is True
    assert agent._in_target_group is True


def test_qq_search_type_is_allowed_in_read_only():
    """搜索优先后，QQ 也要能在只读模式下往搜索框输入中文群名。"""
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.read_only = True
    calls = []

    class _Phone:
        def type_text(self, text, allow_search=False):
            calls.append((text, allow_search))

    agent.phone = _Phone()

    action = {
        "action_type": "type", "text": "计算机班级群",
        "search_input": True, "rule": {"name": "search_type"},
    }
    assert agent._execute(action) is False
    assert not action.get("blocked")
    assert calls == [("计算机班级群", True)]
