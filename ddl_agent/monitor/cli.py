"""监控配置与定期检索的命令行入口。

示例::

    python -m ddl_agent.monitor.cli add "监控微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次"
    python -m ddl_agent.monitor.cli list
    python -m ddl_agent.monitor.cli run --once
    python -m ddl_agent.monitor.cli run            # 常驻，按配置周期循环

默认配置文件为当前目录的 ``sources.yaml``；``run`` 每轮都会重新读取配置，
因此运行期间增删来源不需要重启进程。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from .config import (
    APP_LABELS,
    ConfigError,
    add_from_text,
    load_config,
    parse_schedule,
    remove_source,
    save_config,
    set_enabled,
)
from .cursor import CursorStore
from .runner import run_cycle

DEFAULT_CONFIG = "sources.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddl-monitor", description="手机 App DDL 定期监控")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径（默认 sources.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="用一句自然语言新增监控目标")
    add.add_argument("text", help="例如：监控微信「21级课程群」，每6小时一次")

    sub.add_parser("list", help="列出当前配置与自然语言来源")

    remove = sub.add_parser("remove", help="删除一个监控来源")
    remove.add_argument("source_id")

    for name, text in (("enable", "启用"), ("disable", "停用")):
        cmd = sub.add_parser(name, help=f"{text}一个监控来源")
        cmd.add_argument("source_id")

    schedule = sub.add_parser("schedule", help="单独设置检索周期")
    schedule.add_argument("text", help="例如：每6小时 / 每天一次 / 每30分钟")

    reset = sub.add_parser("reset-cursor", help="重置增量游标（下一次会重新做基线扫描）")
    reset.add_argument("--source", dest="source_id", default=None)

    run = sub.add_parser("run", help="执行一轮或常驻定期检索")
    run.add_argument("--once", action="store_true", help="只跑一轮")
    run.add_argument("--engine", default="auto", choices=["auto", "llm", "rules"])
    run.add_argument("--out", default=None, help="覆盖输出目录")
    run.add_argument("--state", default=None, help="覆盖状态目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config_path = Path(args.config)
    try:
        config = load_config(config_path)
        if args.command == "add":
            added = add_from_text(config, args.text)
            save_config(config, config_path)
            for source in added:
                print(f"已添加 {source.id}：{APP_LABELS[source.app]} / {source.target}")
                print(f"  来源原句：{source.nl_text}")
            print(f"配置文件：{config_path}")
            return 0

        if args.command == "list":
            _print_config(config, config_path)
            return 0

        if args.command == "remove":
            if not remove_source(config, args.source_id):
                print(f"没有找到来源：{args.source_id}", file=sys.stderr)
                return 1
            save_config(config, config_path)
            print(f"已删除 {args.source_id}")
            return 0

        if args.command in ("enable", "disable"):
            enabled = args.command == "enable"
            if not set_enabled(config, args.source_id, enabled):
                print(f"没有找到来源：{args.source_id}", file=sys.stderr)
                return 1
            save_config(config, config_path)
            print(f"已{'启用' if enabled else '停用'} {args.source_id}")
            return 0

        if args.command == "schedule":
            normalized, minutes = parse_schedule(args.text)
            config.schedule = normalized
            config.interval_minutes = minutes
            save_config(config, config_path)
            print(f"调度周期：{normalized}（{minutes} 分钟）")
            return 0

        if args.command == "reset-cursor":
            state_dir = Path(args.state or config.state_dir)
            store = CursorStore(state_dir / "cursors.json")
            store.reset(args.source_id)
            print("已重置" + (f" {args.source_id}" if args.source_id else "全部") + "游标")
            return 0

        if args.command == "run":
            return _run(args, config, config_path)
    except ConfigError as exc:
        print(f"拒绝执行：{exc}", file=sys.stderr)
        return 2
    return 1


def _print_config(config, config_path: Path) -> None:
    print(f"配置文件：{config_path}")
    print(f"调度周期：{config.schedule}（{config.interval_minutes} 分钟）")
    print(f"输出目录：{config.output_dir}；状态目录：{config.state_dir}")
    if not config.sources:
        print("尚未配置任何监控来源。用 `add` 添加，例如：")
        print('  python -m ddl_agent.monitor.cli add "监控微信「21级课程群」，每6小时一次"')
        return
    print("监控来源：")
    for source in config.sources:
        mark = "x" if source.enabled else " "
        print(
            f"- [{mark}] {source.id} | {APP_LABELS.get(source.app, source.app)} | "
            f"{source.kind} | {source.target} | 最近 {source.lookback_days} 天"
        )
        if source.nl_text:
            print(f"    来自：{source.nl_text}")
        if source.parse_note:
            print(f"    解析：{source.parse_note}")


def _run(args, config, config_path: Path) -> int:
    out_dir = args.out or config.output_dir
    state_dir = args.state or config.state_dir
    while True:
        try:
            cycle_config = load_config(config_path)
        except Exception:  # noqa: BLE001 - 运行期间配置损坏不能中断常驻进程
            traceback.print_exc()
            cycle_config = config
        result = run_cycle(
            cycle_config,
            config_path=config_path,
            engine=args.engine,
            out_dir=out_dir,
            state_dir=state_dir,
        )
        print(
            f"[{result.cycle_id}] engine={result.engine} "
            f"新增 {result.inserted} / 更新 {result.updated} / 跳过 {result.skipped}"
        )
        for source in result.sources:
            if "source_id" not in source:
                continue
            state = "基线" if source.get("baseline") else "增量"
            print(
                f"  - {source['source_id']}（{state}）：读取 {source.get('fetched', 0)} 条，"
                f"新消息 {source.get('new_messages', 0)}，新候选 {source.get('new_candidates', 0)}"
            )
        for error in result.errors:
            print(f"  ! {error}", file=sys.stderr)
        for path in result.outputs:
            print(f"  输出：{path}")

        if args.once:
            return 0 if result.write_ok and not result.errors else 1
        interval = max(5, int(cycle_config.interval_minutes))
        print(f"{interval} 分钟后进行下一轮（Ctrl+C 退出）")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("已停止")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
