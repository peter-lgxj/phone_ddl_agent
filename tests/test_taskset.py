"""任务集结构与实验口径校验。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "experiments" / "tasks.json"


def load():
    return json.loads(TASKS.read_text(encoding="utf-8"))


def test_task_set_shape():
    data = load()
    tasks = data["tasks"]
    assert len(tasks) == 20, "test.md 定义了 20 个任务"
    ids = [t["id"] for t in tasks]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)), "任务 id 不能重复"
    assert {t["level"] for t in tasks} == {1, 2, 3}


def test_each_task_is_self_consistent():
    for t in load()["tasks"]:
        assert t["task"].strip(), f"{t['id']} 缺少任务文本"
        assert "---" not in t["task"], f"{t['id']} 仍残留编辑标记"
        assert isinstance(t["theory_steps"], int) and t["theory_steps"] > 0
        assert t["apps"], f"{t['id']} 缺少 apps"
        if t["level"] >= 2:
            assert len(t["apps"]) >= 2, f"{t['id']} 为多应用任务"


def test_repeat_by_level_matches_protocol():
    data = load()
    assert data["repeat_by_level"] == {"1": 3, "2": 3, "3": 2}
    assert data["default_repeat"] == 3
