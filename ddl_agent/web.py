"""Local web entry point for phone DDL scans and calendar results."""

from __future__ import annotations

import hmac
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from device_lock import device_lock

from .monitor.config import (
    ConfigError, MonitorConfig, add_from_text, load_config, parse_monitor_text,
    remove_source, save_config, set_enabled,
)
from .monitor.launcher import check_device, resolve_adb
from .monitor.runner import run_cycle
from .monitor.sources import MobileAppSource
from .settings import TZ

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "sources.yaml"
blueprint = Blueprint("ddl", __name__)
_guard = threading.Lock()
_scan = {
    "running": False,
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "current_source": None,
    "total_sources": None,
}
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


def _source_factory(on_source=None):
    """构造数据源，并在开始读取某个来源时回报进度给页面。"""

    def factory(source) -> MobileAppSource:
        if on_source is not None:
            on_source(source)
        return _source_without_lock(source)

    return factory


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _config_payload() -> dict:
    config = load_config(CONFIG_PATH)
    calendar_path = _project_path(config.output_dir) / "calendar.html"
    ready = calendar_path.is_file()
    generated_at = None
    if ready:
        generated_at = datetime.fromtimestamp(
            calendar_path.stat().st_mtime, TZ
        ).isoformat(timespec="seconds")
    return {
        "sources": [
            {"id": s.id, "app": s.app, "kind": s.kind, "target": s.target,
             "source_type": s.source_type, "lookback_days": s.lookback_days,
             "enabled": s.enabled}
            for s in config.sources
        ],
        "calendar_ready": ready,
        "calendar_generated_at": generated_at,
        "schedule": config.schedule,
        "interval_minutes": config.interval_minutes,
    }


def _build_scan_config(payload: dict) -> tuple[MonitorConfig, bool]:
    """按页面请求构造本轮扫描配置。

    - ``mode=once`` 且有文本：只构造临时单来源配置，不写 ``sources.yaml``；
    - ``mode=recurring`` 且有文本：新增为定期检索目标并保存；
    - 无文本：按现有启用目标扫描（仅定期模式允许）。
    返回 ``(config, 是否需要保存)``。
    """
    config = load_config(CONFIG_PATH)
    text = str(payload.get("text") or "").strip()
    mode = str(payload.get("mode") or "once").strip().lower()
    schedule_text = str(payload.get("schedule") or "").strip()
    if not text:
        if mode == "once":
            raise ConfigError("请先填写检索目标；仅扫描一次不会保存为定期任务")
        return config, False
    if mode == "recurring":
        add_from_text(config, text, schedule_text=schedule_text or None)
        return config, True
    once = MonitorConfig(
        schedule=config.schedule,
        interval_minutes=config.interval_minutes,
        output_dir=config.output_dir,
        state_dir=config.state_dir,
        write_phone_calendar=config.write_phone_calendar,
        sources=parse_monitor_text(text),
    )
    return once, False


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
    schedule = str((request.get_json(silent=True) or {}).get("schedule") or "").strip()
    try:
        with _guard:
            if _scan["running"]:
                return jsonify(error="扫描中不能修改检索目标"), 409
            config = load_config(CONFIG_PATH)
            added = add_from_text(config, text, schedule_text=schedule or None)
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
            return jsonify(error="扫描中不能修改检索目标"), 409
        config = load_config(CONFIG_PATH)
        if not remove_source(config, source_id):
            return jsonify(error="检索目标不存在"), 404
        save_config(config, CONFIG_PATH)
    return jsonify(**_config_payload())


@blueprint.patch("/api/ddl/sources/<source_id>")
def toggle_source(source_id: str):
    denied = _write_denied()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(error="enabled 必须是布尔值"), 400
    with _guard:
        if _scan["running"]:
            return jsonify(error="扫描中不能修改检索目标"), 409
        config = load_config(CONFIG_PATH)
        if not set_enabled(config, source_id, enabled):
            return jsonify(error="检索目标不存在"), 404
        save_config(config, CONFIG_PATH)
    return jsonify(**_config_payload())


@blueprint.post("/api/ddl/scan")
def start_scan():
    denied = _write_denied()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        config, persist = _build_scan_config(payload)
    except ConfigError as exc:
        return jsonify(error=str(exc)), 400
    if not any(source.enabled for source in config.sources):
        return jsonify(error="请先填写至少一个检索目标"), 400
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
        if persist:
            save_config(config, CONFIG_PATH)
        _scan.update(
            running=True, result=None, error=None,
            started_at=datetime.now(TZ).isoformat(timespec="seconds"),
            finished_at=None, current_source=None,
            total_sources=sum(1 for source in config.sources if source.enabled),
        )
    threading.Thread(target=_scan_worker, args=(config,), daemon=True).start()
    return jsonify(running=True, persisted=persist), 202


def _scan_worker(config: MonitorConfig | None = None) -> None:
    def on_source(source) -> None:
        with _guard:
            _scan["current_source"] = {"app": source.app, "target": source.target}

    try:
        with device_lock(timeout=30) as acquired:
            if not acquired:
                with _guard:
                    _scan.update(error="设备锁被占用：请关闭其他 run.py / app.py / 窗口后重试")
                return
            config = config or load_config(CONFIG_PATH)
            with _guard:
                _scan["total_sources"] = sum(
                    1 for source in getattr(config, "sources", []) if source.enabled
                )
            result = run_cycle(
                config, config_path=CONFIG_PATH,
                out_dir=_project_path(config.output_dir),
                state_dir=_project_path(config.state_dir),
                connector_factory=_source_factory(on_source),
            )
        with _guard:
            _scan.update(result=result.to_dict(), error=None)
    except Exception as exc:  # Surface failures to the page while keeping the server alive.
        with _guard:
            _scan.update(error=str(exc))
    finally:
        with _guard:
            _scan.update(
                running=False, current_source=None,
                finished_at=datetime.now(TZ).isoformat(timespec="seconds"),
            )


@blueprint.get("/ddl/calendar")
def calendar():
    path = _project_path(load_config(CONFIG_PATH).output_dir) / "calendar.html"
    if not path.is_file():
        return "尚无日历，请先完成一次扫描。", 404
    return send_file(path, mimetype="text/html")
