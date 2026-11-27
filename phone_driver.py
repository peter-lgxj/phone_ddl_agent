"""驱动选择：uiautomator2 优先，Android 17 等 RPC 不可用时自动回退到 adb dump。

``SCREEN_DRIVER`` 取值：
- ``auto``（默认）：先试 uiautomator2；未安装该模块、或探测失败属于兼容性问题时回退 adb_dump
- ``uiautomator2``：强制 u2；失败时给出可执行的错误提示，不静默回退
- ``adb_dump``：强制使用 ADB 回退驱动
"""

from __future__ import annotations

import config
from adb_controller import AdbDumpController
from phone_controller import PhoneController, Uiautomator2CompatibilityError

DRIVER_ALIASES = {
    "auto": "auto",
    "uiautomator2": "uiautomator2",
    "u2": "uiautomator2",
    "adb": "adb_dump",
    "adb_dump": "adb_dump",
    "dump": "adb_dump",
}


def resolve_driver_mode(mode: str | None = None) -> str:
    value = (mode if mode is not None else getattr(config, "SCREEN_DRIVER", "auto"))
    key = str(value or "auto").strip().lower().replace("-", "_")
    if key not in DRIVER_ALIASES:
        raise ValueError(
            f"未知的 SCREEN_DRIVER「{value}」，可选：auto / uiautomator2 / adb_dump"
        )
    return DRIVER_ALIASES[key]


def create_phone_controller(locator, mode: str | None = None):
    """按配置创建控制器；auto 模式在 u2 缺失或兼容性错误时回退 ADB。"""
    resolved = resolve_driver_mode(mode)
    if resolved == "adb_dump":
        return AdbDumpController(locator)
    if resolved == "uiautomator2":
        try:
            return PhoneController(locator)
        except ImportError as exc:
            raise RuntimeError(
                "SCREEN_DRIVER=uiautomator2 但未安装 uiautomator2："
                "请执行 pip install 'uiautomator2>=3.7.1'，或改用 SCREEN_DRIVER=adb_dump。"
            ) from exc

    try:
        return PhoneController(locator)
    except ImportError as exc:
        # 模块根本没装（ModuleNotFoundError）：和 Android 17 兼容性错误同级处理，
        # 不能让缺依赖把整轮扫描判成失败。
        print("\n  ⚠️  未安装 uiautomator2，使用 adb_dump")
        print(f"      {exc}\n")
        return AdbDumpController(locator)
    except Uiautomator2CompatibilityError as exc:
        print("\n  ⚠️  uiautomator2 不可用，自动回退到 ADB dump 驱动")
        print(f"      {exc}\n")
        return AdbDumpController(locator)
