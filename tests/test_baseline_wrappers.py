"""baseline wrapper 的可执行性测试（用假的 baseline 仓库，不依赖真实系统）。"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER_DIR = ROOT / "experiments" / "baselines"


def run_wrapper(name, payload, env_extra):
    return subprocess.run(
        [sys.executable, str(WRAPPER_DIR / name)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **env_extra}, timeout=120,
    )


def run_openautoglm(env_extra, task="打开设置"):
    return run_wrapper("openautoglm_wrapper.py", {"task": task}, env_extra)


def test_missing_baseline_dir_fails_with_clear_message():
    proc = run_openautoglm({"OPEN_AUTOGLM_DIR": ""})
    assert proc.returncode != 0
    assert "OPEN_AUTOGLM_DIR" in proc.stderr


def test_wrapper_propagates_upstream_failure(tmp_path):
    """上游崩溃时 wrapper 必须返回非 0，不能被记成正常结束"""
    repo = tmp_path / "broken"
    repo.mkdir()
    (repo / "main.py").write_text(
        "import sys\nprint('partial output')\nsys.exit(3)\n", encoding="utf-8")

    proc = run_openautoglm({"OPEN_AUTOGLM_DIR": str(repo)})
    assert proc.returncode == 3


def test_wrapper_reports_steps_and_summary(tmp_path):
    repo = tmp_path / "fake_autoglm"
    repo.mkdir()
    (repo / "main.py").write_text(
        "print('[step] tap')\nprint('[step] type')\nprint('final summary')\n",
        encoding="utf-8")

    proc = run_openautoglm({"OPEN_AUTOGLM_DIR": str(repo)})
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["steps"] == 2
    assert "final summary" in payload["summary"]
    assert payload["returncode"] == 0
    assert payload["elapsed"] >= 0


def test_openautoglm_builds_positional_task_and_device(tmp_path):
    """上游入口是 `main.py <task>`，设备用 --device-id；不能拼成 --task"""
    repo = tmp_path / "fake_autoglm_argv"
    repo.mkdir()
    (repo / "main.py").write_text(
        "import json, sys\n"
        "json.dump(sys.argv[1:], open('argv.json', 'w', encoding='utf-8'), ensure_ascii=False)\n"
        "print('Result: done')\n",
        encoding="utf-8")

    proc = run_openautoglm({"OPEN_AUTOGLM_DIR": str(repo),
                            "ANDROID_SERIAL": "emulator-5554"}, task="打开设置")
    assert proc.returncode == 0, proc.stderr

    argv = json.loads((repo / "argv.json").read_text(encoding="utf-8"))
    assert argv == ["--device-id", "emulator-5554", "打开设置"]

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["upstream_status"] == "finished"
    assert payload["upstream_result"] == "done"


def test_appagent_wrapper_feeds_task_via_stdin(tmp_path):
    repo = tmp_path / "fake_appagent"
    repo.mkdir()
    (repo / "run.py").write_text(
        "import sys\n"
        "answers = sys.stdin.read().splitlines()\n"
        "print('Round 1')\n"
        "print('Task completed successfully')\n"
        "print('ANSWERS=' + '|'.join(answers))\n",
        encoding="utf-8")

    proc = run_wrapper("appagent_wrapper.py",
                       {"task": "打开设置", "apps": ["设置"]},
                       {"APPAGENT_DIR": str(repo)})
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["steps"] == 1
    assert payload["upstream_status"] == "completed"
    assert "ANSWERS=y|打开设置" in payload["summary"]


def test_mobile_agent_v2_refuses_empty_instruction(tmp_path):
    repo = tmp_path / "fake_mobile_agent"
    repo.mkdir()
    (repo / "run.py").write_text('instruction = ""\nprint("should not run")\n', encoding="utf-8")

    proc = run_wrapper("mobile_agent_v2_wrapper.py", {"task": "打开设置"},
                       {"MOBILE_AGENT_V2_DIR": str(repo)})
    assert proc.returncode != 0
    assert "instruction" in proc.stderr


def test_mobile_agent_v2_counts_decisions_and_detects_stop(tmp_path):
    repo = tmp_path / "fake_mobile_agent_ok"
    repo.mkdir()
    (repo / "run.py").write_text(
        "instruction = '打开设置'\n"
        "print('#' * 50 + ' Decision ' + '#' * 50)\n"
        "print('### Action ###')\n"
        "print('Stop')\n"
        "print('### Operation ###')\n",
        encoding="utf-8")

    proc = run_wrapper("mobile_agent_v2_wrapper.py", {"task": "打开设置"},
                       {"MOBILE_AGENT_V2_DIR": str(repo)})
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["steps"] == 1
    assert payload["upstream_status"] == "stopped"
