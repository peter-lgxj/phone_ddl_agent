"""规则快路：开群/返回/滚动/点已知输入框不调用视觉模型，且不绕过只读保护。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import config
import rule_policy
import screen_agent
import ui_state
from memory import TaskMemory

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "android17"
GROUPS_XML = (FIXTURE / "qq_groups_list.xml").read_text(encoding="utf-8")
CHAT_XML = (FIXTURE / "qq_group_top.xml").read_text(encoding="utf-8")


def make_ctx(xml: str, task: str, **kwargs) -> rule_policy.RuleContext:
    state = kwargs.pop("state", {})
    return rule_policy.RuleContext(
        task=task, step=1, read_only=kwargs.pop("read_only", False),
        xml=xml, state=state, **kwargs
    )


# ---------------------------------------------------------------- 规则单元
def test_extract_quoted_targets_supports_chinese_and_ascii_quotes():
    assert rule_policy.extract_quoted_targets(
        "在 QQ 中查找「COMP7503 作业通知群」里实验报告二的截止时间"
    ) == ["COMP7503 作业通知群"]
    assert rule_policy.extract_quoted_targets('打开 "G4" 与 “W1”') == ["G4", "W1"]
    assert rule_policy.extract_quoted_targets("没有引号的任务") == []


def test_open_group_taps_clickable_row_center():
    state = {}
    action = rule_policy.decide(make_ctx(
        GROUPS_XML, "在 QQ 中查找「COMP7503 作业通知群」里实验报告二的截止时间",
        state=state))

    assert action["action_type"] == "tap"
    assert action["rule"]["name"] == "open_group"
    assert (action["x"], action["y"]) == (540, 1375)
    assert state["entered_group"] == "COMP7503 作业通知群"


def test_similar_group_name_uses_exact_row_not_longer_one():
    action = rule_policy.decide(make_ctx(
        GROUPS_XML, "在 QQ 中查找「2026 计算机班级群」里实验报告二的截止时间"))

    assert action is not None
    assert (action["x"], action["y"]) == (540, 1697)


def test_missing_group_falls_back_to_vlm():
    action = rule_policy.decide(make_ctx(
        GROUPS_XML, "在 QQ 中查找「不存在的群」里实验报告二的截止时间"))
    assert action is None


def test_chat_page_with_input_box_never_taps_group_title():
    # 会话页标题「2026 计算机班级群」可点击且与任务群名一致；已有输入框时不得当群列表行点
    action = rule_policy.decide(make_ctx(
        CHAT_XML, "在「2026 计算机班级群」里找实验报告二的截止时间"))
    assert action is None


def test_title_bar_row_is_rejected_even_without_input_box():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="2026 计算机班级群" class="android.widget.TextView" '
        'clickable="true" bounds="[103,156][488,218]" />'
        '<node text="历史消息" class="android.widget.TextView" '
        'clickable="true" bounds="[100,900][900,1000]" />'
        '</node></hierarchy>'
    )
    assert rule_policy.decide(make_ctx(xml, "打开「2026 计算机班级群」")) is None


def test_scroll_only_after_entering_target_group():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 0}
    action = rule_policy.decide(make_ctx(CHAT_XML, "读取群消息", state=state))

    assert action["action_type"] == "swipe"
    assert action["rule"]["name"] == "scroll"
    assert (action["x"], action["y"]) == (540, 1780)
    assert (action["x2"], action["y2"]) == (540, 712)
    assert state["scrolls"] == 1


def test_scroll_stops_when_screen_stops_changing():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 0}
    ctx = make_ctx(CHAT_XML, "读取群消息", state=state, max_scrolls=6)

    assert rule_policy.decide(ctx) is not None       # 第一次：记录当前文字并滚动
    assert rule_policy.decide(ctx) is not None       # 文字未变化：再给一次机会
    assert rule_policy.decide(ctx) is None           # 连续两次未变化：交回 VLM
    assert state["scroll_disabled"] is True


def test_scroll_stops_at_message_end_marker():
    xml = CHAT_XML.replace("群里禁水，只发通知", "没有更多消息")
    state = {"entered_group": "2026 计算机班级群", "scrolls": 0}
    assert rule_policy.decide(make_ctx(xml, "读取群消息", state=state)) is None
    assert state["scroll_disabled"] is True


def test_scroll_stops_when_screen_already_has_answer_keyword():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 0}
    # 聊天页已经显示「实验报告二」「11.22 23:59」，再滚没有收益
    action = rule_policy.decide(make_ctx(
        CHAT_XML, "在「2026 计算机班级群」里找实验报告二的截止时间", state=state))

    assert action is None
    assert state["scroll_disabled"] is True
    assert state["scroll_stop_keyword"] in ("实验报告二", "截止")


def test_task_keywords_excludes_navigation_group_name():
    words = rule_policy.task_keywords(
        "在 QQ 中查找「COMP7503 作业通知群」里实验报告二的截止时间")
    assert "实验报告二" in words
    assert "截止" in words
    assert "作业通知群" not in words


def test_scroll_respects_default_max_scrolls_of_two():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 2}
    assert rule_policy.decide(make_ctx(CHAT_XML, "读取群消息", state=state)) is None


def test_scroll_respects_max_scrolls():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 6}
    assert rule_policy.decide(make_ctx(CHAT_XML, "读取群消息", state=state,
                                       max_scrolls=6)) is None


def test_input_box_rule_is_skipped_in_read_only():
    xml = (
        '<hierarchy><node text="搜索" class="android.widget.EditText" '
        'resource-id="com.tencent.mm:id/b4z" bounds="[100,200][900,300]" '
        'clickable="true"/></hierarchy>'
    )
    assert rule_policy.decide(make_ctx(xml, "搜索「COMP7503」", read_only=True)) is None
    action = rule_policy.decide(make_ctx(xml, "搜索「COMP7503」", read_only=False))
    assert action["action_type"] == "tap"
    assert action["rule"]["name"] == "tap_input"
    assert (action["x"], action["y"]) == (500, 250)


def test_back_after_localization_miss():
    state = {"entered_group": "2026 计算机班级群"}
    action = rule_policy.decide(make_ctx(
        CHAT_XML, "读取群消息", state=state,
        last_action={"action_type": "wait", "localization_failed": True}))

    assert action["action_type"] == "back"
    assert action["rule"]["name"] == "back_after_miss"
    assert state["entered_group"] is None


def test_rule_never_emits_mutating_actions():
    with pytest.raises(ValueError):
        rule_policy._action("send", "bad", "不该发生")


# ---------------------------------------------------------------- 集成
class _StubPhone:
    def __init__(self, xmls: list[str]):
        self.xmls = list(xmls)
        self.calls: list[tuple] = []
        self.screenshot_calls = 0

    def dump_hierarchy(self) -> str:
        self.calls.append(("dump",))
        return self.xmls.pop(0) if len(self.xmls) > 1 else self.xmls[0]

    def screenshot(self, save_path="screen.png"):
        self.screenshot_calls += 1
        Image.new("RGB", (1080, 2374), (20, 20, 20)).save(save_path)

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def swipe(self, x1, y1, x2, y2):
        self.calls.append(("swipe", x1, y1, x2, y2))

    def press_back(self):
        self.calls.append(("back",))


class _ExplodingClient:
    """规则命中时不允许发生任何模型调用。"""

    def __init__(self):
        def explode(**_kwargs):
            raise AssertionError("规则快路命中后不应调用视觉模型")
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=explode))


def _make_agent(tmp_path, monkeypatch, xmls):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "RULE_FASTPATH", True)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.0)
    (tmp_path / "screen_current.png").write_bytes(b"")
    ui_state.init_state("规则快路")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = _ExplodingClient()
    agent.model = "fake"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = _StubPhone(xmls)
    agent.ocr = SimpleNamespace()
    agent.read_only = True
    agent.run_dir = tmp_path / "run"
    agent.run_dir.mkdir()
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


def test_two_rule_steps_make_no_model_calls(tmp_path, monkeypatch):
    agent = _make_agent(
        tmp_path, monkeypatch,
        [GROUPS_XML, CHAT_XML, CHAT_XML, CHAT_XML],
    )
    memory = TaskMemory("查看「COMP7503 作业通知群」里的群消息")

    agent._execute_step(1, memory)
    agent._execute_step(2, memory)

    assert ("tap", 540, 1375) in agent.phone.calls
    assert ("swipe", 540, 1780, 540, 712) in agent.phone.calls
    step1 = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    step2 = json.loads((agent.run_dir / "step_002.json").read_text(encoding="utf-8"))
    assert step1["timings"]["rule_name"] == "open_group"
    assert step1["timings"]["rule_saved_calls"] == 1
    assert step2["timings"]["rule_name"] == "scroll"
    assert step2["observation"].startswith("[规则]")

    summary = agent._usage_summary(len(memory.steps))
    assert summary["rule_fastpath"] == {
        "hits": 2, "saved_calls": 2, "by_rule": {"open_group": 1, "scroll": 1}
    }


def test_read_only_still_blocks_mutating_actions_after_rule_hit(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, [GROUPS_XML, CHAT_XML])
    agent.read_only = True
    for action_type in ("send", "press_enter", "type"):
        action = {"action_type": action_type, "text": "不要发送"}
        assert agent._execute(action) is False
        assert action["blocked"] is True
        assert "只读" in action["blocked_reason"]
