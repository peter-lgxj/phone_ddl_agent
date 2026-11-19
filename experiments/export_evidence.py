"""把 run 目录导出为可入仓的最小脱敏证据集。

默认剔除全屏截图、发送图、原始 observation/action 文本与 OCR 原文，
只保留步数、动作类型、阶段耗时、token usage、图片尺寸与哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from summarize_timings import summarize_run

README = """# 脱敏运行证据

本目录由 `experiments/export_evidence.py` 生成，源 run：`{run}`。

保留：
- `summary.json`：步数、各阶段耗时、P50/P90
- `steps.jsonl`：每步动作类型、`timings`、`usage`、观察长度与 SHA256、图片尺寸
- `README.md`：本说明

剔除（不公开）：
- `step_*_current.png` / `step_*_observe.png` / `step_*_sent.jpg` 全屏截图
- 原始 `task`、`action` 文本、`observation` 文本、`target` 与 OCR 识别文字
- 任何应用账号、群名、课程名或聊天内容

用途：只支持耗时/token 复核与失败步骤定位，不包含可还原的聊天内容。
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _sanitize_timings(timings: dict) -> dict:
    result = {}
    for key, value in (timings or {}).items():
        if key == "ocr_snap":
            moves = value.get("moves") if isinstance(value, dict) else None
            result[key] = {"moves": len(moves or [])}
        elif key in {"snap_tree", "snap_ocr", "snap_yolo"} and isinstance(value, dict):
            result[key] = {
                name: metric for name, metric in value.items()
                if isinstance(metric, (int, float, str, bool)) or metric is None
            }
        elif isinstance(value, (int, float, str, bool)) or value is None:
            result[key] = value
    return result


def _sanitize_step(step: dict) -> dict:
    action = step.get("action") or {}
    observation = str(step.get("observation") or "")
    image = step.get("image") or {}
    return {
        "step": step.get("step"),
        "action_type": action.get("action_type"),
        "action_fields": sorted(action.keys()),
        "target_present": bool(action.get("target")),
        "observation_chars": len(observation),
        "observation_sha256": _sha256(observation),
        "observation_scope": step.get("observation_scope", "post_action"),
        "timings": _sanitize_timings(step.get("timings") or {}),
        "usage": step.get("usage"),
        "image": {
            key: image.get(key)
            for key in ("orig_size", "sent_size", "scale_x", "scale_y",
                        "resized", "enhanced", "bytes")
            if key in image
        },
    }


def export_evidence(run_dir: str | Path, out_dir: str | Path) -> dict:
    run = Path(run_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    steps = []
    for step_file in sorted(run.glob("step_*.json")):
        try:
            steps.append(_sanitize_step(json.loads(step_file.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue

    summary = summarize_run(run)
    summary["source_run"] = run.name
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with open(out / "steps.jsonl", "w", encoding="utf-8") as f:
        for step in steps:
            f.write(json.dumps(step, ensure_ascii=False) + "\n")
    (out / "README.md").write_text(README.format(run=run.name), encoding="utf-8")
    return {"run": run.name, "steps": len(steps), "out": str(out)}


def main() -> None:
    parser = argparse.ArgumentParser(description="导出脱敏 run 证据")
    parser.add_argument("run", help="runs/ 下的 run 目录")
    parser.add_argument("--out", help="输出目录；默认 docs/evidence/<run 名>")
    args = parser.parse_args()
    out = args.out or str(Path("docs/evidence") / Path(args.run).name)
    result = export_evidence(args.run, out)
    print(f"已导出 {result['steps']} 步到 {result['out']}")


if __name__ == "__main__":
    main()
