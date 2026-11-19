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
