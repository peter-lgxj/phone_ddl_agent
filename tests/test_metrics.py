"""任务集与指标聚合的纯本地校验，不需要手机和模型。"""

import json
from pathlib import Path

import pytest

import metrics

ROOT = Path(__file__).resolve().parent.parent


def write_run(tmp_path, records, judgement=None):
    (tmp_path / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")
    if judgement is not None:
        (tmp_path / "judgement.json").write_text(
            json.dumps(judgement, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_metrics_aggregation(tmp_path):
    records = [
        # T01：3 次全成功
        {"task_id": "T01", "level": 1, "apps": ["微信"], "theory_steps": 5, "attempt": 1,
         "success": True, "steps": 6, "elapsed": 30.0},
        {"task_id": "T01", "level": 1, "apps": ["微信"], "theory_steps": 5, "attempt": 2,
         "success": True, "steps": 7, "elapsed": 32.0},
        {"task_id": "T01", "level": 1, "apps": ["微信"], "theory_steps": 5, "attempt": 3,
         "success": True, "steps": 5, "elapsed": 31.0},
        # T09：多应用，2 成功 1 失败，走 judgement.json
        {"task_id": "T09", "level": 2, "apps": ["微信", "高德地图"], "theory_steps": 9, "attempt": 1,
         "success": True, "steps": 12, "elapsed": 60.0},
        {"task_id": "T09", "level": 2, "apps": ["微信", "高德地图"], "theory_steps": 9, "attempt": 2,
         "success": False, "steps": 14, "elapsed": 65.0},
        {"task_id": "T09", "level": 2, "apps": ["微信", "高德地图"], "theory_steps": 9, "attempt": 3,
         "success": None, "steps": 10, "elapsed": 58.0},
        # T17：Level 3，2 次中 1 次成功
        {"task_id": "T17", "level": 3, "apps": ["微信", "大众点评", "高德地图"], "theory_steps": 14,
         "attempt": 1, "success": False, "steps": 20, "elapsed": 90.0},
        {"task_id": "T17", "level": 3, "apps": ["微信", "大众点评", "高德地图"], "theory_steps": 14,
         "attempt": 2, "success": True, "steps": 18, "elapsed": 85.0},
    ]
    run_dir = write_run(tmp_path, records, judgement={"T09": True})

    got = metrics.load_records(run_dir)
    grouped = metrics.group_by_task(metrics.resolve_success(*got))
    overall = metrics.summarize(grouped)

    assert overall["tasks"] == 3
    assert overall["judged"] == 3
    assert overall["TSR"] == 100.0          # 每个任务至少成功一次
    assert overall["MultiAppTSR"] == 100.0
    assert overall["CS"] == pytest.approx(100 / 3)   # 只有 T01 全部成功
    # SE：T01=5/6，T09=9/11，T17=14/18 的均值
    expected_se = (5 / 6 + 9 / 11 + 14 / 18) / 3
    assert abs(overall["SE"] - expected_se) < 1e-9
    # AT：三个任务平均耗时再取均值
    assert abs(overall["AT"] - ((31 + 61 + 87.5) / 3)) < 1e-9


def test_unjudged_tasks_are_excluded(tmp_path):
    records = [
        {"task_id": "T01", "level": 1, "apps": ["微信"], "theory_steps": 5, "attempt": 1,
         "success": True, "steps": 5, "elapsed": 10.0},
        {"task_id": "T02", "level": 1, "apps": ["设置"], "theory_steps": 4, "attempt": 1,
         "success": None, "steps": 6, "elapsed": 12.0},
    ]
    run_dir = write_run(tmp_path, records)
    grouped = metrics.group_by_task(metrics.resolve_success(*metrics.load_records(run_dir)))
    result = metrics.summarize(grouped)

    assert result["tasks"] == 2
    assert result["judged"] == 1
    assert result["TSR"] == 100.0
    assert result["unjudged"] == ["T02"]
