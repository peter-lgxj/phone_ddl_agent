"""reset 结果如实上报 + 设备锁的单元测试（不连接真机）。"""

import run_experiment as rwe


def test_reset_reports_adb_failures(monkeypatch):
    """adb 非 0 时不能把包记成已停止"""
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (1, ""))

    result = rwe.reset_device(["微信", "未知应用"])

    assert result["stopped"] == []
    assert result["failed"][0]["package"] == "com.tencent.mm"
    assert result["failed"][0]["rc"] == 1
    assert result["not_mapped"] == ["未知应用"]
    assert result["home"] is False


def test_reset_reports_missing_adb(monkeypatch):
    monkeypatch.setattr(rwe, "adb", lambda args, timeout=15: (None, ""))

    result = rwe.reset_device(["微信"])

    assert result["stopped"] == []
    assert result["failed"][0]["rc"] is None
    assert result["home"] is False


def test_reset_stops_first_successful_candidate(monkeypatch):
    def fake_adb(args, timeout=15):
        if args[-1] == "com.miui.notes":
            return 1, ""
        return 0, ""

    monkeypatch.setattr(rwe, "adb", fake_adb)
    result = rwe.reset_device(["备忘录"])

    assert result["stopped"] == ["com.huawei.notepad"]
    assert result["home"] is True


def test_memo_app_has_rom_candidates():
    assert "备忘录" in rwe.PACKAGE_BY_APP
    assert len(rwe.PACKAGE_BY_APP["备忘录"]) >= 2


def test_device_lock_skips_when_busy(monkeypatch, tmp_path):
    lock = tmp_path / ".device.lock"
    lock.write_text("pid=999")
    monkeypatch.setattr(rwe, "DEVICE_LOCK", lock)

    acquired = []
    with rwe.device_lock(timeout=0) as ok:
        acquired.append(ok)

    assert acquired == [False]
    assert lock.exists(), "不能删除别人持有的锁"
