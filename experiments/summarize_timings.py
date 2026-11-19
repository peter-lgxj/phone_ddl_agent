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


def summarize_run(run_dir: str | Path) -> dict:
    path = Path(run_dir)
    steps = []
    for step_file in sorted(path.glob("step_*.json")):
        try:
            steps.append(json.loads(step_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

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

    step_totals = []
    for step in steps:
        timings = step.get("timings") or {}
        total = sum(
            float(timings.get(stage) or 0)
            for stage in ("decision", "observe", "ocr", "screenshot",
                          "execute", "settle", "preprocess", "semantic", "completion")
        )
        step_totals.append(total)

    return {
        "run": path.name,
        "steps": len(steps),
        "stages": stages,
        "step_total": {
            "total": round(sum(step_totals), 3),
            "mean": round(statistics.fmean(step_totals), 3) if step_totals else 0.0,
            "p50": _percentile(step_totals, 0.50),
            "p90": _percentile(step_totals, 0.90),
        },
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
