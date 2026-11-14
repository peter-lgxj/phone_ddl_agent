from __future__ import annotations

"""
run_experiment.py — 按任务集重复执行并落盘 results.jsonl

需要连接手机并配置模型密钥；本脚本只负责采集，不判定任务成功与否。
人工判定方式（二选一）：
  1. 执行后编辑 results.jsonl 的 "success" 字段（true/false）
  2. 在同目录写 judgement.json：{"T01": true, "T09": false}

用法：
  python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3 --mode single
  python experiments/run_experiment.py --tasks experiments/tasks.json --mode multi --tag multi
  python experiments/run_experiment.py --tasks experiments/tasks.json --no-semantic --tag no_semantic
  python experiments/run_experiment.py --tasks experiments/tasks.json --dry-run
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_tasks(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    for t in tasks:
        missing = {"id", "task"} - set(t)
        if missing:
            raise SystemExit(f"任务 {t} 缺少字段：{sorted(missing)}")
    return tasks


def collect_steps(agent) -> int | None:
    """优先用多应用累计步数，其次用最后一次执行的 memory"""
    total = getattr(agent, "total_steps", 0)
    if total:
        return total
    memory = getattr(agent, "last_memory", None)
    return len(memory.steps) if memory else None


def collect_run_dir(agent) -> str:
    """单任务取 agent.run_dir，多应用取最后一次子任务的目录"""
    path = getattr(agent, "run_dir", None)
    if path is None:
        exec_agent = getattr(agent, "exec_agent", None)
        path = getattr(exec_agent, "run_dir", None)
    return str(path) if path else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="重复执行任务集并落盘结果")
    ap.add_argument("--tasks", default=str(ROOT / "experiments" / "tasks.json"))
    ap.add_argument("--repeat", type=int, default=None, help="每个任务的重复次数，默认取 tasks.json 的 default_repeat")
    ap.add_argument("--mode", choices=["single", "multi"], default="single")
    ap.add_argument("--tag", default="", help="结果目录后缀，便于区分消融实验")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--no-semantic", action="store_true", help="消融：关闭语义分析层")
    ap.add_argument("--baseline", default="", help="baseline 名称；提供时忽略 --mode，走子进程适配器")
    ap.add_argument("--baseline-command", default="", help="baseline 执行命令，从 stdin 读任务 JSON")
    ap.add_argument("--out", default=str(ROOT / "experiments" / "results"))
    ap.add_argument("--dry-run", action="store_true", help="只打印任务集，不导入执行器")
    args = ap.parse_args()

    task_file = Path(args.tasks)
    data = json.loads(task_file.read_text(encoding="utf-8"))
    tasks = load_tasks(task_file)
    repeat = args.repeat or (data.get("default_repeat", 3) if isinstance(data, dict) else 3)
    repeat_by_level = data.get("repeat_by_level", {}) if isinstance(data, dict) else {}

    def repeat_for(task):
        """按任务等级覆盖重复次数（test.md 中 Level 3 为 2 次），--repeat 可强制统一"""
        if args.repeat:
            return args.repeat
        return int(repeat_by_level.get(str(task.get("level")), repeat))

    print(f"任务集：{task_file}（{len(tasks)} 个任务），重复 {repeat} 次"
          f"（按等级覆盖：{repeat_by_level or '无'}），模式 {args.mode}")
    if args.dry_run:
        for t in tasks:
            print(f"  {t['id']} L{t.get('level', '?')} theory={t.get('theory_steps', '?')} "
                  f"repeat={repeat_for(t)} {t['task']}")
        print("dry-run：未导入执行器，未连接设备，未调用模型")
        return 0

    # 延迟导入：dry-run 与依赖检查不需要加载 torch/easyocr
    import config
    if args.no_semantic:
        config.ENABLE_SEMANTIC_ANALYSIS = False
    if args.max_steps:
        config.MAX_STEPS = args.max_steps

    adapter = None
    if args.baseline:
        if not args.baseline_command:
            raise SystemExit("--baseline 需要同时提供 --baseline-command")
        import shlex
        from experiments.baselines.adapter import SubprocessAdapter
        adapter = SubprocessAdapter(args.baseline, shlex.split(args.baseline_command))
        print(f"baseline 模式：{args.baseline} -> {args.baseline_command}")
    else:
        from screen_agent import ScreenAgent
        from multi_agent import MultiAppAgent

    stamp = time.strftime("%Y%m%d-%H%M%S") + (f"_{args.tag}" if args.tag else "")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": f"baseline:{args.baseline}" if adapter else args.mode,
        "repeat": repeat,
        "repeat_by_level": repeat_by_level,
        "max_steps": config.MAX_STEPS,
        "semantic_analysis": config.ENABLE_SEMANTIC_ANALYSIS,
        "active_provider": config.ACTIVE_PROVIDER,
        "planner_provider": config.PLANNER_PROVIDER,
        "task_file": str(task_file),
        "tag": args.tag,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    results_path = out_dir / "results.jsonl"
    with open(results_path, "a", encoding="utf-8") as f:
        for t in tasks:
            task_repeat = repeat_for(t)
            for attempt in range(1, task_repeat + 1):
                record = {
                    "task_id": t["id"],
                    "level": t.get("level"),
                    "apps": t.get("apps", []),
                    "theory_steps": t.get("theory_steps"),
                    "mode": args.mode,
                    "attempt": attempt,
                    "success": None,          # 需人工判定
                    "status": "unknown",
                    "steps": None,
                    "elapsed": None,
                    "run_dir": "",
                }
                print(f"\n=== {t['id']} 第 {attempt}/{repeat} 次：{t['task']}")
                started = time.time()
                agent = None
                try:
                    if adapter is not None:
                        result = adapter.run(t["task"])
                        record.update({
                            "status": "finished",
                            "mode": f"baseline:{args.baseline}",
                            "steps": result.steps,
                            "elapsed": result.elapsed,
                            "baseline_summary": result.summary,
                            "baseline_raw": result.raw,
                        })
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        f.flush()
                        continue
                    if args.mode == "multi":
                        agent = MultiAppAgent()
                    else:
                        agent = ScreenAgent(max_steps=config.MAX_STEPS)
                    agent.run(t["task"])
                    record["status"] = "finished"
                except Exception as e:
                    record["status"] = "error"
                    record["error"] = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                record["elapsed"] = round(time.time() - started, 1)
                record["steps"] = collect_steps(agent) if agent else None
                record["run_dir"] = collect_run_dir(agent) if agent else ""
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()

    print(f"\n结果已写入：{results_path}")
    print("下一步：人工标注 success 后执行 python experiments/metrics.py " + str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
