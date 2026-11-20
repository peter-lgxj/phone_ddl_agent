"""连接预热与 HTTP 会话复用：两个模型客户端共用一个连接池，启动期完成握手。"""

from __future__ import annotations

import config
import screen_agent


class FakeModels:
    def __init__(self):
        self.calls = 0

    def list(self):
        self.calls += 1
        return []


class FakeOpenAI:
    instances: list["FakeOpenAI"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.models = FakeModels()
        FakeOpenAI.instances.append(self)


class FakeOCR:
    def __init__(self, device=None):
        self.warmed = 0

    def warmup(self):
        self.warmed += 1

    def set_tree_provider(self, provider):
        pass


class FakePhone:
    kind = "uiautomator2"

    def __init__(self):
        self.warmed = 0

    def warmup(self):
        self.warmed += 1

    def dump_hierarchy(self):
        return "<hierarchy/>"


def _patch_agent_deps(monkeypatch):
    FakeOpenAI.instances = []
    monkeypatch.setattr(screen_agent, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(screen_agent, "OCRLocator", FakeOCR)
    monkeypatch.setattr(screen_agent, "create_phone_controller", lambda _ocr: FakePhone())
    monkeypatch.setattr(screen_agent, "AppLauncher", lambda _phone: None)
    monkeypatch.setattr(config, "ACTIVE_PROVIDER", "fake")
    monkeypatch.setattr(config, "PLANNER_PROVIDER", "fake")
    monkeypatch.setattr(config, "PROVIDERS", {
        "fake": {
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:9/v1",
            "model": "fake-model",
            "support_multi_image": True,
        },
    })
    monkeypatch.setattr(config, "OCR_PREHEAT", False)
    monkeypatch.setattr(config, "CONNECTION_WARMUP", True)


def test_make_http_client_uses_keepalive_limits(monkeypatch):
    monkeypatch.setattr(config, "HTTP_MAX_CONNECTIONS", 5)
    monkeypatch.setattr(config, "HTTP_KEEPALIVE_SECONDS", 30)
    client = screen_agent.make_http_client()
    try:
        limits = client._transport._pool._max_keepalive_connections
        expiry = client._transport._pool._keepalive_expiry
        assert limits == 5
        assert expiry == 30
    finally:
        client.close()


def test_both_clients_share_one_http_pool_and_warmup(monkeypatch):
    _patch_agent_deps(monkeypatch)
    agent = screen_agent.ScreenAgent()

    assert len(FakeOpenAI.instances) == 2
    first, second = FakeOpenAI.instances
    assert first.kwargs["http_client"] is second.kwargs["http_client"]
    assert first.kwargs["http_client"] is agent._http_client
    assert first.models.calls == 1          # 启动预热只发一次
    assert agent.phone.warmed == 1
    assert agent._warmup_seconds >= 0.0


def test_warmup_can_be_disabled(monkeypatch):
    _patch_agent_deps(monkeypatch)
    monkeypatch.setattr(config, "CONNECTION_WARMUP", False)

    agent = screen_agent.ScreenAgent()

    assert FakeOpenAI.instances[0].models.calls == 0
    assert agent.phone.warmed == 0
    assert agent._warmup_seconds == 0.0


def test_adb_warmup_starts_adb_server(monkeypatch):
    from adb_controller import AdbDumpController

    calls = []
    controller = AdbDumpController(probe=False)
    monkeypatch.setattr(controller, "_run",
                        lambda *args, timeout=30: calls.append(args))
    controller.warmup()

    assert ("start-server",) in calls
    assert ("get-state",) in calls
