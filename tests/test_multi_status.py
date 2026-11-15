"""多应用失败/超时传播的回归测试（不需要设备与模型）。"""

import json

import pytest

import multi_agent
import task_planner


class FakeExec:
    def __init__(self, status):
        self.last_status = status
        self.last_memory = None

    def run(self, goal):
        return f"{goal} 的执行摘要"


class FakePlanner:
    """模拟 replan 在最后一个子任务失败时仍返回 task_done=True 的历史行为"""

    def plan(self, task):
        return task_planner.TaskPlan(task, [task_planner.SubTask(1, "打开设置", app="设置")])

    def replan(self, plan, current):
        return True, "所有子任务已完成"


def make_agent(tmp_path, status):
    agent = object.__new__(multi_agent.MultiAppAgent)
    agent.exec_agent = FakeExec(status)
    agent.planner = FakePlanner()
    agent.emit_fn = lambda event, data: None
    agent.log_path = str(tmp_path / "multi_log.json")
    agent.total_steps = 0
    agent.last_status = "idle"
    agent._go_home = lambda: None
    return agent


def test_timeout_subtask_is_not_reported_as_success(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, "timeout")

    with pytest.raises(RuntimeError) as exc:
        agent.run("打开设置")

    assert "未完成" in str(exc.value)
    assert agent.last_status == "timeout"


def test_failed_subtask_marks_overall_failed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, "failed")

    with pytest.raises(RuntimeError):
        agent.run("打开设置")

    assert agent.last_status == "failed"


def test_done_subtask_completes_normally(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, "done")

    agent.run("打开设置")

    assert agent.last_status == "done"
    log = json.loads((tmp_path / "multi_log.json").read_text(encoding="utf-8"))
    assert log["subtasks"][0]["status"] == "done"


class FakeExecWithDirs(FakeExec):
    """每次执行产出自己的证据目录，模拟 SubTaskScreenAgent"""

    def __init__(self, status, base):
        super().__init__(status)
        self.base = base
        self.run_dir = None
        self.calls = 0

    def run(self, goal):
        self.calls += 1
        self.run_dir = self.base / f"20260101-000000_sub{self.calls}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return super().run(goal)


class TwoTaskPlanner:
    def plan(self, task):
        return task_planner.TaskPlan(task, [
            task_planner.SubTask(1, "打开设置", app="设置"),
            task_planner.SubTask(2, "打开微信", app="微信"),
        ])

    def replan(self, plan, current):
        return all(t.status == "done" for t in plan.subtasks), "完成"


def test_every_subtask_has_traceable_run_dir(monkeypatch, tmp_path):
    """多应用模式下每个子任务都要留下可定位的证据目录，而不是只有最后一个"""
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, "done")
    agent.exec_agent = FakeExecWithDirs("done", tmp_path)
    agent.planner = TwoTaskPlanner()

    agent.run("打开设置再看微信")

    assert agent.last_status == "done"
    assert len(agent.sub_run_dirs) == 2
    assert agent.sub_run_dirs == [str(tmp_path / "20260101-000000_sub1"),
                                  str(tmp_path / "20260101-000000_sub2")]
    log = json.loads((tmp_path / "multi_log.json").read_text(encoding="utf-8"))
    assert [st["run_dir"] for st in log["subtasks"]] == agent.sub_run_dirs
