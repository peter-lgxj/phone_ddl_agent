"""观察结构化、D06 置顶确认、D13 时间与 needs_review 对齐的离线回归。"""

from __future__ import annotations

import json
from datetime import datetime

from PIL import Image

import config
import screen_agent
import ui_state
from ddl_agent.connectors.mobile_reading import build_mobile_messages, split_observation
from ddl_agent.extract.rules import RuleExtractor
from ddl_agent.models import RawMessage
from ddl_agent.pipeline import DdlAgent
from ddl_agent.settings import TZ
from memory import TaskMemory

NOW = datetime(2026, 11, 22, 17, 0, tzinfo=TZ)


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Completions:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False)
        self.calls = 0
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        return _Response(self.payload)


class _Client:
    def __init__(self, payload):
        self.chat = type("Chat", (), {"completions": _Completions(payload)})()


class _Phone:
    def screenshot(self, save_path="screen.png"):
        Image.new("RGB", (1080, 2374), (30, 30, 30)).save(save_path)

    def swipe(self, *_args):
        pass

    def tap(self, *_args):
        pass


class _OCR:
    def detect_tree(self):
        return []

    def detect_ocr(self, _path):
        return []


def _agent(tmp_path, monkeypatch, payload):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "RULE_FASTPATH", False)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(config, "OBSERVE_OCR_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    ui_state.init_state("观察结构化")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = _Client(payload)
    agent.model = "fake"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = _Phone()
    agent.ocr = _OCR()
    agent.read_only = True
    agent.run_dir = tmp_path
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
    agent._last_frame_hash = None
    agent._last_frame_bytes = None
    agent._last_frame_meta = None
    agent._pending_settle_frame = None
    agent.usage_events = []
    agent._last_step_usage = []
    return agent


# ---------------------------------------------------------------- 观察结构化
def test_merged_observation_returns_structured_messages_in_one_request(tmp_path, monkeypatch):
    payload = {
        "messages": [
            {"sender": "叶小凡", "time": "未显示",
             "body": "小组作业提交：11.20 23:59 前把 report 传到共享文件夹"},
            {"sender": "TA", "time": "2026-11-19 10:00", "body": "作业三 11.29 23:59"},
        ],
        "observation": "群聊界面",
        "action_type": "wait",
        "description": "保持当前界面",
    }
    agent = _agent(tmp_path, monkeypatch, payload)

    action = agent._think_and_act(TaskMemory("读群消息"), merged=True)

    completions = agent.client.chat.completions
    assert completions.calls == 1
    content = completions.requests[0]["messages"][0]["content"]
    assert len([item for item in content if item["type"] == "image_url"]) == 1
    assert agent._last_observation_format == "structured"
    assert len(agent._last_observation_messages) == 2
    assert "发送者：叶小凡 时间：未显示 原文：小组作业提交" in agent._last_merged_observation
    assert "发送者：TA 时间：2026-11-19 10:00 原文：作业三" in agent._last_merged_observation
    assert "messages" not in action


def test_merged_structured_step_still_makes_one_llm_call(tmp_path, monkeypatch):
    payload = {
        "messages": [
            {"sender": "叶小凡", "time": "未显示",
             "body": "小组作业提交：11.20 23:59 前把 report 传到共享文件夹"},
        ],
        "observation": "群聊界面",
        "action_type": "wait",
        "description": "保持当前界面",
    }
    agent = _agent(tmp_path, monkeypatch, payload)
    memory = TaskMemory("读群消息")

    agent._execute_step(1, memory)

    assert agent.client.chat.completions.calls == 1
    assert "发送者：叶小凡 时间：未显示 原文：小组作业提交" in (
        memory.get_last_observation()
    )


def test_finish_step_keeps_structured_messages(tmp_path, monkeypatch):
    """finish + messages 同一步：消息不能被「任务完成」覆盖。"""
    payload = {
        "messages": [
            {"sender": "eNotices System", "time": "06:33",
             "body": "HKG Daily Notices (22 NOV 2026)"},
            {"sender": "Huang Chao (via hkumoodle)", "time": "昨天",
             "body": "COMP7607_1A_2026: Course Project Pre..."},
        ],
        "observation": "Outlook 收件箱可见 2 封邮件",
        "action_type": "finish",
        "summary": "已读取收件箱邮件",
        "task_done": True,
    }
    agent = _agent(tmp_path, monkeypatch, payload)
    memory = TaskMemory("读 Outlook 邮件")

    finished, _done, _summary = agent._execute_step(1, memory)

    assert finished is True
    observation = memory.get_last_observation()
    assert "发送者：eNotices System 时间：06:33" in observation
    assert "发送者：Huang Chao (via hkumoodle) 时间：昨天" in observation
    assert "[完成] 任务完成" in observation

    archived = json.loads((tmp_path / "step_001.json").read_text(encoding="utf-8"))
    assert "发送者：eNotices System 时间：06:33" in archived["observation"]
    assert len(archived["observation_messages"]) == 2
    assert archived["observation_format"] == "structured"

    agent.last_memory = memory
    messages = build_mobile_messages(
        agent, source_name="outlook:inbox", source_type="official_email",
        subject="Outlook", thread_id="outlook", now=NOW,
    )
    assert [message.body for message in messages] == [
        "HKG Daily Notices (22 NOV 2026)",
        "COMP7607_1A_2026: Course Project Pre...",
    ]


def test_build_mobile_messages_skips_list_phase_observations(tmp_path):
    """进入目标群之前看到的列表/搜索页内容不能算成该群的消息。"""
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.run_dir = tmp_path
    memory = TaskMemory("打开「超商书友群」，最近7天")
    memory.steps = [
        {
            "step": 1, "action_type": "swipe", "in_target_group": False,
            "observation": "发送者：张万湑 时间：00:12 原文：别的群的预览",
        },
        {
            "step": 2, "action_type": "tap", "in_target_group": True,
            "observation": "发送者：李四 时间：09:00 原文：目标群的DDL",
        },
    ]
    agent.last_memory = memory

    messages = build_mobile_messages(
        agent, source_name="QQ / 超商书友群", source_type="im_group",
        subject="QQ / 超商书友群", thread_id="qq-shangchao", now=NOW,
    )

    assert [message.body for message in messages] == ["目标群的DDL"]


def test_finish_without_messages_keeps_completion_text(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, {
        "observation": "",
        "action_type": "finish",
        "summary": "完成",
        "task_done": True,
    })
    memory = TaskMemory("读群消息")

    finished, _done, _summary = agent._execute_step(1, memory)

    assert finished is True
    assert memory.get_last_observation() == "任务完成"
    archived = json.loads((tmp_path / "step_001.json").read_text(encoding="utf-8"))
    assert archived["observation"] == "任务完成"
    assert "observation_messages" not in archived


def test_non_finish_step_does_not_get_completion_marker(tmp_path, monkeypatch):
    payload = {
        "messages": [
            {"sender": "叶小凡", "time": "未显示", "body": "作业三 11.29 23:59"},
        ],
        "observation": "群聊界面",
        "action_type": "wait",
        "description": "保持当前界面",
    }
    agent = _agent(tmp_path, monkeypatch, payload)
    memory = TaskMemory("读群消息")

    finished, _done, _summary = agent._execute_step(1, memory)

    assert finished is False
    observation = memory.get_last_observation()
    assert "发送者：叶小凡 时间：未显示 原文：作业三 11.29 23:59" == observation
    assert "[完成]" not in observation


def test_build_mobile_messages_prefers_archived_observation_messages(tmp_path):
    """下游不再依赖 observation：即使它只有「任务完成」，也能还原消息。"""
    steps = [{
        "step": 2,
        "observation": "任务完成",
        "observation_messages": [
            {"sender": "eNotices System", "time": "06:33", "body": "第一封"},
            {"sender": "careers@cedars.hku.hk", "time": "周五", "body": "第二封"},
        ],
    }]
    agent = type("Agent", (), {
        "last_memory": type("Memory", (), {"steps": steps})(),
        "run_dir": str(tmp_path),
    })()

    messages = build_mobile_messages(
        agent, source_name="outlook:inbox", source_type="official_email",
        subject="Outlook", thread_id="outlook", now=NOW,
    )

    assert [message.body for message in messages] == ["第一封", "第二封"]
    assert messages[0].published_time == "06:33"
    assert messages[0].published_at is None


def test_build_mobile_messages_dedupes_same_message_across_steps(tmp_path):
    """翻页后同一条消息会出现在不同步：id 不能带步号/行号，否则重复入库。"""
    steps = [
        {
            "step": 1,
            "observation": "群聊",
            "observation_messages": [
                {"sender": "叶小凡", "time": "昨天 20:00", "body": "作业三 11.29 23:59"},
            ],
        },
        {
            "step": 2,
            "observation": "继续向上",
            "observation_messages": [
                {"sender": "叶小凡", "time": "昨天 20:00", "body": "作业三 11.29 23:59"},
                {"sender": "TA", "time": "昨天 18:00", "body": "作业二 11.25 23:59"},
            ],
        },
    ]
    agent = type("Agent", (), {
        "last_memory": type("Memory", (), {"steps": steps})(),
        "run_dir": str(tmp_path),
    })()

    messages = build_mobile_messages(
        agent, source_name="微信 / 21级课程群", source_type="student_chat",
        subject="微信 / 21级课程群", thread_id="wechat-a", now=NOW,
    )

    assert [message.body for message in messages] == [
        "作业三 11.29 23:59",
        "作业二 11.25 23:59",
    ]
    assert len({message.message_id for message in messages}) == 2


def test_merged_prose_without_messages_archives_fallback_marker(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, {
        "observation": "群里有 11.20 的置顶消息",
        "action_type": "wait",
        "description": "保持当前界面",
    })

    action = agent._think_and_act(TaskMemory("读群消息"), merged=True)
    agent._last_step_timings = {}
    agent._archive_step(1, action, agent._last_merged_observation)

    archived = json.loads((tmp_path / "step_001.json").read_text(encoding="utf-8"))
    assert archived["observation_format"] == "prose_fallback"
    assert "缺少 messages" in archived["observation_format_reason"]
    assert agent._last_observation_messages is None


def test_observation_messages_missing_field_is_format_failure(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, {
        "messages": [{"sender": "叶小凡", "body": "小组作业 11.20 23:59"}],
        "observation": "缺了时间字段",
        "action_type": "wait",
        "description": "保持当前界面",
    })

    agent._think_and_act(TaskMemory("读群消息"), merged=True)

    assert agent._last_observation_format == "prose_fallback"
    assert "缺少字段" in agent._last_observation_format_reason
    assert "time" in agent._last_observation_format_reason


def test_structured_absent_time_is_blank_not_review_but_prose_is_review():
    structured = split_observation(
        "发送者：未显示 时间：未显示 原文：机器学习作业三，11.29 23:59 前交到 Moodle",
        now=NOW,
    )[0]
    assert structured.published_at is None
    assert structured.needs_review is False
    assert "未读到时间" in structured.review_reason

    prose = split_observation("群里有 11.20 的置顶消息", now=NOW)[0]
    assert prose.needs_review is True
    assert prose.published_at is None


def test_structured_real_time_is_written_and_missing_sender_not_blocking():
    record = split_observation(
        "发送者：未显示 时间：2026-11-19 10:00 原文：作业三 11.29 23:59",
        now=NOW,
    )[0]
    assert record.published_at.isoformat() == "2026-11-19T10:00:00+08:00"
    assert record.needs_review is False
    assert "未读到发送者" in record.review_reason


# ---------------------------------------------------------------- D06
class _CapturedSource:
    def __init__(self, messages):
        self.name = "LLM小组(5)"
        self.source_type = "student_chat"
        self.kind = "chat"
        self.messages = messages

    def fetch(self, _plan=None):
        return list(self.messages)


class _DropPinned:
    """模拟 LLM 完全漏抽取顶消息；规则兜底应补回。"""

    def extract(self, messages, now=None):
        return [
            candidate for candidate in RuleExtractor().extract(messages, now=now)
            if candidate.message_id != "pin"
        ]


def test_d06_pinned_recovered_deadline_is_confirmed_not_uncertain():
    messages = [
        RawMessage("pin", "LLM小组(5)", "student_chat", "叶小凡", None,
                   "微信 / LLM小组(5)",
                   "小组作业提交：11.20 23:59 前把 report 传到共享文件夹，命名 组号_姓名.pdf",
                   needs_review=True),
        RawMessage("banner", "LLM小组(5)", "student_chat", "", None,
                   "微信 / LLM小组(5)", "“叶小凡”置顶了一条消息"),
        RawMessage("n1", "LLM小组(5)", "student_chat", "", None,
                   "微信 / LLM小组(5)", "作业三再提醒一次，11.29 23:59，别拖"),
        RawMessage("n2", "LLM小组(5)", "student_chat", "", None,
                   "微信 / LLM小组(5)", "课程报告最终提交截止 12.05 23:59"),
    ]
    agent = DdlAgent(
        sources=[_CapturedSource(messages)],
        extractor=_DropPinned(),
        now=NOW,
    )

    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert [record.deadline_iso for record in result.records] == [
        "2026-11-20T23:59:00+08:00"
    ]
    assert result.records[0].status == "confirmed"
    assert result.trace["focus_filter"]["pinned_filter"] == "applied"
    assert result.needs_review is False


def test_d06_missing_sender_does_not_downgrade_pinned_target():
    message = RawMessage(
        "pin", "LLM小组(5)", "student_chat", "", None,
        "微信 / LLM小组(5)",
        "小组作业提交：11.20 23:59 前把 report 传到共享文件夹",
        needs_review=False,
        review_reason="未读到发送者",
    )
    banner = RawMessage("banner", "LLM小组(5)", "student_chat", "", None,
                        "微信 / LLM小组(5)", "“叶小凡”置顶了一条消息")
    agent = DdlAgent(
        sources=[_CapturedSource([message, banner])],
        extractor=RuleExtractor(),
        now=NOW,
    )

    result = agent.run(
        "打开微信，进入「LLM小组」，找到置顶的那条 DDL 通知，报告截止时间",
        write=False,
    )

    assert [record.deadline_iso for record in result.records] == [
        "2026-11-20T23:59:00+08:00"
    ]
    assert result.records[0].status == "confirmed"


# ---------------------------------------------------------------- 标注一致性
def test_uncertain_record_sets_result_needs_review():
    message = RawMessage(
        "g1", "COMP7503课程群", "student_chat", "叶小凡", None,
        "微信 / COMP7503课程群", "机器学习作业三，11.29 23:59 前交到 Moodle",
        needs_review=True,
        review_reason="时间字段解析失败",
    )
    agent = DdlAgent(
        sources=[_CapturedSource([message])],
        extractor=RuleExtractor(),
        now=NOW,
    )

    result = agent.run("打开微信，进入群，找作业三的截止时间", write=False)

    assert any(record.status == "uncertain" for record in result.records)
    assert result.needs_review is True
    assert "uncertain" in result.review_reason
