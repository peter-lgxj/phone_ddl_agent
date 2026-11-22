"""一键演示脚本：离线回放、无设备降级、错误提示。"""

from __future__ import annotations

import json
from pathlib import Path

import scripts.demo as demo

ROOT = Path(__file__).resolve().parents[1]


def test_offline_demo_writes_all_outputs(tmp_path):
    code = demo.main(["--offline", "--out", str(tmp_path)])

    assert code == 0
    payload = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert payload["counts"]["total"] == 3
    assert (tmp_path / "deadlines.ics").exists()
    assert (tmp_path / "calendar.html").exists()
    names = {record["task_name"] for record in payload["records"]}
    assert {"作业三", "Final Report", "小组作业"} <= names


def test_offline_demo_records_same_source_supersede(tmp_path, capsys):
    demo.main(["--offline", "--out", str(tmp_path)])

    payload = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    homework = next(r for r in payload["records"] if r["task_name"] == "作业三")
    assert homework["deadline"] == "2026-12-06T23:59:00+08:00"
    assert homework["superseded_by"]
    assert "延期" in homework["note"]
    assert "同源覆盖" in capsys.readouterr().out


def test_auto_mode_falls_back_to_offline_without_device(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "connected_devices", lambda: [])

    code = demo.main(["--mode", "auto", "--out", str(tmp_path)])

    assert code == 0
    assert (tmp_path / "data.json").exists()


def test_live_mode_without_device_reports_next_step(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "connected_devices", lambda: [])

    code = demo.main(["--live", "--no-fallback", "--out", str(tmp_path)])

    output = capsys.readouterr().out
    assert code == 2
    assert "adb devices" in output
    assert "Traceback" not in output


def test_missing_demo_config_gives_readable_error(tmp_path, monkeypatch, capsys):
    code = demo.main(["--offline", "--demo-config", str(tmp_path / "nope.json")])

    output = capsys.readouterr().out
    assert code == 2
    assert "离线演示配置不存在" in output
    assert "Traceback" not in output


def test_missing_sources_yaml_points_to_example(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "connected_devices", lambda: ["device-1"])
    monkeypatch.chdir(tmp_path)

    code = demo.main(["--live", "--no-fallback", "--out", str(tmp_path / "out")])

    output = capsys.readouterr().out
    assert code == 2
    assert "sources.example.yaml" in output
    assert "Traceback" not in output


def test_device_busy_reports_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "connected_devices", lambda: ["device-1"])
    (tmp_path / "sources.yaml").write_text(
        (ROOT / "sources.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(demo, "device_lock", lambda timeout=0: _busy())

    code = demo.main([
        "--live", "--no-fallback", "--sources", str(tmp_path / "sources.yaml"),
        "--out", str(tmp_path / "out"),
    ])

    output = capsys.readouterr().out
    assert code == 2
    assert "占用" in output
    assert "Traceback" not in output


class _busy:
    def __enter__(self):
        return False

    def __exit__(self, *exc):
        return False


def test_demo_never_writes_chat_or_sends(monkeypatch, tmp_path):
    """离线演示不应触达手机；这里用假的 adb 调用记录断言没有 shell input 动作。"""
    calls: list[tuple] = []

    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        raise FileNotFoundError("no adb")

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    demo.main(["--offline", "--out", str(tmp_path)])

    assert all("shell" not in call for call in calls)
