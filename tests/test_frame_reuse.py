"""同屏复用：截图哈希未变时跳过重新截图与重编码，并在 step JSON 标记 reused_frame。"""

from __future__ import annotations

import json

from PIL import Image

import config
import image_preprocess
import screen_agent
import ui_state
from memory import TaskMemory


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]
        self.usage = None


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        return FakeResponse(self.content)


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


class StubPhone:
    def __init__(self, color):
        self.color = color
        self.screenshots = 0

    def screenshot(self, save_path="screen.png"):
        self.screenshots += 1
        Image.new("RGB", (1080, 2374), self.color).save(save_path)

    def swipe(self, *_args):
        pass

    def tap(self, *_args):
        pass


class StubOCR:
    def detect_tree(self):
        return []

    def detect_ocr(self, _path):
        return []


def _agent(tmp_path, monkeypatch, color=(20, 20, 20)):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "RULE_FASTPATH", False)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "MERGED_OBSERVE", True)
    monkeypatch.setattr(config, "COMPLETION_CHECK", "decision")
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(config, "OBSERVE_OCR_ENABLED", False)
    ui_state.init_state("同屏复用")

    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = FakeClient(json.dumps({
        "observation": "界面没有变化", "action_type": "wait", "description": "等待",
    }, ensure_ascii=False))
    agent.model = "fake"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = StubPhone(color)
    agent.ocr = StubOCR()
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
    agent._last_frame_hash = None
    agent._last_frame_bytes = None
    agent._last_frame_meta = None
    agent._pending_settle_frame = None
    agent.usage_events = []
    agent._last_step_usage = []
    return agent


def test_same_frame_skips_screenshot_and_encode(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    calls = {"encode": 0}
    original = image_preprocess.prepare_image_bytes

    def counting(path):
        calls["encode"] += 1
        return original(path)

    monkeypatch.setattr(image_preprocess, "prepare_image_bytes", counting)

    agent._execute_step(1, TaskMemory("等待页面"))
    first = dict(agent._last_step_timings)
    assert first["reused_frame"] is False
    assert calls["encode"] == 1

    shots_before = agent.phone.screenshots
    agent._execute_step(2, TaskMemory("等待页面"))
    second = dict(agent._last_step_timings)

    assert second["reused_frame"] is True
    assert second["preprocess"] == 0.0
    assert calls["encode"] == 1          # 第二步没有再次编码
    assert agent.phone.screenshots == shots_before + 1  # 只截图一次（没有 settle）


def test_step_json_records_reused_frame_true(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)

    agent._execute_step(1, TaskMemory("等待页面"))
    agent._execute_step(2, TaskMemory("等待页面"))

    step1 = json.loads((agent.run_dir / "step_001.json").read_text(encoding="utf-8"))
    step2 = json.loads((agent.run_dir / "step_002.json").read_text(encoding="utf-8"))
    assert step1["reused_frame"] is False
    assert step2["reused_frame"] is True
    assert step2["timings"]["reuse_reason"] == "hash"


def test_changed_frame_is_not_reused(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, color=(20, 20, 20))
    agent._execute_step(1, TaskMemory("等待页面"))

    agent.phone.color = (200, 10, 10)
    agent._execute_step(2, TaskMemory("等待页面"))

    step2 = json.loads((agent.run_dir / "step_002.json").read_text(encoding="utf-8"))
    assert step2["reused_frame"] is False
    assert step2["timings"]["reused_frame"] is False


def test_settle_frame_is_reused_without_new_capture(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    agent._execute_step(1, TaskMemory("等待页面"))

    # 模拟真实流程：动作执行后 settle 保留稳定帧，下一步直接复用
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.5)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_POLL_SECONDS", 0.02)
    agent._settle_screen()
    assert agent._pending_settle_frame is not None

    shots_before = agent.phone.screenshots
    timings = {}
    data, meta, reused = agent._capture_frame(timings)

    assert reused is True
    assert timings["reuse_reason"] == "hash"
    assert agent.phone.screenshots == shots_before  # 没有重新截图
    assert data and meta["mime"] == "image/png"
