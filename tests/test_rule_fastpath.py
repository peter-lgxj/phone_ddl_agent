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


def test_extract_quoted_targets_prefers_group_brackets_over_ui_path():
    """任务文案同时引用界面路径和目标群时，只把「」里的群名当目标。"""
    task = (
        "打开QQ。先看首屏消息列表或“联系人 > 群聊”里有没有「逗鱼时刻素材3群」："
        "有就直接点它进入群聊；没有就用顶部搜索框输入「逗鱼时刻素材3群」。"
    )
    assert rule_policy.extract_quoted_targets(task) == ["逗鱼时刻素材3群"]


def test_extract_quoted_targets_falls_back_to_other_quotes():
    assert rule_policy.extract_quoted_targets('进入“COMP7503 作业通知群”读消息') == [
        "COMP7503 作业通知群"
    ]


def test_chat_title_reads_group_name_from_top_bar():
    """进群后标题校验：会话页标题栏读到的就是群名，人数后缀不算。"""
    assert rule_policy.chat_title(CHAT_XML) == "2026 计算机班级群"


def test_chat_title_ignores_message_list_page():
    """列表页没有会话标题；不能把「消息/联系人/搜索」当群名。"""
    assert rule_policy.chat_title(GROUPS_XML) == ""


def test_chat_title_keeps_case_variant():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="llm小组" clickable="true" bounds="[103,156][488,218]" />'
        '<node text="" content-desc="返回消息" clickable="true" bounds="[27,154][92,219]" />'
        '</node></hierarchy>'
    )
    title = rule_policy.chat_title(xml)
    assert title == "llm小组"
    # 目标是「LLM小组」时，大小写不同的标题不能算命中
    assert rule_policy.label_matches_target(title, "LLM小组") is False


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


def test_missing_group_falls_back_to_search():
    state = {}
    action = rule_policy.decide(make_ctx(
        GROUPS_XML, "在 QQ 中查找「不存在的群」里实验报告二的截止时间", state=state))
    # 目标不在可见行且有搜索入口：优先搜索，而不是让模型反复滑动列表
    assert action["rule"]["name"] == "search_entry"
    assert state["search_stage"] == "entry"


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
    assert (action["x"], action["y"]) == (540, 1946)
    assert (action["x2"], action["y2"]) == (540, 474)
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


def test_scroll_respects_default_max_scrolls():
    """翻页是只读可判定动作：默认上限放宽到 30，不再两屏就交回模型。"""
    state = {"entered_group": "2026 计算机班级群", "scrolls": 30}
    assert rule_policy.decide(make_ctx(CHAT_XML, "读取群消息", state=state)) is None


def test_scroll_keeps_going_after_two_screens():
    state = {"entered_group": "2026 计算机班级群", "scrolls": 2,
             "last_scroll_text": "上一屏不同的文字"}
    action = rule_policy.decide(make_ctx(CHAT_XML, "读取群消息", state=state))
    assert action is not None
    assert action["rule"]["name"] == "scroll"
    assert state["scrolls"] == 3


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


def test_tap_hits_send_detects_coords_and_label():
    # 聊天页发送按钮 bounds=[843,2079][1037,2192]
    assert rule_policy.tap_hits_send(CHAT_XML, 940, 2135) == "coords"
    assert rule_policy.tap_hits_send(CHAT_XML, 540, 1375) == ""
    assert rule_policy.tap_hits_send(CHAT_XML, 540, 1375, "发送") == "target_text"


def test_rule_refuses_tap_inside_send_region():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="2026 计算机班级群" class="android.widget.TextView" '
        'clickable="true" bounds="[843,2079][1037,2192]" />'
        '<node text="发送" resource-id="com.tencent.mobileqq:id/send_btn" '
        'class="android.widget.Button" clickable="true" '
        'bounds="[843,2079][1037,2192]" />'
        '</node></hierarchy>'
    )
    state = {}
    assert rule_policy.decide(make_ctx(
        xml, "打开「2026 计算机班级群」", state=state)) is None
    assert state["rejected_tap"] == "coords"


SEARCH_ENTRY_XML = (
    '<hierarchy><node bounds="[0,0][1080,2374]">'
    '<node text="搜索" class="android.view.ViewGroup" clickable="true" '
    'bounds="[0,244][1080,405]" />'
    '<node text="群聊" class="android.widget.TextView" clickable="true" '
    'bounds="[0,900][1080,1000]" />'
    '</node></hierarchy>'
)

SEARCH_INPUT_XML = (
    '<hierarchy><node bounds="[0,0][1080,2374]">'
    '<node text="搜索指定内容" resource-id="com.tencent.mobileqq:id/kbs" '
    'class="android.widget.TextView" clickable="false" '
    'bounds="[43,386][1037,437]" />'
    '</node></hierarchy>'
)

SEARCH_RESULT_XML = (
    '<hierarchy><node bounds="[0,0][1080,2374]">'
    '<node text="COMP7503 作业通知群" class="android.widget.TextView" '
    'clickable="true" bounds="[0,700][1080,820]" />'
    '</node></hierarchy>'
)


def test_target_not_visible_prefers_search_entry():
    state = {}
    action = rule_policy.decide(make_ctx(
        SEARCH_ENTRY_XML, "打开「COMP7503 作业通知群」", state=state))

    assert action["rule"]["name"] == "search_entry"
    assert (action["x"], action["y"]) == (540, 324)
    assert state["search_stage"] == "entry"


def test_search_entry_types_exact_name_into_search_box():
    state = {"search_stage": "entry", "search_attempted": True}
    action = rule_policy.decide(make_ctx(
        SEARCH_INPUT_XML, "打开「COMP7503 作业通知群」", state=state))

    assert action["action_type"] == "type"
    assert action["rule"]["name"] == "search_type"
    assert action["text"] == "COMP7503 作业通知群"
    assert action["search_input"] is True
    assert action["resource_id"] == "com.tencent.mobileqq:id/kbs"
    assert state["search_stage"] == "result"


def test_search_ascii_fallback_on_adb_dump():
    state = {"search_stage": "entry", "search_attempted": True}
    action = rule_policy.decide(rule_policy.RuleContext(
        task="打开「COMP7503 作业通知群」", step=1, read_only=True,
        xml=SEARCH_INPUT_XML, state=state, search_ascii_only=True))

    # adb_dump 的 input text 不支持中文：用名称里的 ASCII 唯一片段搜索
    assert action["text"] == "COMP7503"
    assert action["target"] == "COMP7503 作业通知群"


def test_force_search_skips_visible_row():
    state = {}
    action = rule_policy.decide(rule_policy.RuleContext(
        task="打开「COMP7503 作业通知群」", step=1, read_only=True,
        xml=GROUPS_XML, state=state, search_force=True))

    assert action["rule"]["name"] == "search_entry"


def test_search_disabled_falls_back_to_list_scroll():
    state = {}
    action = rule_policy.decide(rule_policy.RuleContext(
        task="打开「COMP7503 作业通知群」", step=1, read_only=True,
        xml=SEARCH_ENTRY_XML, state=state, search_allowed=False))

    # 搜索不可用时才降级为滚动列表；滚动过程只滑不点
    assert action["rule"]["name"] == "list_scroll"
    assert action["action_type"] == "swipe"
    assert state.get("search_stage") is None


def test_search_result_taps_unique_exact_row():
    state = {"search_stage": "result", "search_attempted": True}
    action = rule_policy.decide(make_ctx(
        SEARCH_RESULT_XML, "打开「COMP7503 作业通知群」", state=state))

    assert action["rule"]["name"] == "search_result"
    assert (action["x"], action["y"]) == (540, 760)
    assert state["entered_group"] == "COMP7503 作业通知群"


def test_search_miss_backs_out_before_list_fallback():
    state = {"search_stage": "result", "search_attempted": True}
    action = rule_policy.decide(make_ctx(
        GROUPS_XML, "打开「不存在的群」", state=state))

    # 搜索无结果先退回列表，下一步才滚动兜底；这里不能点任何行
    assert action["rule"]["name"] == "search_back"
    assert action["action_type"] == "back"
    assert state["search_failed"] == "no_result"
    assert state["search_backs"] == 1


def test_search_result_prefers_exact_name_over_similar_group():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="2026 计算机班级群" clickable="true" bounds="[0,700][1080,800]" />'
        '<node text="2026 计算机班级二群" clickable="true" bounds="[0,820][1080,920]" />'
        '</node></hierarchy>'
    )
    state = {"search_stage": "result", "search_attempted": True}
    action = rule_policy.decide(make_ctx(xml, "打开「2026 计算机班级群」", state=state))

    # 逐字符一致的候选优先，近似名「二群」不能被选中
    assert action["rule"]["name"] == "search_result"
    assert (action["x"], action["y"]) == (540, 750)
    assert state["entered_group"] == "2026 计算机班级群"


def test_search_result_taps_exact_name_not_case_variant():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="llm小组" clickable="true" bounds="[0,700][1080,800]" />'
        '<node text="LLM小组" clickable="true" bounds="[0,820][1080,920]" />'
        '</node></hierarchy>'
    )
    state = {"search_stage": "result", "search_attempted": True}
    action = rule_policy.decide(make_ctx(xml, "打开「LLM小组」", state=state))

    assert action["rule"]["name"] == "search_result"
    assert (action["x"], action["y"]) == (540, 870)
    assert state["entered_group"] == "LLM小组"


def test_search_result_reports_ambiguity_for_case_variant_only():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="llm小组" clickable="true" bounds="[0,700][1080,800]" />'
        '</node></hierarchy>'
    )
    state = {"search_stage": "result", "search_attempted": True}
    with pytest.raises(rule_policy.AmbiguousGroupName) as excinfo:
        rule_policy.decide(make_ctx(xml, "打开「LLM小组」", state=state))

    # 只有大小写不同的结果时列出候选，绝不自动点
    assert excinfo.value.target == "LLM小组"
    assert excinfo.value.candidates == ["llm小组"]
    assert state.get("entered_group") is None


def test_search_result_reports_ambiguity_for_similar_name_only():
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="2026 计算机班级二群" clickable="true" bounds="[0,700][1080,800]" />'
        '</node></hierarchy>'
    )
    state = {"search_stage": "result", "search_attempted": True}
    with pytest.raises(rule_policy.AmbiguousGroupName) as excinfo:
        rule_policy.decide(make_ctx(xml, "打开「2026 计算机班级群」", state=state))

    assert "2026 计算机班级二群" in excinfo.value.candidates
    assert state.get("entered_group") is None


def test_label_matches_target_is_case_sensitive_and_ignores_preview():
    assert rule_policy.label_matches_target("超商书友群, ,253条未读,预览", "超商书友群")
    assert rule_policy.label_matches_target("超商书友群(3)", "超商书友群")
    assert not rule_policy.label_matches_target("超商书友群2", "超商书友群")
    assert not rule_policy.label_matches_target("llm小组", "LLM小组")
    assert not rule_policy.label_matches_target("2026 计算机班级二群", "2026 计算机班级群")


def test_qq_row_with_preview_text_is_exact_group():
    """QQ 会话行在 adb dump 里是「群名、未读数、预览」拼成的一个节点。"""
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="超商书友群, ,253条未读,张万湑: 明天交,00:12" clickable="true" '
        'bounds="[0,300][1080,420]" />'
        '</node></hierarchy>'
    )

    action = rule_policy.decide(make_ctx(xml, "打开「超商书友群」"))

    assert action["rule"]["name"] == "open_group"
    assert (action["x"], action["y"]) == (540, 360)
    assert action["target"] == "超商书友群"


def test_similar_row_is_never_tapped_when_search_available():
    """首屏只有近似名时不点它，先走搜索。"""
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="搜索" clickable="true" bounds="[0,80][1080,160]" />'
        '<node text="2026 计算机班级二群" clickable="true" bounds="[0,700][1080,800]" />'
        '</node></hierarchy>'
    )
    state = {}

    action = rule_policy.decide(make_ctx(xml, "打开「2026 计算机班级群」", state=state))

    assert action["rule"]["name"] == "search_entry"
    assert state["search_stage"] == "entry"
    assert state.get("entered_group") is None


def test_case_variant_row_is_never_tapped_when_search_available():
    """「LLM小组」与「llm小组」并存时，目标写「LLM小组」不能点成小写那个。"""
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="搜索" clickable="true" bounds="[0,80][1080,160]" />'
        '<node text="llm小组, ,3条未读,10:12" clickable="true" bounds="[0,700][1080,800]" />'
        '</node></hierarchy>'
    )
    state = {}

    action = rule_policy.decide(make_ctx(xml, "打开「LLM小组」", state=state))

    assert action["rule"]["name"] == "search_entry"
    assert state.get("entered_group") is None


def test_list_fallback_scrolls_without_tapping_non_target_rows():
    """搜索不可用时滚动兜底：只滑不点，非目标群永远拿不到点击。"""
    xml = (
        '<hierarchy><node bounds="[0,0][1080,2374]">'
        '<node text="搜索" clickable="true" bounds="[0,80][1080,160]" />'
        '<node text="2026 计算机班级二群" clickable="true" bounds="[0,700][1080,800]" />'
        '<node text="llm小组" clickable="true" bounds="[0,900][1080,1000]" />'
        '</node></hierarchy>'
    )
    ctx = rule_policy.RuleContext(
        task="打开「2026 计算机班级群」", step=1, read_only=True,
        xml=xml, search_allowed=False, list_max_scrolls=3,
    )

    actions = [rule_policy.decide(ctx) for _ in range(3)]

    assert all(item is None or item["action_type"] == "swipe" for item in actions)
    assert actions[0]["rule"]["name"] == "list_scroll"
    assert ctx.state["list_failed"] in {"max_scrolls", "stagnant"}


def test_rule_action_allows_search_type_but_rejects_message_type(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, [SEARCH_INPUT_XML])
    agent._rule_state = {"search_stage": "entry", "search_attempted": True}
    memory = TaskMemory("打开「COMP7503 作业通知群」")
    action = agent._rule_action(memory)

    assert action["action_type"] == "type"
    agent._execute(action)
    assert action.get("blocked") is not True
    assert ("type", "COMP7503 作业通知群", True) in agent.phone.calls

    # 同样的 type 但不在搜索框（消息输入框/无 search_type 标记）必须拒绝
    agent.phone.xmls = [CHAT_XML]
    blocked = {"action_type": "type", "text": "不要发送"}
    assert agent._execute(blocked) is False
    assert blocked["blocked"] is True


def test_search_type_asserts_current_screen_is_search_box(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, [CHAT_XML])
    monkeypatch.setattr(rule_policy, "decide", lambda ctx: {
        "action_type": "type", "text": "COMP7503 作业通知群",
        "search_input": True, "resource_id": "com.tencent.mobileqq:id/kbs",
        "rule": {"name": "search_type"},
    })
    memory = TaskMemory("打开「COMP7503 作业通知群」")

    with pytest.raises(AssertionError, match="不是搜索框"):
        agent._rule_action(memory)


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

    def type_text(self, text, allow_search=False):
        self.calls.append(("type", text, allow_search))


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


def test_tree_only_switch_enables_rule_fastpath_without_screenshot(tmp_path, monkeypatch):
    """非合并模式 + TREE_ONLY_DECISION：控件树能证明的步骤不截图、不调视觉模型。"""
    agent = _make_agent(
        tmp_path, monkeypatch,
        [GROUPS_XML, GROUPS_XML, GROUPS_XML],
    )
    monkeypatch.setattr(config, "MERGED_OBSERVE", False)
    monkeypatch.setattr(config, "TREE_ONLY_DECISION", True)
    memory = TaskMemory("查看「COMP7503 作业通知群」里的群消息")

    agent._execute_step(1, memory)

    step1 = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    assert step1["timings"]["rule_hit"] is True
    assert step1["timings"]["tree_only"] is True
    assert agent.phone.screenshot_calls == 0


def test_two_rule_steps_make_no_model_calls(tmp_path, monkeypatch):
    agent = _make_agent(
        tmp_path, monkeypatch,
        [GROUPS_XML, CHAT_XML, CHAT_XML, CHAT_XML],
    )
    memory = TaskMemory("查看「COMP7503 作业通知群」里的群消息")

    agent._execute_step(1, memory)
    agent._execute_step(2, memory)

    assert ("tap", 540, 1375) in agent.phone.calls
    assert ("swipe", 540, 1946, 540, 474) in agent.phone.calls
    step1 = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    step2 = json.loads((agent.run_dir / "step_002.json").read_text(encoding="utf-8"))
    assert step1["timings"]["rule_name"] == "open_group"
    assert step1["timings"]["rule_saved_calls"] == 1
    assert step2["timings"]["rule_name"] == "scroll"
    assert step2["observation"].startswith("[规则]")

    summary = agent._usage_summary(len(memory.steps))
    fastpath = summary["rule_fastpath"]
    assert fastpath["hits"] == 2
    assert fastpath["saved_calls"] == 2
    assert fastpath["by_rule"] == {"open_group": 1, "scroll": 1}
    assert fastpath["rule_hit_steps"] == 2
    assert fastpath["rule_step_seconds"] > 0
    assert 0 < fastpath["rule_time_ratio"] <= 1


# ---------------------------------------------------------------- 成本核算
def _cost_agent(monkeypatch, driver: str, mode: str):
    monkeypatch.setattr(config, "RULE_FASTPATH_MODE", mode)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.driver_kind = driver
    return agent


def test_adb_dump_cost_enables_rule_fastpath_after_light_observe(monkeypatch):
    agent = _cost_agent(monkeypatch, "adb_dump", "auto")
    enabled, cost = agent._resolve_rule_fastpath()

    # 2 x 2.5s dump + 0.3s 动作 = 5.3s <= 一次模型决策 5.6s（实测模型步 5.20–5.58s）
    assert enabled is True
    assert cost["rule_step_estimate"] == 5.3
    assert cost["rule_step_estimate_with_screenshot"] == 6.3
    assert cost["dump_seconds"] == 2.5
    assert cost["driver"] == "adb_dump"


def test_uiautomator2_keeps_rule_fastpath_enabled(monkeypatch):
    agent = _cost_agent(monkeypatch, "uiautomator2", "auto")
    enabled, cost = agent._resolve_rule_fastpath()

    assert enabled is True
    assert cost["rule_step_estimate"] == 0.9


def test_explicit_switch_overrides_cost_estimate(monkeypatch):
    agent = _cost_agent(monkeypatch, "adb_dump", "on")
    assert agent._resolve_rule_fastpath()[0] is True

    agent = _cost_agent(monkeypatch, "uiautomator2", "off")
    assert agent._resolve_rule_fastpath()[0] is False


def test_read_only_still_blocks_mutating_actions_after_rule_hit(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, [GROUPS_XML, CHAT_XML])
    agent.read_only = True
    for action_type in ("send", "press_enter", "type"):
        action = {"action_type": action_type, "text": "不要发送"}
        assert agent._execute(action) is False
        assert action["blocked"] is True
        assert "只读" in action["blocked_reason"]


def test_rule_action_asserts_tap_never_lands_on_send(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, [CHAT_XML])
    monkeypatch.setattr(rule_policy, "decide", lambda ctx: {
        "action_type": "tap", "x": 940, "y": 2135, "target": "发送",
        "rule": {"name": "bad"},
    })
    memory = TaskMemory("查看「2026 计算机班级群」里的群消息")

    with pytest.raises(AssertionError, match="发送按钮区域"):
        agent._rule_action(memory)
