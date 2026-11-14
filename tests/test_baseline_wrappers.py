"""baseline wrapper 骨架的可执行性测试（用假的 baseline 仓库，不依赖真实系统）。"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "experiments" / "baselines" / "openautoglm_wrapper.py"


def run_wrapper(env_extra, task="打开设置"):
    return subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=json.dumps({"task": task}),
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **env_extra}, timeout=120,
    )


def test_missing_baseline_dir_fails_with_clear_message():
    proc = run_wrapper({"OPEN_AUTOGLM_DIR": ""})
    assert proc.returncode != 0
    assert "OPEN_AUTOGLM_DIR" in proc.stderr


def test_wrapper_reports_steps_and_summary(tmp_path):
    repo = tmp_path / "fake_autoglm"
    repo.mkdir()
    (repo / "main.py").write_text(
        "print('[step] tap')\nprint('[step] type')\nprint('final summary')\n",
        encoding="utf-8")

    proc = run_wrapper({"OPEN_AUTOGLM_DIR": str(repo)})
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["steps"] == 2
    assert "final summary" in payload["summary"]
    assert payload["returncode"] == 0
    assert payload["elapsed"] >= 0
