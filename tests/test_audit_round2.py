"""第二轮自查：证据保留策略、adb 错误路径、配置校验。"""

from __future__ import annotations

import os
import time

import pytest

import screen_agent
from ddl_agent.monitor.config import ConfigError, MonitorConfig


def _make_runs(root, count: int) -> list:
    runs = []
    base = time.time() - count * 60
    for index in range(count):
        path = root / f"run-{index:02d}"
        path.mkdir()
        (path / "step.json").write_text("{}", encoding="utf-8")
        stamp = base + index * 60
        os.utime(path, (stamp, stamp))
        runs.append(path)
    return runs


def test_cleanup_old_runs_keeps_latest_window(tmp_path):
    _make_runs(tmp_path, 25)

    removed = screen_agent.cleanup_old_runs(tmp_path, keep=20)

    remaining = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert len(remaining) == 20
    assert len(removed) == 5
    assert "run-24" in remaining and "run-19" in remaining
    assert "run-00" not in remaining


def test_cleanup_old_runs_protects_marked_dirs(tmp_path):
    _make_runs(tmp_path, 23)
    keep_named = tmp_path / "keep-demo"
    keep_named.mkdir()
    os.utime(keep_named, (time.time() - 10_000, time.time() - 10_000))
    marked = tmp_path / "old-evidence"
    marked.mkdir()
    (marked / "KEEP").write_text("", encoding="utf-8")
    os.utime(marked, (time.time() - 9_000, time.time() - 9_000))

    screen_agent.cleanup_old_runs(tmp_path, keep=20)

    assert keep_named.exists()
    assert marked.exists()
    assert not (tmp_path / "run-00").exists()


def test_cleanup_old_runs_reads_env_keep(tmp_path, monkeypatch):
    _make_runs(tmp_path, 6)
    monkeypatch.setenv("RUNS_KEEP", "3")

    screen_agent.cleanup_old_runs(tmp_path)

    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 3


def test_cleanup_old_runs_never_raises(tmp_path, monkeypatch):
    _make_runs(tmp_path, 4)
    monkeypatch.setattr(screen_agent.shutil, "rmtree",
                        lambda path: (_ for _ in ()).throw(OSError("locked")))

    removed = screen_agent.cleanup_old_runs(tmp_path, keep=1)

    assert removed == []
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 4


def test_experiment_adb_raises_explicit_error(monkeypatch):
    import run_experiment

    def missing(*args, **kwargs):
        raise FileNotFoundError("adb not found")

    monkeypatch.setattr(run_experiment.subprocess, "run", missing)

    with pytest.raises(run_experiment.AdbError, match="adb 执行失败"):
        run_experiment.adb(["devices"])


def test_config_rejects_unsupported_app():
    with pytest.raises(ConfigError, match="不支持"):
        MonitorConfig.from_dict({"sources": [
            {"id": "a", "app": "telegram", "kind": "group", "target": "x",
             "source_type": "chat"},
        ]})


def test_config_rejects_invalid_lookback():
    with pytest.raises(ConfigError, match="lookback_days"):
        MonitorConfig.from_dict({"sources": [
            {"id": "a", "app": "qq", "kind": "group", "target": "x",
             "source_type": "chat", "lookback_days": 0},
        ]})


def test_config_rejects_invalid_interval():
    with pytest.raises(ConfigError, match="interval_minutes"):
        MonitorConfig.from_dict({"interval_minutes": 0})


def test_config_rejects_missing_target():
    with pytest.raises(ConfigError, match="缺少"):
        MonitorConfig.from_dict({"sources": [
            {"id": "a", "app": "qq", "kind": "group", "target": "",
             "source_type": "chat"},
        ]})


def test_web_rejects_second_scan_while_running(tmp_path, monkeypatch):
    from flask import Flask

    from ddl_agent import web

    monkeypatch.setattr(web, "CONFIG_PATH", tmp_path / "sources.yaml")
    monkeypatch.setattr(web, "resolve_adb", lambda: "adb")
    monkeypatch.setattr(web, "check_device", lambda adb: None)
    monkeypatch.setattr(web, "_scan", {"running": True, "result": None, "error": None})
    app = Flask(__name__)
    app.register_blueprint(web.blueprint)
    client = app.test_client()

    response = client.post("/api/ddl/scan", json={"mode": "once", "text": "QQ「群」，最近14天"})

    assert response.status_code == 409
    assert "扫描已经在运行" in response.json["error"]
