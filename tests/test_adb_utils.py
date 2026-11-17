"""adb 可执行文件解析：环境变量优先，其次 PATH，最后 adbutils 自带。"""
import adb_utils


def test_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "adb.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("ADB_BINARY", str(fake))
    assert adb_utils.resolve_adb() == str(fake)


def test_ignores_missing_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ADB_BINARY", str(tmp_path / "nope.exe"))
    resolved = adb_utils.resolve_adb()
    assert resolved != str(tmp_path / "nope.exe")
    assert isinstance(resolved, str) and resolved


def test_module_level_adb_is_string():
    assert isinstance(adb_utils.ADB, str) and adb_utils.ADB
