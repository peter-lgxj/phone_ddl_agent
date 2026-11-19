"""Local web entry point for phone DDL scans and calendar results."""

from __future__ import annotations

import threading
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from .monitor.config import ConfigError, add_from_text, load_config, remove_source, save_config
from .monitor.launcher import check_device, check_device_lock, resolve_adb
from .monitor.runner import run_cycle

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "sources.yaml"
blueprint = Blueprint("ddl", __name__)
_guard = threading.Lock()
_scan = {"running": False, "result": None, "error": None}


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _config_payload() -> dict:
    config = load_config(CONFIG_PATH)
    return {
        "sources": [
            {"id": s.id, "app": s.app, "target": s.target,
             "lookback_days": s.lookback_days, "enabled": s.enabled}
            for s in config.sources
        ],
        "calendar_ready": (_project_path(config.output_dir) / "calendar.html").is_file(),
    }


@blueprint.get("/ddl")
def page():
    return render_template("ddl.html")


@blueprint.get("/api/ddl")
def status():
    with _guard:
        scan = dict(_scan)
    return jsonify({**_config_payload(), **scan})


@blueprint.post("/api/ddl/sources")
def add_source():
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    try:
        with _guard:
            if _scan["running"]:
                return jsonify(error="扫描中不能修改来源"), 409
            config = load_config(CONFIG_PATH)
            added = add_from_text(config, text)
            save_config(config, CONFIG_PATH)
    except ConfigError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(added=[s.id for s in added], **_config_payload()), 201


@blueprint.delete("/api/ddl/sources/<source_id>")
def delete_source(source_id: str):
    with _guard:
        if _scan["running"]:
            return jsonify(error="扫描中不能修改来源"), 409
        config = load_config(CONFIG_PATH)
        if not remove_source(config, source_id):
            return jsonify(error="来源不存在"), 404
        save_config(config, CONFIG_PATH)
    return jsonify(**_config_payload())


@blueprint.post("/api/ddl/scan")
def start_scan():
    config = load_config(CONFIG_PATH)
    if not any(source.enabled for source in config.sources):
        return jsonify(error="请先添加至少一个监控来源"), 400
    adb = resolve_adb()
    if not adb:
        return jsonify(error="未找到 adb；请设置 ADB_BINARY 并连接手机"), 400
    problem = check_device(adb) or check_device_lock()
    if problem:
        return jsonify(error=problem), 400
    with _guard:
        if _scan["running"]:
            return jsonify(error="扫描已经在运行"), 409
        _scan.update(running=True, result=None, error=None)
    threading.Thread(target=_scan_worker, daemon=True).start()
    return jsonify(running=True), 202


def _scan_worker() -> None:
    try:
        config = load_config(CONFIG_PATH)
        result = run_cycle(
            config, config_path=CONFIG_PATH,
            out_dir=_project_path(config.output_dir),
            state_dir=_project_path(config.state_dir),
        )
        with _guard:
            _scan.update(result=result.to_dict(), error=None)
    except Exception as exc:  # Surface failures to the page while keeping the server alive.
        with _guard:
            _scan.update(error=str(exc))
    finally:
        with _guard:
            _scan["running"] = False


@blueprint.get("/ddl/calendar")
def calendar():
    path = _project_path(load_config(CONFIG_PATH).output_dir) / "calendar.html"
    if not path.is_file():
        return "尚无日历，请先完成一次扫描。", 404
    return send_file(path, mimetype="text/html")
