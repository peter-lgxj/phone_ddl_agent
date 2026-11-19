"""Local web entry point for phone DDL scans and calendar results."""

from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from device_lock import device_lock

from .monitor.config import ConfigError, add_from_text, load_config, remove_source, save_config
from .monitor.launcher import check_device, resolve_adb
from .monitor.runner import run_cycle
from .monitor.sources import MobileAppSource

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "sources.yaml"
blueprint = Blueprint("ddl", __name__)
_guard = threading.Lock()
_scan = {"running": False, "result": None, "error": None}
_LOOPBACK = {"127.0.0.1", "::1", None}


def _write_denied():
    """写操作鉴权：配置 DDL_WEB_TOKEN 时校验请求头，否则只允许本机。

    扫描会读取手机聊天，来源增删会改配置，因此不能只靠默认绑定 127.0.0.1。
    """
    token = os.getenv("DDL_WEB_TOKEN", "").strip()
    if token:
        supplied = request.headers.get("X-DDL-Token", "")
        if not hmac.compare_digest(supplied, token):
            return jsonify(error="缺少或错误的 DDL_WEB_TOKEN"), 401
        return None
    if request.remote_addr not in _LOOPBACK:
        return jsonify(error="非本机请求被拒绝；请设置 DDL_WEB_TOKEN 并通过 X-DDL-Token 请求头提供"), 403
    return None


def _source_without_lock(source) -> MobileAppSource:
    """扫描线程已持有设备锁，数据源不能再次加锁（否则会等满 30s 失败）。"""
    return MobileAppSource(source, use_device_lock=False)


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
    denied = _write_denied()
    if denied:
        return denied
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
    denied = _write_denied()
    if denied:
        return denied
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
    denied = _write_denied()
    if denied:
        return denied
    config = load_config(CONFIG_PATH)
    if not any(source.enabled for source in config.sources):
        return jsonify(error="请先添加至少一个监控来源"), 400
    adb = resolve_adb()
    if not adb:
        return jsonify(error="未找到 adb；请设置 ADB_BINARY 并连接手机"), 400
    # 设备是否被占用不在这里预检：预检与真正读取之间存在 TOCTOU，
    # 统一由扫描线程持有设备锁，拿不到锁时把错误写回页面。
    problem = check_device(adb)
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
        with device_lock(timeout=30) as acquired:
            if not acquired:
                with _guard:
                    _scan.update(error="设备锁被占用：请关闭其他 run.py / app.py / 监控窗口后重试")
                return
            config = load_config(CONFIG_PATH)
            result = run_cycle(
                config, config_path=CONFIG_PATH,
                out_dir=_project_path(config.output_dir),
                state_dir=_project_path(config.state_dir),
                connector_factory=_source_without_lock,
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
