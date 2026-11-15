from __future__ import annotations

"""
metrics.py — 聚合 results.jsonl 的六项实验指标

指标定义见 test.md 第二节：
  TSR  成功率        = 成功的任务数 / 任务数（多次执行取最优）
  SE   步骤效率      = 理论最少步数 / 实际步数（仅统计成功任务）
  Multi-App TSR      = 涉及 >=2 个 App 的任务成功率
  AT   平均执行时间  = 默认各任务平均耗时再取任务均值（秒），可用 --at-aggregate 切换为
                      「纳入统计的总耗时 ÷ 任务数」
  ST   单步平均耗时  = 总耗时 / 总步数（秒/步）
  CS   完成稳定性    = 所有重复次数均成功的任务数 / 任务数

成功与否必须人工判定：results.jsonl 的 success 字段，或同目录 judgement.json。
用法：python experiments/metrics.py experiments/results/<run_dir>
"""

import json
import sys
from pathlib import Path


def load_records(run_dir: Path):
    path = run_dir / "results.jsonl"
    if not path.exists():
        raise SystemExit(f"未找到 {path}")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    judgement = {}
    jpath = run_dir / "judgement.json"
    if jpath.exists():
        judgement = json.loads(jpath.read_text(encoding="utf-8"))
    return records, judgement


def resolve_success(records, judgement):
    """把 success 解析成 True/False，未判定的保持 None。

    judgement.json 支持两种粒度：
      {"T01": true}                     任务级，只能算 TSR，不能算 CS
      {"T01": [true, false, true]}      逐次判定，按 attempt 取对应值
    """
    for r in records:
        if isinstance(r.get("success"), bool):
            r["judgement_scope"] = "attempt"
            continue
        value = judgement.get(r["task_id"])
        if isinstance(value, list):
            idx = int(r.get("attempt", 1)) - 1
            if 0 <= idx < len(value) and isinstance(value[idx], bool):
                r["success"] = value[idx]
                r["judgement_scope"] = "attempt"
            else:
                r["success"] = None
                r["judgement_scope"] = None
        elif isinstance(value, bool):
            r["success"] = value
            r["judgement_scope"] = "task"   # 任务级：无法区分哪几次成功
        else:
            r["success"] = None
            r["judgement_scope"] = None
    return records


def group_by_task(records):
    grouped = {}
    for r in records:
        grouped.setdefault(r["task_id"], []).append(r)
    for attempts in grouped.values():
        attempts.sort(key=lambda x: x.get("attempt", 0))
    return grouped


def summarize(grouped, level=None, at_mode="all", at_aggregate="task_mean"):
    """at_mode 控制 AT 取哪些耗时：
      all     各任务全部 attempt 的平均耗时（默认，与旧结果兼容）
      success 只用判定为成功的 attempt
      best    每个任务取最快一次

    at_aggregate 控制 AT 如何跨任务聚合：
      task_mean      先算每个任务的平均耗时，再对任务等权平均（默认，旧结果）
      total_per_task 所有纳入统计的 attempt 总耗时 ÷ 任务数
    两者在每任务尝试次数相同时相等；尝试次数不等（超时、失败重放）时会分叉。
    """
    tasks = {tid: a for tid, a in grouped.items() if level is None or a[0].get("level") == level}
    judged = {tid: a for tid, a in tasks.items() if all(isinstance(x.get("success"), bool) for x in a)}
    unjudged = sorted(set(tasks) - set(judged))

    tsr = sum(1 for a in judged.values() if any(x["success"] for x in a))
    tsr_rate = tsr / len(judged) * 100 if judged else None

    multi = {tid: a for tid, a in judged.items() if len(a[0].get("apps") or []) >= 2}
    multi_tsr = (sum(1 for a in multi.values() if any(x["success"] for x in a)) / len(multi) * 100) if multi else None

    # CS 必须覆盖全部已判定任务；只要有任务用了任务级布尔，分母就会缩水，直接拒绝计算
    cs_pending = sorted(tid for tid, a in judged.items()
                        if any(x.get("judgement_scope") != "attempt" for x in a))
    cs_coverage = (len(judged) - len(cs_pending)) / len(judged) if judged else None
    if judged and not cs_pending:
        stable = sum(1 for a in judged.values() if all(x["success"] for x in a))
        cs = stable / len(judged) * 100
    else:
        cs = None

    se_values = []
    for a in judged.values():
        if not any(x["success"] for x in a):
            continue
        theory = a[0].get("theory_steps")
        steps = [x["steps"] for x in a if x["success"] and x.get("steps")]
        if theory and steps:
            se_values.append(theory / (sum(steps) / len(steps)))
    se = sum(se_values) / len(se_values) if se_values else None

    per_task_values = []
    for a in tasks.values():
        if at_mode == "success":
            values = [x["elapsed"] for x in a
                      if x.get("success") is True and x.get("elapsed") is not None]
        elif at_mode == "best":
            values = ([min(x["elapsed"] for x in a if x.get("elapsed") is not None)]
                      if any(x.get("elapsed") is not None for x in a) else [])
        else:
            values = [x["elapsed"] for x in a if x.get("elapsed") is not None]
        if values:
            per_task_values.append(values)

    if per_task_values:
        if at_aggregate == "total_per_task":
            at = sum(sum(v) for v in per_task_values) / len(per_task_values)
        else:
            at = sum(sum(v) / len(v) for v in per_task_values) / len(per_task_values)
    else:
        at = None

    timed = [x for a in tasks.values() for x in a if x.get("elapsed") is not None and x.get("steps")]
    st = sum(x["elapsed"] for x in timed) / sum(x["steps"] for x in timed) if timed else None

    return {
        "tasks": len(tasks), "judged": len(judged), "unjudged": unjudged,
        "TSR": tsr_rate, "MultiAppTSR": multi_tsr, "SE": se,
        "AT": at, "ST": st, "CS": cs,
        "CS_coverage": cs_coverage, "CS_pending": cs_pending,
    }


def fmt(value, unit="%"):
    return "N/A" if value is None else f"{value:.2f}{unit}"


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="聚合 results.jsonl 的实验指标")
    ap.add_argument("run_dir")
    ap.add_argument("--at-mode", choices=["all", "success", "best"], default="all",
                    help="AT 口径：all=全部尝试均值，success=仅成功尝试，best=每任务最快一次")
    ap.add_argument("--at-aggregate", choices=["task_mean", "total_per_task"], default="task_mean",
                    help="AT 跨任务聚合：task_mean=先任务内平均再任务间等权（默认）；"
                         "total_per_task=纳入统计的总耗时÷任务数（两者在尝试次数相等时相同）")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    records, judgement = load_records(run_dir)
    grouped = group_by_task(resolve_success(records, judgement))

    print(f"结果目录：{run_dir}（AT 口径：{args.at_mode}；聚合：{args.at_aggregate}）")
    print(f"{'范围':<10}{'任务数':>6}{'已判定':>8}{'TSR':>9}{'多AppTSR':>10}{'SE':>8}{'AT(s)':>9}{'ST(s/步)':>10}{'CS':>8}")
    for label, level in [("总体", None), ("Level1", 1), ("Level2", 2), ("Level3", 3)]:
        s = summarize(grouped, level, args.at_mode, args.at_aggregate)
        if not s["tasks"]:
            continue
        print(f"{label:<10}{s['tasks']:>6}{s['judged']:>8}"
              f"{fmt(s['TSR']):>9}{fmt(s['MultiAppTSR']):>10}{fmt(s['SE'], ''):>8}"
              f"{fmt(s['AT'], ''):>9}{fmt(s['ST'], ''):>10}{fmt(s['CS']):>8}")

    pending = sorted({tid for a in grouped.values() for tid in [a[0]['task_id']] if any(x.get('success') is None for x in a)})
    if pending:
        print(f"\n⚠️  以下任务尚未人工判定，未计入 TSR/CS/SE：{', '.join(pending)}")
        print("   可编辑 results.jsonl 的 success 字段，或在同目录写 judgement.json。")

    overall = summarize(grouped, None, args.at_mode, args.at_aggregate)
    if overall["judged"] and overall["CS"] is None:
        print(f"\n⚠️  CS 未计算：{len(overall['CS_pending'])}/{overall['judged']} 个已判定任务是任务级布尔，"
              f"CS 覆盖率仅 {overall['CS_coverage'] * 100:.0f}%。")
        print(f"   需要逐次判定的任务：{', '.join(overall['CS_pending'])}")
        print('   写成 {"T01": [true, false, true]}（按 attempt 顺序）才能计算 CS。')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
