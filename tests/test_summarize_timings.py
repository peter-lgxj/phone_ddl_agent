"""summarize_timings：阶段耗时、P50/P90 与批量对比。"""

import json

from summarize_timings import summarize_run


def _write_step(path, step, **timings):
    path.mkdir(parents=True, exist_ok=True)
    (path / f"step_{step:03d}.json").write_text(
        json.dumps({"step": step, "timings": timings}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_summarize_run_stages_and_percentiles(tmp_path):
    run = tmp_path / "run_a"
    _write_step(run, 1, decision=2.0, observe=0.0, ocr=1.0,
                snap_yolo={"elapsed": 0.5}, execute=0.2)
    _write_step(run, 2, decision=4.0, observe=2.0, ocr=3.0,
                snap_yolo={"elapsed": 1.0}, execute=0.4)

    summary = summarize_run(run)

    assert summary["steps"] == 2
    assert summary["stages"]["decision"]["mean"] == 3.0
    assert summary["stages"]["decision"]["p50"] == 3.0
    assert summary["stages"]["decision"]["p90"] == 3.8
    assert summary["stages"]["yolo"]["total"] == 1.5
    assert summary["step_total"]["total"] == 12.6
    assert summary["step_total"]["mean"] == 6.3


def test_summarize_run_ignores_bad_json(tmp_path):
    run = tmp_path / "run_b"
    _write_step(run, 1, decision=1.0)
    (run / "step_002.json").write_text("{not json", encoding="utf-8")

    summary = summarize_run(run)

    assert summary["steps"] == 1
    assert summary["stages"]["decision"]["total"] == 1.0


def test_summarize_run_missing_stage_is_zero(tmp_path):
    run = tmp_path / "run_c"
    _write_step(run, 1, decision=1.0)

    summary = summarize_run(run)

    assert summary["stages"]["settle"]["samples"] == 0
    assert summary["stages"]["settle"]["mean"] == 0.0
