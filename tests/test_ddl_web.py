"""Web flow checks without driving a real phone."""

from ddl_agent import web
from flask import Flask


def test_source_config_and_calendar_routes(tmp_path, monkeypatch):
    template_dir = web.ROOT / "templates"
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setattr(web, "CONFIG_PATH", tmp_path / "sources.yaml")
    app = Flask(__name__, template_folder=str(template_dir))
    app.register_blueprint(web.blueprint)
    client = app.test_client()

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
