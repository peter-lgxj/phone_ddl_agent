"""汇总一个或多个 run 目录的 step_NNN.json 耗时。

用法：
    python experiments/summarize_timings.py runs/20261117-211201
    python experiments/summarize_timings.py runs/merged_off runs/merged_on --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# 顶层 timings 字段；YOLO 单独从 snap_yolo.elapsed 读取
STAGES = [
    "decision",
    "observe",
    "ocr",
    "screenshot",
    "execute",
    "settle",
    "preprocess",
    "semantic",
    "completion",
]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _step_values(steps: list[dict], stage: str) -> list[float]:
    values = []
    for step in steps:
        timings = step.get("timings") or {}
        if stage == "yolo":
            value = (timings.get("snap_yolo") or {}).get("elapsed")
        else:
            value = timings.get(stage)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _step_totals(steps: list[dict]) -> list[float]:
    totals = []
    for step in steps:
        timings = step.get("timings") or {}
        totals.append(sum(
            float(timings.get(stage) or 0)
            for stage in ("decision", "observe", "ocr", "screenshot",
                          "execute", "settle", "preprocess", "semantic", "completion")
        ))
    return totals


def _summarize_group(steps: list[dict]) -> dict:
    """一组步骤（规则命中步或模型步）的阶段耗时与整步合计。"""
    stages = {}
    for stage in STAGES + ["yolo"]:
        values = _step_values(steps, stage)
        stages[stage] = {
            "samples": len(values),
            "total": round(sum(values), 3),
            "mean": round(statistics.fmean(values), 3) if values else 0.0,
            "p50": _percentile(values, 0.50),
            "p90": _percentile(values, 0.90),
        }
    totals = _step_totals(steps)
    return {
        "steps": len(steps),
        "stages": stages,
        "step_total": {
            "total": round(sum(totals), 3),
            "mean": round(statistics.fmean(totals), 3) if totals else 0.0,
            "p50": _percentile(totals, 0.50),
            "p90": _percentile(totals, 0.90),
        },
    }


def summarize_run(run_dir: str | Path) -> dict:
    path = Path(run_dir)
    steps = []
    for step_file in sorted(path.glob("step_*.json")):
        try:
            steps.append(json.loads(step_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    # 规则命中步不调用模型，和模型步混在一起会把提速差异稀释掉，必须分组统计
    rule_steps = [s for s in steps if (s.get("timings") or {}).get("rule_hit")]
    model_steps = [s for s in steps if not (s.get("timings") or {}).get("rule_hit")]
    groups = {
        "rule": _summarize_group(rule_steps),
        "model": _summarize_group(model_steps),
    }
    combined = _summarize_group(steps)
    rule_seconds = groups["rule"]["step_total"]["total"]
    total_seconds = combined["step_total"]["total"]

    return {
        "run": path.name,
        "steps": len(steps),
        "rule_steps": len(rule_steps),
        "model_steps": len(model_steps),
        # 规则步总耗时占全部步耗时的比例；规则步关闭时为 0
        "rule_time_ratio": (
            round(rule_seconds / total_seconds, 3) if total_seconds else 0.0
        ),
        "stages": combined["stages"],
        "step_total": combined["step_total"],
        "groups": groups,
    }


def _print_run(summary: dict) -> None:
    print(f"\n== {summary['run']}（{summary['steps']} 步）==")
    print(f"{'stage':<12}{'n':>4}{'total':>10}{'mean':>10}{'p50':>10}{'p90':>10}")
    for stage, row in summary["stages"].items():
        if not row["samples"]:
            continue
        print(f"{stage:<12}{row['samples']:>4}{row['total']:>10.2f}"
              f"{row['mean']:>10.2f}{row['p50']:>10.2f}{row['p90']:>10.2f}")
    row = summary["step_total"]
    print(f"{'step_total':<12}{summary['steps']:>4}{row['total']:>10.2f}"
          f"{row['mean']:>10.2f}{row['p50']:>10.2f}{row['p90']:>10.2f}")
    print(f"\n  分组：规则命中 {summary['rule_steps']} 步 / 模型 "
          f"{summary['model_steps']} 步，规则步耗时占比 "
          f"{summary['rule_time_ratio'] * 100:.1f}%")
    print(f"  {'group':<10}{'n':>4}{'total':>10}{'mean':>10}{'p50':>10}{'p90':>10}")
    for name in ("rule", "model"):
        group = summary["groups"][name]
        totals = group["step_total"]
        print(f"  {name:<10}{group['steps']:>4}{totals['total']:>10.2f}"
              f"{totals['mean']:>10.2f}{totals['p50']:>10.2f}{totals['p90']:>10.2f}")


def _print_compare(summaries: list[dict]) -> None:
    if len(summaries) < 2:
        return
    print("\n== 批量对比（mean 秒 / p90 秒）==")
    header = f"{'stage':<12}" + "".join(f"{s['run'][:18]:>22}" for s in summaries)
    print(header)
    for stage in STAGES + ["yolo", "step_total"]:
        cells = []
        for summary in summaries:
            row = (summary["step_total"] if stage == "step_total"
                   else summary["stages"][stage])
            cells.append(f"{row['mean']:.2f} / {row['p90']:.2f}".rjust(22))
        print(f"{stage:<12}" + "".join(cells))


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总 run 目录的每步阶段耗时")
    parser.add_argument("runs", nargs="+", help="一个或多个 run 目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = parser.parse_args()

    summaries = [summarize_run(run) for run in args.runs]
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return
    for summary in summaries:
        _print_run(summary)
    _print_compare(summaries)


if __name__ == "__main__":
    main()
