"""CI 依赖单一真源：tests 的第三方 import 必须都在 requirements-lock.txt 里。"""

from __future__ import annotations

from pathlib import Path

from scripts.check_test_deps import check, main, normalize_distribution, parse_requirements


def _repo(tmp_path: Path, test_source: str, lock_source: str) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "_common.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_demo.py").write_text(test_source, encoding="utf-8")
    (tmp_path / "config.py").write_text("", encoding="utf-8")
    (tmp_path / "requirements-lock.txt").write_text(lock_source, encoding="utf-8")
    return tmp_path


def test_missing_third_party_import_is_reported(tmp_path):
    repo = _repo(
        tmp_path,
        "import json\nimport cv2\nimport _common\nfrom config import VALUE\n",
        "pytest==9.2.0\n",
    )

    missing, locked, _imports = check(repo)

    assert any("cv2" in item for item in missing)
    assert normalize_distribution("PyYAML") == "pyyaml"
    assert "pytest" in locked


def test_locked_third_party_import_passes(tmp_path):
    repo = _repo(
        tmp_path,
        "import cv2\nfrom PIL import Image\nimport pytest\n",
        "opencv-python-headless==5.0.0.93\npillow==13.0.0\npytest==9.2.0\n",
    )

    missing, _locked, _imports = check(repo)
    assert missing == []


def test_requirement_include_is_followed(tmp_path):
    repo = _repo(tmp_path, "import flask\n", "-r base.txt\n")
    (tmp_path / "base.txt").write_text("Flask==3.1.3\n", encoding="utf-8")

    locked = parse_requirements(tmp_path / "requirements-lock.txt")
    assert "flask" in locked
    assert check(repo)[0] == []


def test_cli_returns_nonzero_and_message_for_unlocked_import(tmp_path, capsys):
    repo = _repo(tmp_path, "import bs4\n", "pytest==9.2.0\n")

    exit_code = main(["--repo", str(repo)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "bs4" in output
    assert "requirements-lock.txt" in output


def test_repository_tests_are_in_sync_with_lock():
    repo = Path(__file__).resolve().parents[1]
    missing, _locked, _imports = check(repo)
    assert missing == [], f"tests 新依赖没进 lock：{missing}"


def test_lock_contains_test_tools():
    repo = Path(__file__).resolve().parents[1]
    locked = parse_requirements(repo / "requirements-lock.txt")
    assert {"pytest", "ruff", "openpyxl", "flask", "opencv-python-headless"} <= locked


def test_no_test_reads_gitignored_run_directories():
    """测试代码不得把 runs/output/state 当输入；有则必须改用 tests/fixtures。"""
    repo = Path(__file__).resolve().parents[1]
    offenders = []
    patterns = (
        'ROOT / "runs"', "ROOT / 'runs'",
        'ROOT / "output"', "ROOT / 'output'",
        'ROOT / "state"', "ROOT / 'state'",
        'Path("runs")', "Path('runs')",
        'Path("output")', "Path('output')",
        'Path("state")', "Path('state')",
        'open("runs/', "open('runs/",
        'open("output/', "open('output/",
        'open("state/', "open('state/",
    )
    for path in (repo / "tests").rglob("*.py"):
        if path.name == Path(__file__).name:
            continue  # 守卫脚本自身包含这些模式字符串
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern in text:
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders, f"测试引用了未入库目录：{offenders}"


def test_ci_workflow_uses_lock_and_single_trigger():
    workflow = (Path(__file__).resolve().parents[1]
                / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-lock.txt" in workflow
    assert "python-dotenv openpyxl" not in workflow
    assert "branches: [main]" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "scripts/check_test_deps.py" in workflow
