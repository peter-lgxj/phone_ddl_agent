"""设备级互斥锁：同一时刻只允许一个进程操作手机。

所有入口（run.py / app.py / frontend.py / experiments/run_experiment.py）共用
<repo>/runs/.device.lock。锁文件记录 token + pid，释放时校验 token，
僵尸锁只在持有进程已退出时才回收，避免互删。
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent / "runs" / ".device.lock"
STALE_UNREADABLE_SECONDS = 600


def _pid_alive(pid: int) -> bool:
    """不依赖 psutil 的进程存活检测"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock() -> dict | None:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_stale(info: dict | None) -> bool:
    if info is None:
        try:
            return time.time() - LOCK_PATH.stat().st_mtime > STALE_UNREADABLE_SECONDS
        except FileNotFoundError:
            return True
    return not _pid_alive(int(info.get("pid", -1)))


def acquire(timeout: int = 30) -> str | None:
    """返回 token 表示持有锁；None 表示超时未获取"""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    token = uuid.uuid4().hex
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({
                    "token": token,
                    "pid": os.getpid(),
                    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                }).encode())
            except OSError:
                # 先关 fd 再删（Windows 上打开状态无法 unlink），不能留空锁
                os.close(fd)
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    pass
                raise
            else:
                os.close(fd)
            return token
        except FileExistsError:
            info = _read_lock()
            if _is_stale(info):
                stale_token = (info or {}).get("token")
                current = _read_lock()
                # 只删刚才判定为僵尸的那份，避免误删别人新建的锁
                if (current or {}).get("token") == stale_token:
                    try:
                        LOCK_PATH.unlink()
                    except FileNotFoundError:
                        pass
                continue
            if time.time() > deadline:
                return None
            time.sleep(0.5)


def release(token: str) -> None:
    """只释放自己持有的锁"""
    info = _read_lock()
    if (info or {}).get("token") == token:
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def device_lock(timeout: int = 30):
    token = acquire(timeout)
    try:
        yield token is not None
    finally:
        if token is not None:
            release(token)
