"""维护性能力测试：失败重放、AT 口径、多设备 serial、baseline 命令覆盖。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import metrics
import run_experiment as rwe

BASELINE_DIR = Path(__file__).resolve().parent.parent / "experiments" / "baselines"
sys.path.insert(0, str(BASELINE_DIR))

from _common import command_from_env  # noqa: E402


def records():
    return [
        {"task_id": "T1", "level": 1, "attempt": 1, "success": True, "steps": 5, "elapsed": 10.0},
        {"task_id": "T1", "level": 1, "attempt": 2, "success": False, "steps": 9, "elapsed": 20.0},
        {"task_id": "T2", "level": 1, "attempt": 1, "success": True, "steps": 6, "elapsed": 30.0},
        {"task_id": "T2", "level": 1, "attempt": 2, "success": True, "steps": 8, "elapsed": 100.0},
    ]


def test_at_mode_changes_denominator():
    grouped = metrics.group_by_task(records())

    at_all = metrics.summarize(grouped, at_mode="all")["AT"]
    at_success = metrics.summarize(grouped, at_mode="success")["AT"]
    at_best = metrics.summarize(grouped, at_mode="best")["AT"]

    assert at_all == 40.0                      # (15 + 65) / 2
    assert at_success < at_all                 # 排除失败尝试后 T1 变为 10s
    assert at_best == 20.0                     # (10 + 30) / 2
    assert at_all > at_success > at_best


def test_filter_failed_picks_only_failed_attempts(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"task_id": "T1", "success": False},
        {"task_id": "T2", "status": "error"},
        {"task_id": "T3", "agent_status": "timeout"},
        {"task_id": "T4", "success": True},
    ]), encoding="utf-8")
    tasks = [{"id": i, "task": "x"} for i in ("T1", "T2", "T3", "T4")]

    picked = rwe.filter_failed(tasks, str(path))

    assert [t["id"] for t in picked] == ["T1", "T2", "T3"]


def test_adb_uses_android_serial(monkeypatch):
    seen = {}

    class FakeProc:
        returncode = 0
        stdout = "List of devices attached"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setenv("ANDROID_SERIAL", "emulator-5554")
    monkeypatch.setattr(rwe.subprocess, "run", fake_run)

    rc, _ = rwe.adb(["devices"])

    assert rc == 0
    assert seen["cmd"][:3] == ["adb", "-s", "emulator-5554"]


def test_baseline_command_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OPEN_AUTOGLM_CMD", "python main.py --task {task} --serial emulator-1")

    cmd = command_from_env("OPEN_AUTOGLM_CMD", ["fallback"], "打开设置")

    assert cmd == ["python", "main.py", "--task", "打开设置", "--serial", "emulator-1"]


def test_baseline_command_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("OPEN_AUTOGLM_CMD", raising=False)

    assert command_from_env("OPEN_AUTOGLM_CMD", ["fallback"], "任务") == ["fallback"]


def test_collect_run_dirs_keeps_every_subtask():
    """多应用模式 results.jsonl 要能追到每个子任务的证据目录"""
    agent = SimpleNamespace(sub_run_dirs=["runs/1_sub1", "runs/1_sub2"])

    assert rwe.collect_run_dirs(agent) == ["runs/1_sub1", "runs/1_sub2"]


def test_collect_run_dirs_single_task_falls_back_to_run_dir():
    assert rwe.collect_run_dirs(SimpleNamespace(run_dir="runs/1")) == ["runs/1"]
    assert rwe.collect_run_dirs(SimpleNamespace(run_dir=None)) == []
