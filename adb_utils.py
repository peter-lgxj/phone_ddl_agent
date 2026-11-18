"""定位 adb 可执行文件。

项目里有多处以 `subprocess.run(["adb", ...])` 直接调用 adb，依赖 adb 在 PATH 上。
通过 pip 安装的 uiautomator2 / adbutils 自带 adb 二进制但不会写入 PATH，
因此这里按「环境变量 ADB_BINARY → PATH → adbutils 自带」的顺序解析。
"""
import os
import shutil
from pathlib import Path


def resolve_adb() -> str:
    override = os.getenv("ADB_BINARY", "")
    if override and Path(override).is_file():
        return override

    found = shutil.which("adb")
    if found:
        return found

    try:
        from adbutils import adb_path
        bundled = adb_path()
        if bundled and Path(bundled).is_file():
            return bundled
    except Exception:
        pass

    # 都没有时保留原行为：交给 subprocess 报 FileNotFoundError / 由用户修 PATH
    return "adb"


ADB = resolve_adb()
