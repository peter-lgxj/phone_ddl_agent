"""一键演示：扫描来源 → 抽取 → 去重 → 冲突裁决 → 导出 → 人读摘要。

用法::

    python scripts/demo.py                 # 有手机走真机，无手机自动离线回放
    python scripts/demo.py --offline       # 强制离线，使用 examples/demo
    python scripts/demo.py --live           # 强制真机，读取 sources.yaml
    python scripts/demo.py --out demo_output

真机模式只读：不输入聊天框、不点发送、不发消息；来源由 ``sources.yaml`` 决定。
离线模式走本地 JSON 归档，不需要手机和 API key。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adb_utils import ADB  # noqa: E402
from ddl_agent.connectors.local_chat import LocalChatConnector  # noqa: E402
from ddl_agent.factory import build_agent  # noqa: E402
from ddl_agent.output import export_outputs  # noqa: E402

DEMO_CONFIG = ROOT / "examples" / "demo" / "sources.json"


class DemoError(RuntimeError):
    """演示可预期的失败；消息直接展示给用户，不打印栈。"""


def connected_devices() -> list[str]:
    try:
        completed = subprocess.run(
            [ADB, "devices"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
    except Exception:
        return []
    devices = []
    for line in (completed.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _offline(args) -> tuple[list, str, list[Path], datetime, list[str]]:
    config_path = Path(args.demo_config)
    if not config_path.exists():
        raise DemoError(f"离线演示配置不存在：{config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoError(f"离线演示配置格式非法：{config_path}：{exc}") from exc

    base = config_path.parent
    sources = []
    for item in data.get("sources") or []:
        path = Path(item.get("path") or "")
        if not path.is_absolute():
            path = base / path
        sources.append(LocalChatConnector(
            name=str(item.get("name") or path.stem),
            path=path,
            source_type=item.get("source_type") or "student_chat",
        ))
    if not sources:
        raise DemoError(f"离线演示配置没有 sources：{config_path}")

    now = datetime.fromisoformat(data["now"]) if data.get("now") else datetime.now().astimezone()
    engine = args.engine
    notes: list[str] = []
    if engine == "llm":
        from ddl_agent.llm.client import LLMClient
        if LLMClient.from_config() is None:
            notes.append(
                "未检测到可用 API key，已改用规则引擎；"
                "在 .env 配置 DEEPSEEK_API_KEY 后可加 --engine llm"
            )
            engine = "rules"
    agent, used = build_agent(sources, now=now, engine=engine, fixed_sources=True)
    if used == "rules" and args.engine != "rules" and not notes:
        notes.append(
            "未使用 LLM（未配置 API key 或 LLM_ENABLED=0），当前为规则引擎；"
            "配置 .env 中的 DEEPSEEK_API_KEY 后可用 --engine llm"
        )
    result = agent.run(args.query, write=False)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = export_outputs(result.records, out, now)
    return result.records, used, paths, now, notes


def _live(args) -> tuple[list, str, list[Path], datetime, list[str]]:
    config_path = Path(args.sources)
    if not config_path.exists():
        raise DemoError(
            f"未找到监控配置：{config_path}。"
            "复制 sources.example.yaml 为 sources.yaml 并改成自己的群名/收件箱。"
        )
    from ddl_agent.monitor.config import ConfigError, load_config
    from ddl_agent.monitor.runner import run_cycle

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise DemoError(f"监控配置不合法：{exc}") from exc

    notes: list[str] = []
    # 设备锁由 run_cycle 里每个来源各自获取；这里不能再持锁，
    # 否则会与外层非重入锁自我死锁，导致所有来源都报“设备锁被占用”。
    result = run_cycle(
        config, config_path=config_path, engine=args.engine,
        out_dir=args.out, state_dir=args.state,
    )
    if not result.write_ok:
        detail = "；".join(result.errors[:3]) or "所有来源都未读取成功"
        raise DemoError(f"真机读取失败，未生成产物：{detail}")
    if result.errors:
        notes.extend(result.errors)
    if result.engine == "rules" and args.engine != "rules":
        notes.append(
            "未使用 LLM（未配置 API key 或 LLM_ENABLED=0），当前为规则引擎；"
            "配置 .env 中的 DEEPSEEK_API_KEY 后可用 --engine llm"
        )
    paths = [Path(path) for path in result.outputs]
    now = datetime.fromisoformat(result.started_at)
    return result.records, result.engine, paths, now, notes


def _print_summary(records, engine: str, paths: list[Path], mode: str,
                   notes: list[str]) -> None:
    confirmed = sum(1 for record in records if record.status == "confirmed")
    uncertain = sum(1 for record in records if record.status == "uncertain")
    missing = sum(1 for record in records if record.status == "missing_deadline")
    print(
        f"演示完成（{mode} / {engine}）：共 {len(records)} 条 DDL"
        f"（confirmed {confirmed}，uncertain {uncertain}，missing_deadline {missing}）"
    )
    for index, record in enumerate(records, start=1):
        print(f"[{index}] {record.task_name} | {record.deadline_iso or '无截止时间'} | {record.status}")
        print(f"    来源：{', '.join(record.sources) or record.source}")
        if record.resolution_note:
            print(f"    裁决：{record.resolution_note}")
        if record.superseded_by:
            print(f"    同源覆盖：{len(record.superseded_by)} 条旧值保留在 data.json")
        if record.conflicts:
            print(f"    跨来源冲突：{len(record.conflicts)} 组，详见 data.json 的 conflicts")
    print("输出：")
    for path in paths:
        print(f"  - {path}")
    for note in notes:
        print(f"提示：{note}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DDL Agent 一键演示（只读）")
    parser.add_argument("--mode", choices=["auto", "live", "offline"], default="auto",
                        help="auto=有手机走真机、无手机离线；默认 auto")
    parser.add_argument("--offline", action="store_true", help="等价于 --mode offline")
    parser.add_argument("--live", action="store_true", help="等价于 --mode live")
    parser.add_argument("--sources", default="sources.yaml", help="真机模式的 sources.yaml")
    parser.add_argument("--demo-config", default=str(DEMO_CONFIG),
                        help="离线回放配置（默认 examples/demo/sources.json）")
    parser.add_argument("--out", default="demo_output", help="输出目录")
    parser.add_argument("--state", default="demo_output/state", help="真机模式的游标目录")
    parser.add_argument("--query", default="汇总所有课程 DDL", help="演示查询")
    parser.add_argument("--engine", choices=["rules", "llm", "auto"], default="rules",
                        help="抽取/裁决引擎；默认 rules，离线可复现")
    parser.add_argument("--no-fallback", action="store_true",
                        help="真机不可用时不要自动改走离线回放")
    parser.add_argument("--debug", action="store_true", help="失败时打印完整栈（排障用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode = args.mode
    if args.offline:
        mode = "offline"
    elif args.live:
        mode = "live"

    fallback_allowed = mode == "auto" and not args.no_fallback
    try:
        if mode != "offline":
            try:
                if not connected_devices():
                    raise DemoError(
                        "未检测到已连接手机：请开启 USB 调试并用 `adb devices` 确认。"
                    )
                records, engine, paths, _now, notes = _live(args)
                mode = "live"
            except DemoError as exc:
                if not fallback_allowed:
                    raise
                print(f"提示：{exc} 改用离线回放。")
                mode = "offline"
        if mode == "offline":
            records, engine, paths, _now, notes = _offline(args)
    except DemoError as exc:
        print(f"演示失败：{exc}")
        print("下一步：检查配置/设备后重试；没有手机可直接运行 `python scripts/demo.py --offline`。")
        return 2
    except KeyboardInterrupt:
        print("已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001 - 演示入口需要给出人话提示
        if args.debug:
            raise
        print(f"演示失败：{type(exc).__name__}: {exc}")
        print("下一步：加 `--debug` 查看完整栈；没有手机可先用 `--offline`。")
        return 2

    _print_summary(records, engine, paths, mode, notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
