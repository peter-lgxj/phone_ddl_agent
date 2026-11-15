"""reset 结果如实上报 + 设备锁归属的单元测试（不连接真机）。"""

import json
import os

import pytest

import device_lock as dl
import run_experiment as rwe


def stub(monkeypatch, installed, force_stop_rc=0, adb_rc=0):
    monkeypatch.setattr(rwe, "_installed", lambda pkg: pkg in installed if installed is not None else None)

    def fake_adb(args, timeout=15):
        if args == ["get-state"]:
            return 0, "device"
        if args[:3] == ["shell", "am", "force-stop"]:
            return force_stop_rc, ""
        if args[:2] == ["shell", "input"]:
            return adb_rc, ""
        return adb_rc, ""

    monkeypatch.setattr(rwe, "adb", fake_adb)


def test_reset_reports_force_stop_failure(monkeypatch):
    stub(monkeypatch, {"com.tencent.mm"}, force_stop_rc=1)

    result = rwe.reset_device(["微信"])

    assert result["stopped"] == []
    assert result["failed"][0]["package"] == "com.tencent.mm"
    assert result["failed"][0]["rc"] == 1


def test_reset_reports_missing_adb(monkeypatch):
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (None, ""))

    result = rwe.reset_device(["微信"])

    assert result["stopped"] == []
    assert result["failed"][0]["reason"].startswith("device not ready")
    assert result["not_installed"] == [], "设备离线不能算作应用未安装"
    assert result["home"] is False


def test_reset_reports_offline_device(monkeypatch):
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (1, "error: device offline"))

    result = rwe.reset_device(["微信"])

    assert "device not ready" in result["failed"][0]["reason"]
    assert result["not_installed"] == []


def test_installed_requires_exact_package_match(monkeypatch):
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (0, "package:com.tencent.mm2"))

    assert rwe._installed("com.tencent.mm") is False


def test_installed_returns_none_when_command_fails(monkeypatch):
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (1, "error: device offline"))

    assert rwe._installed("com.tencent.mm") is None


def test_reset_skips_uninstalled_candidates_without_false_alarm(monkeypatch):
    """未安装的候选包不算失败，真正安装的包才被 stop"""
    stub(monkeypatch, {"com.huawei.notepad"}, force_stop_rc=0)

    result = rwe.reset_device(["备忘录"])

    assert result["stopped"] == ["com.huawei.notepad"]
    assert result["failed"] == []
    assert "com.miui.notes" in result["not_installed"]


def test_reset_fails_when_no_candidate_installed(monkeypatch):
    stub(monkeypatch, set())

    result = rwe.reset_device(["备忘录"])

    assert result["stopped"] == []
    assert result["failed"][0]["reason"] == "no candidate installed"


def test_reset_reports_unmapped_app(monkeypatch):
    stub(monkeypatch, set())

    result = rwe.reset_device(["未知应用"])

    assert result["not_mapped"] == ["未知应用"]


def test_memo_app_covers_review_devices():
    candidates = rwe.PACKAGE_BY_APP["备忘录"]
    assert "com.vivo.notes" in candidates
    assert "com.hihonor.notepad" in candidates


def test_device_lock_busy_when_owner_alive(monkeypatch, tmp_path):
    lock = tmp_path / ".device.lock"
    lock.write_text(json.dumps({"token": "other", "pid": os.getpid()}), encoding="utf-8")
    monkeypatch.setattr(dl, "LOCK_PATH", lock)

    with dl.device_lock(timeout=0) as ok:
        assert ok is False
    assert lock.exists(), "不能删除活进程持有的锁"


def test_device_lock_reclaims_dead_owner(monkeypatch, tmp_path):
    lock = tmp_path / ".device.lock"
    lock.write_text(json.dumps({"token": "dead", "pid": 99999999}), encoding="utf-8")
    monkeypatch.setattr(dl, "LOCK_PATH", lock)

    with dl.device_lock(timeout=1) as ok:
        assert ok is True

    assert not lock.exists()


def test_release_does_not_remove_someone_elses_lock(monkeypatch, tmp_path):
    lock = tmp_path / ".device.lock"
    monkeypatch.setattr(dl, "LOCK_PATH", lock)

    token = dl.acquire(timeout=1)
    assert token
    lock.write_text(json.dumps({"token": "newer", "pid": os.getpid()}), encoding="utf-8")
    dl.release(token)

    assert lock.exists(), "旧持有者退出时不能删掉新持有者的锁"


def test_write_failure_removes_empty_lock(monkeypatch, tmp_path):
    lock = tmp_path / ".device.lock"
    monkeypatch.setattr(dl, "LOCK_PATH", lock)

    def broken_write(fd, data):
        raise OSError("disk full")

    monkeypatch.setattr(dl.os, "write", broken_write)
    with pytest.raises(OSError):
        dl.acquire(timeout=0)

    assert not lock.exists(), "写入失败不能留下空锁"
