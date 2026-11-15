from __future__ import annotations

"""
run_experiment.py — 按任务集重复执行并落盘 results.jsonl

需要连接手机并配置模型密钥；本脚本只负责采集，不判定任务成功与否。
每个任务前默认回桌面并 force-stop 本任务涉及的已知应用（--no-reset 关闭）。
manifest.json 记录 git SHA、模型全名、温度、任务集哈希与设备信息。

人工判定方式（二选一）：
  1. 执行后编辑 results.jsonl 的 "success" 字段（true/false）
  2. 在同目录写 judgement.json：
     {"T01": true}                    任务级，只能算 TSR
     {"T01": [true, false, true]}     逐次判定，才能算 CS

用法：
  python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3 --mode single
  python experiments/run_experiment.py --tasks experiments/tasks.json --mode multi --tag multi
  python experiments/run_experiment.py --tasks experiments/tasks.json --no-semantic --tag no_semantic
  python experiments/run_experiment.py --tasks experiments/tasks.json --dry-run
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 已知 App 的候选包名（同一 App 在不同 ROM 上包名不同），用于每个任务前 force-stop
PACKAGE_BY_APP = {
    "微信": ["com.tencent.mm"],
    "设置": ["com.android.settings"],
    "高德地图": ["com.autonavi.minimap"],
    "淘宝": ["com.taobao.taobao"],
    "美团": ["com.sankuai.meituan"],
    "网易云音乐": ["com.netease.cloudmusic"],
    "京东": ["com.jingdong.app.mall"],
    "大众点评": ["com.dianping.v1"],
    "备忘录": ["com.miui.notes", "com.huawei.notepad", "com.coloros.note", "com.android.notes"],
}

DEVICE_LOCK = ROOT / "runs" / ".device.lock"


def adb(args: list[str], timeout: int = 15):
    """返回 (returncode, stdout)。adb 缺失/超时/异常时 returncode 为 None"""
    try:
        proc = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip()
    except Exception:
        return None, ""


def _prop(name: str) -> str:
    rc, out = adb(["shell", "getprop", name])
    return out if rc == 0 else ""


def reset_device(apps: list[str]) -> dict:
    """按 test.md 尽力还原初始状态：回桌面 + force-stop 本任务涉及的应用。

    返回真实执行结果，只有 adb 返回 0 才算成功；无法自动清除应用内草稿/聊天记录。
    """
    result = {"home": False, "stopped": [], "failed": [], "not_mapped": []}
    rc, _ = adb(["shell", "input", "keyevent", "3"])
    result["home"] = rc == 0
    for app in apps:
        candidates = PACKAGE_BY_APP.get(app)
        if not candidates:
            result["not_mapped"].append(app)
            continue
        for pkg in candidates:
            rc, _ = adb(["shell", "am", "force-stop", pkg])
            if rc == 0:
                result["stopped"].append(pkg)
                break
            result["failed"].append({"app": app, "package": pkg, "rc": rc})
            if rc is None:   # adb 不可用，继续试其它包名没有意义
                break
    return result


@contextmanager
def device_lock(timeout: int = 30):
    """进程级设备锁：避免 runner 与 Web 界面/另一个 runner 同时重置同一台手机。

    拿不到锁时返回 False，由调用方跳过重置而不是强行打断正在运行的任务。
    """
    DEVICE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(DEVICE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} at={time.strftime('%Y-%m-%d %H:%M:%S')}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - DEVICE_LOCK.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > 300:                    # 陈旧锁（崩溃残留）自动清理
                try:
                    DEVICE_LOCK.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.time() > deadline:
                yield False
                return
            time.sleep(1)
    try:
        yield True
    finally:
        try:
            DEVICE_LOCK.unlink()
        except FileNotFoundError:
            pass


def device_info() -> dict:
    rc, out = adb(["devices"])
    connected = [line.split("\t")[0] for line in out.splitlines()[1:] if line.strip()]
    return {
        "model": _prop("ro.product.model"),
        "android": _prop("ro.build.version.release"),
        "resolution": (lambda rc2, o: o if rc2 == 0 else "")(*adb(["shell", "wm", "size"])),
        "adb_available": rc is not None,
        "connected_devices": connected,
    }


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


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
    ap.add_argument("--no-reset", action="store_true",
                    help="不在每个任务前 force-stop 应用（默认会尽力还原初始状态）")
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
        "step_retries": config.STEP_RETRIES,
        "temperature": config.LLM_TEMPERATURE,
        "semantic_analysis": config.ENABLE_SEMANTIC_ANALYSIS,
        "active_provider": config.ACTIVE_PROVIDER,
        "exec_model": config.PROVIDERS[config.ACTIVE_PROVIDER]["model"],
        "planner_provider": config.PLANNER_PROVIDER,
        "planner_model": config.PROVIDERS[config.PLANNER_PROVIDER]["model"],
        "task_file": str(task_file),
        "task_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
        "git_sha": git_sha(),
        "device": device_info(),
        "reset_before_task": not args.no_reset,
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
                    "reset": None,
                }
                print(f"\n=== {t['id']} 第 {attempt}/{repeat} 次：{t['task']}")
                if not args.no_reset:
                    with device_lock() as locked:
                        if locked:
                            record["reset"] = reset_device(t.get("apps", []))
                            res = record["reset"]
                            if res["failed"] or res["not_mapped"]:
                                print(f"⚠️  重置未完全成功：failed={res['failed']} "
                                      f"not_mapped={res['not_mapped']}（adb={'可用' if res['home'] or res['stopped'] else '不可用或设备离线'}）")
                        else:
                            record["reset"] = {"skipped": "device lock busy"}
                            print("⚠️  设备锁被占用（可能有其它任务在跑），本次跳过重置")
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
                    # test.md：超时或崩溃记为失败，避免被当作未判定而缩小 TSR 分母
                    record["success"] = False
                    traceback.print_exc()
                record["elapsed"] = round(time.time() - started, 1)
                record["steps"] = collect_steps(agent) if agent else None
                record["run_dir"] = collect_run_dir(agent) if agent else ""
                record["agent_status"] = getattr(agent, "last_status", None) if agent else None
                if record["agent_status"] == "timeout":
                    # 达最大步数不等于成功
                    record["success"] = False
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()

    print(f"\n结果已写入：{results_path}")
    print("下一步：人工标注 success 后执行 python experiments/metrics.py " + str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
