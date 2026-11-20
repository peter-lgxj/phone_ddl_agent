"""截图与 OCR 并行：观察请求与 OCR 重叠，OCR 失败不影响截图和决策。"""

from __future__ import annotations

import json
import time

from PIL import Image

import config
import screen_agent
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


class SlowClient:
    def __init__(self, content, latency=0.2, events=None):
        self.content = content
        self.latency = latency
        self.events = events if events is not None else {}
        self.prompts = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.prompts.append(kwargs)
        self.events["vlm_start"] = time.perf_counter()
        time.sleep(self.latency)
        self.events["vlm_end"] = time.perf_counter()
        return FakeResponse(self.content)


class TimedOCR:
    def __init__(self, latency=0.2, events=None, fail=False):
        self.latency = latency
        self.events = events if events is not None else {}
        self.fail = fail
        self.calls = 0

    def detect_ocr(self, _path):
        self.calls += 1
        self.events["ocr_start"] = time.perf_counter()
        time.sleep(self.latency)
        self.events["ocr_end"] = time.perf_counter()
        if self.fail:
            raise RuntimeError("OCR 崩了")
        return [{"text": "截止 11.29", "center": (500, 800),
                 "bbox": (0, 700, 400, 900), "source": "ocr", "type": "text"}]


class StubPhone:
    def screenshot(self, save_path="screen.png"):
        Image.new("RGB", (1080, 2374), (20, 20, 20)).save(save_path)


def make_agent(tmp_path, monkeypatch, client, ocr):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    monkeypatch.setattr(config, "OBSERVE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(config, "OBSERVE_OCR_ENABLED", True)
    monkeypatch.setattr(config, "OBSERVE_OCR_ACTIONS", {"swipe", "scroll"})
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_ANALYSIS", False)
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.client = client
    agent.model = "fake"
    agent.support_multi_image = False
    agent.extra_body = None
    agent.phone = StubPhone()
    agent.ocr = ocr
    agent.read_only = True
    agent.run_dir = None
    agent.driver_kind = "uiautomator2"
    agent._observe_valid = False
    agent._last_image_meta = None
    agent._last_model_action = None
    agent._last_decision_timings = {}
    agent._last_step_timings = {}
    agent._last_observe_ocr_seconds = 0.0
    agent._last_observation_scope = "post_action"
    agent.usage_events = []
    agent._last_step_usage = []
    return agent


def test_observe_ocr_overlaps_vlm_call(tmp_path, monkeypatch):
    events = {}
    client = SlowClient("界面已更新", latency=0.2, events=events)
    ocr = TimedOCR(latency=0.2, events=events)
    agent = make_agent(tmp_path, monkeypatch, client, ocr)

    observation = agent._observe({"action_type": "swipe"}, TaskMemory("滚动"))

    assert "[OCR]" in observation and "截止 11.29" in observation
    assert ocr.calls == 1
    # 两段区间重叠：OCR 在 VLM 结束前开始，VLM 在 OCR 结束前开始
    assert events["ocr_start"] < events["vlm_end"]
    assert events["vlm_start"] < events["ocr_end"]
    assert agent._last_observe_ocr_seconds >= 0.15


def test_observe_ocr_failure_keeps_model_observation(tmp_path, monkeypatch):
    events = {}
    client = SlowClient("界面已更新", latency=0.1, events=events)
    ocr = TimedOCR(latency=0.1, events=events, fail=True)
    agent = make_agent(tmp_path, monkeypatch, client, ocr)

    observation = agent._observe({"action_type": "swipe"}, TaskMemory("滚动"))

    assert observation == "界面已更新"
    assert ocr.calls == 1


def test_observe_no_ocr_for_tap(tmp_path, monkeypatch):
    events = {}
    client = SlowClient("已点击", latency=0.05, events=events)
    ocr = TimedOCR(latency=0.05, events=events)
    agent = make_agent(tmp_path, monkeypatch, client, ocr)

    assert agent._observe({"action_type": "tap"}, TaskMemory("点击")) == "已点击"
    assert ocr.calls == 0


class MergedOCR:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def detect_ocr(self, _path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("OCR 崩了")
        return [{"text": "上一屏消息", "center": (500, 800),
                 "bbox": (0, 700, 400, 900), "source": "ocr", "type": "text"}]


def test_merged_decision_keeps_ocr_text(tmp_path, monkeypatch):
    content = json.dumps({"observation": "还在列表", "action_type": "tap",
                          "x": 100, "y": 200, "description": "点开"},
                         ensure_ascii=False)
    events = {}
    client = SlowClient(content, latency=0.01, events=events)
    ocr = MergedOCR()
    agent = make_agent(tmp_path, monkeypatch, client, ocr)
    monkeypatch.setattr(config, "OCR_SNAP_MODE", "tree_only")
    agent.ocr.detect_tree = lambda: []
    memory = TaskMemory("继续")
    memory.add_step(thought="", action={"action_type": "swipe"}, observation="上一屏")

    action = agent._think_and_act(memory, merged=True)

    assert action["action_type"] == "tap"
    assert ocr.calls == 1
    prompt = json.dumps(client.prompts[-1], ensure_ascii=False)
    assert "[上一动作后的 OCR]" in prompt and "上一屏消息" in prompt


def test_merged_ocr_failure_does_not_block_decision(tmp_path, monkeypatch):
    content = json.dumps({"observation": "还在列表", "action_type": "wait",
                          "description": "等待"}, ensure_ascii=False)
    client = SlowClient(content, latency=0.01)
    agent = make_agent(tmp_path, monkeypatch, client, MergedOCR(fail=True))
    memory = TaskMemory("继续")
    memory.add_step(thought="", action={"action_type": "swipe"}, observation="上一屏")

    action = agent._think_and_act(memory, merged=True)

    assert action["action_type"] == "wait"
