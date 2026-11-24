"""双击启动入口背后的逻辑：环境检查、人话报错、单轮或常驻检索、打开日历页。

运行环境是电脑，操作对象是手机：本程序通过 ADB 驱动手机，
微信 / QQ / Outlook 的界面全部在手机上被读取，手机端不安装本项目组件。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from .config import load_config
from .runner import CycleResult, run_cycle

NO_DEVICE_MSG = (
    "没有检测到手机。\n"
    "下一步：用 USB 线连接手机并解锁屏幕；在「设置 → 开发者选项」打开「USB 调试」；"
    "手机弹出授权框时点「允许」。"
)
UNAUTHORIZED_MSG = (
    "手机未授权 USB 调试。\n"
    "下一步：解锁手机，在弹窗里点「允许」（可勾选“始终允许”）；"
    "如果没弹窗，到开发者选项里「撤销 USB 调试授权」后重新插拔数据线。"
)
OFFLINE_MSG = (
    "手机状态是 offline。\n"
    "下一步：重新插拔数据线，或在命令行执行 adb kill-server 后重试；"
    "同时确认手机没有停留在“仅充电”模式。"
)
MULTI_DEVICE_MSG = (
    "检测到多台设备。本项目同一时刻只驱动一台手机。\n"
    "下一步：断开多余的手机或模拟器，只保留要监控的那一台。"
)
ADB_MISSING_MSG = (
    "找不到 adb。\n"
    "下一步：安装 Android Platform Tools，并把 platform-tools 目录加入 PATH；"
    "或设置环境变量 ADB_BINARY 指向 adb.exe 的完整路径。"
)


def resolve_adb() -> str | None:
    try:
        from adb_utils import resolve_adb as _resolve

        candidate = _resolve()
    except Exception:  # noqa: BLE001 - 环境探测失败时继续尝试 PATH
        candidate = ""
    if candidate and Path(candidate).is_file():
        return candidate
    return shutil.which("adb")


def parse_devices(output: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("List of devices", "*")):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def device_problem(devices: list[tuple[str, str]]) -> str | None:
    if not devices:
        return NO_DEVICE_MSG
    states = [state for _, state in devices]
    if "unauthorized" in states:
        return UNAUTHORIZED_MSG
    if "offline" in states:
        return OFFLINE_MSG
    if len(devices) > 1:
        return MULTI_DEVICE_MSG
    if states[0] != "device":
        return f"手机状态异常：{states[0]}。\n下一步：重新插拔数据线，或执行 adb kill-server 后重试。"
    return None


def check_device(adb: str) -> str | None:
    try:
        proc = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return ADB_MISSING_MSG
    except subprocess.TimeoutExpired:
        return "adb 响应超时。\n下一步：执行 adb kill-server 后重试，并确认 USB 线可以传输数据。"
    except OSError as exc:
        return f"调用 adb 失败：{exc}\n下一步：确认 ADB_BINARY 指向可执行文件。"
    if proc.returncode != 0:
        return f"adb devices 返回错误：{(proc.stderr or proc.stdout).strip()}\n下一步：执行 adb kill-server 后重试。"
    return device_problem(parse_devices(proc.stdout))


def check_device_lock() -> str | None:
    try:
        from device_lock import device_lock
    except Exception as exc:  # noqa: BLE001 - 锁模块不可用不阻断离线演示
        return f"设备锁模块不可用：{exc}"
    with device_lock(timeout=1) as acquired:
        if acquired:
            return None
    return (
        "手机正被本项目的另一个窗口占用（设备锁）。\n"
        "下一步：关闭其他正在运行的 run.py / app.py / 扫描窗口，或等它结束；"
        "确认没有其他窗口后，可删除 runs/.device.lock 再重试。"
    )


def _print_cycle(result: CycleResult) -> None:
    status_label = {"ok": "成功", "partial": "部分成功", "failed": "失败"}.get(
        result.status, result.status
    )
    print(
        f"[{result.cycle_id}] {status_label}"
        f"（成功来源 {result.sources_ok}）："
        f"新增 {result.inserted} / 更新 {result.updated} / 跳过 {result.skipped}"
    )
    for source in result.sources:
        if "source_id" not in source:
            continue
        stage = "基线" if source.get("baseline") else "增量"
        print(
            f"  - {source['source_id']}（{stage}）：读取 {source.get('fetched', 0)} 条，"
            f"新消息 {source.get('new_messages', 0)}，新候选 {source.get('new_candidates', 0)}"
        )
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)


def open_calendar(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())
    except OSError as exc:
        print(f"  无法自动打开日历页：{exc}；请手动打开 {path}")


def _choose_mode(mode: str | None) -> str:
    if mode:
        return mode
    if not sys.stdin.isatty():
        return "once"
    print("请选择运行模式：")
    print("  1. 仅扫描一次（马上跑一遍就退出）")
    print("  2. 定期（按配置周期反复跑，Ctrl+C 停止）")
    answer = input("输入 1 或 2，直接回车默认 2：").strip()
    return "once" if answer == "1" else "loop"


def _run_once(config, args, open_page: bool = True) -> CycleResult:
    print("正在读取手机应用，请保持屏幕解锁……")
    result = run_cycle(
        config,
        config_path=args.config,
        engine=args.engine,
        out_dir=args.out,
        state_dir=args.state,
    )
    _print_cycle(result)
    out = Path(args.out or config.output_dir)
    calendar = out / "calendar.html"
    if open_page and str(calendar) in result.outputs:
        # 失败轮不打开旧日历，避免把上一轮产物当成本轮结果
        open_calendar(calendar)
    print(f"结果目录：{out.resolve()}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="手机 App DDL 监控启动器")
    parser.add_argument("--config", default="sources.yaml")
    parser.add_argument("--mode", choices=["once", "loop"], default=None)
    parser.add_argument("--engine", default="auto", choices=["auto", "llm", "rules"])
    parser.add_argument("--out", default=None)
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)

    print("=" * 56)
    print(" 手机 DDL 监控")
    print(" 运行环境：这台电脑；操作对象：手机上的微信 / QQ / Outlook")
    print(" 电脑通过 ADB 驱动手机，手机端不安装任何本项目组件，也不需要 root")
    print("=" * 56)

    adb = resolve_adb()
    if not adb:
        print(f"\n[错误] {ADB_MISSING_MSG}")
        return 2
    print(f"\n[1/3] adb：{adb}")

    problem = check_device(adb)
    if problem:
        print(f"[错误] {problem}")
        return 2
    print("[2/3] 手机已连接并授权")

    lock_problem = check_device_lock()
    if lock_problem:
        print(f"[错误] {lock_problem}")
        return 2
    print("[3/3] 设备空闲，可以开始")

    config = load_config(args.config)
    if not config.sources:
        print(
            "\n[提示] 还没有配置检索目标（sources.yaml）。\n"
            "下一步：在项目目录运行\n"
            '  python -m ddl_agent.monitor.cli add "定期读取微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次"\n'
            "也可以把 sources.example.yaml 复制为 sources.yaml 后编辑。"
        )
        return 2

    mode = _choose_mode(args.mode)
    if mode == "once":
        try:
            result = _run_once(config, args)
        except KeyboardInterrupt:
            print("\n已停止")
            return 130
        except Exception as exc:  # noqa: BLE001 - 单轮入口必须给人话提示，不抛栈
            print(f"[错误] 本轮执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "下一步：检查手机连接与 sources.yaml 后重试；"
                "没有手机可先用 `python scripts/demo.py --offline`。"
            )
            return 1
        return 0 if result.write_ok and not result.errors else 1

    first = True
    while True:
        try:
            config = load_config(args.config)
            _run_once(config, args, open_page=first)
        except KeyboardInterrupt:
            print("\n已停止")
            return 0
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因单轮异常退出
            print(f"[错误] 本轮执行失败：{exc}", file=sys.stderr)
        interval = max(5, int(config.interval_minutes))
        print(f"{interval} 分钟后进行下一轮（Ctrl+C 停止）")
        first = False
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\n已停止")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
