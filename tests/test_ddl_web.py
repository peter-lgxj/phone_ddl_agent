"""Web flow checks without driving a real phone."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ddl_agent import web
from flask import Flask


def _client(tmp_path, monkeypatch):
    template_dir = web.ROOT / "templates"
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setattr(web, "CONFIG_PATH", tmp_path / "sources.yaml")
    monkeypatch.setattr(web, "_scan", {"running": False, "result": None, "error": None})
    monkeypatch.delenv("DDL_WEB_TOKEN", raising=False)
    app = Flask(__name__, template_folder=str(template_dir))
    app.register_blueprint(web.blueprint)
    return app.test_client()


def test_source_config_and_calendar_routes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/ddl").status_code == 200
    assert client.get("/api/ddl").json["sources"] == []
    assert client.post("/api/ddl/scan").status_code == 400
    response = client.post("/api/ddl/sources", json={
        "text": "监控微信「课程群」，最近14天"
    })
    assert response.status_code == 201
    assert response.json["sources"][0]["target"] == "课程群"
    assert client.get("/ddl/calendar").status_code == 404

    out = tmp_path / "output"
    out.mkdir()
    (out / "calendar.html").write_text("<h1>DDL 日历</h1>", encoding="utf-8")
    assert "DDL 日历" in client.get("/ddl/calendar").get_data(as_text=True)
    assert client.delete("/api/ddl/sources/" + response.json["added"][0]).status_code == 200
    assert client.get("/api/ddl").json["sources"] == []


def test_write_endpoints_require_token_when_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("DDL_WEB_TOKEN", "secret-token")

    assert client.post("/api/ddl/sources", json={"text": "监控微信「课程群」"}).status_code == 401
    assert client.post("/api/ddl/scan").status_code == 401
    assert client.delete("/api/ddl/sources/any").status_code == 401
    response = client.post(
        "/api/ddl/sources",
        json={"text": "监控微信「课程群」"},
        headers={"X-DDL-Token": "secret-token"},
    )
    assert response.status_code == 201


def test_page_copy_and_mode_choices(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    html = client.get("/ddl").get_data(as_text=True)

    assert "检索目标" in html
    assert "仅扫描一次" in html
    assert "定期" in html
    assert "监控来源" not in html


def _stub_phone_and_thread(monkeypatch, started):
    monkeypatch.setattr(web, "resolve_adb", lambda: "adb")
    monkeypatch.setattr(web, "check_device", lambda adb: None)

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.args = args

        def start(self):
            started.append(self.args[0])

    monkeypatch.setattr(web.threading, "Thread", ImmediateThread)


def test_scan_once_does_not_persist_target(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = []
    _stub_phone_and_thread(monkeypatch, started)

    response = client.post("/api/ddl/scan", json={
        "text": "QQ「临时群」，最近14天", "mode": "once",
    })

    assert response.status_code == 202
    assert response.json["persisted"] is False
    assert not (tmp_path / "sources.yaml").exists()
    assert [source.target for source in started[0].sources] == ["临时群"]


def test_scan_recurring_persists_target(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = []
    _stub_phone_and_thread(monkeypatch, started)

    response = client.post("/api/ddl/scan", json={
        "text": "监控 QQ「定期群」，每6小时一次", "mode": "recurring", "schedule": "每6小时",
    })

    assert response.status_code == 202
    assert response.json["persisted"] is True
    saved = (tmp_path / "sources.yaml").read_text(encoding="utf-8")
    assert "定期群" in saved
    assert [source.target for source in started[0].sources] == ["定期群"]


def test_scan_once_without_text_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post("/api/ddl/scan", json={"mode": "once"})

    assert response.status_code == 400
    assert "检索目标" in response.json["error"]


def test_scan_worker_holds_device_lock_and_disables_nested_lock(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def fake_lock(timeout=30):
        calls.append(timeout)
        yield True

    captured = {}

    def fake_cycle(config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"inserted": 0})

    monkeypatch.setattr(web, "device_lock", fake_lock)
    monkeypatch.setattr(web, "run_cycle", fake_cycle)
    monkeypatch.setattr(web, "load_config", lambda _path: SimpleNamespace(output_dir="output",
                                                                         state_dir="state"))
    monkeypatch.setattr(web, "_scan", {"running": True, "result": None, "error": None})

    web._scan_worker()

    assert calls == [30]
    assert captured["connector_factory"] is web._source_without_lock
    assert web._scan["running"] is False
    assert web._scan["result"] == {"inserted": 0}


def test_scan_worker_reports_busy_lock_without_running_cycle(tmp_path, monkeypatch):
    @contextmanager
    def busy_lock(timeout=30):
        yield False

    monkeypatch.setattr(web, "device_lock", busy_lock)
    monkeypatch.setattr(web, "run_cycle", lambda *_args, **_kwargs: pytest.fail("不应启动扫描"))
    monkeypatch.setattr(web, "_scan", {"running": True, "result": None, "error": None})

    web._scan_worker()

    assert "设备锁被占用" in web._scan["error"]
    assert web._scan["running"] is False
