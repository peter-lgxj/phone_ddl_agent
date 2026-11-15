"""P1 回归用例：重试副作用、context 注入、CS 判定、消融 prompt。"""

import json
import pytest

import config
import metrics
import screen_agent
import task_planner
import ui_state
from memory import TaskMemory


class DummyPhone:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return recorder


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """重试测试不应真的等待退避时间"""
    monkeypatch.setattr(screen_agent.time, "sleep", lambda *_: None)


def make_agent(monkeypatch, tmp_path, decide, execute, observe):
    monkeypatch.chdir(tmp_path)
    ui_state.init_state("测试任务")   # 真实 run() 也先初始化状态文件
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.phone = DummyPhone()
    agent.run_dir = None
    monkeypatch.setattr(agent, "_think_and_act", decide)
    monkeypatch.setattr(agent, "_execute", execute)
    monkeypatch.setattr(agent, "_observe", observe)
    monkeypatch.setattr(agent, "_check_task_done", lambda memory: False)
    return agent


def test_observe_failure_does_not_replay_action(monkeypatch, tmp_path):
    """动作执行后观察失败，不能重新执行动作（否则微信会重复发消息）"""
    state = {"execute": 0, "observe": 0}

    def decide(memory):
        return {"action_type": "send", "description": "发送消息"}

    def execute(action):
        state["execute"] += 1
        return False

    def observe(action, memory):
        state["observe"] += 1
        raise RuntimeError("API 超时")

    agent = make_agent(monkeypatch, tmp_path, decide, execute, observe)
    memory = TaskMemory("发消息")
    finished, done, summary = agent._execute_step(1, memory)

    assert finished is False and done is False
    assert state["execute"] == 1, "动作被执行了多次"
    assert state["observe"] == 1, "观察不应重试"
    assert "观察失败" in memory.steps[0]["observation"]
    assert summary == ""


def test_decision_retry_still_happens_before_action(monkeypatch, tmp_path):
    """决策阶段（无副作用）仍应重试，重试成功后动作只执行一次"""
    state = {"decide": 0, "execute": 0}

    def decide(memory):
        state["decide"] += 1
        if state["decide"] == 1:
            raise RuntimeError("429")
        return {"action_type": "wait", "description": "等待"}

    def execute(action):
        state["execute"] += 1
        return False

    agent = make_agent(monkeypatch, tmp_path, decide, execute, lambda a, m: "加载中")
    monkeypatch.setattr(config, "STEP_RETRIES", 1)

    agent._execute_step(1, TaskMemory("等待页面"))

    assert state["decide"] == 2
    assert state["execute"] == 1


def test_done_summary_keeps_last_observation(monkeypatch, tmp_path):
    """完成检测命中时，摘要必须带上最后观察，供 context_needed 传递"""
    agent = make_agent(
        monkeypatch, tmp_path,
        lambda m: {"action_type": "wait", "description": "等待"},
        lambda a: False,
        lambda a, m: "消息中地点=天安门",
    )
    monkeypatch.setattr(agent, "_check_task_done", lambda memory: True)

    _, done, summary = agent._execute_step(3, TaskMemory("读消息"))

    assert done is True
    assert "天安门" in summary


def test_build_exec_goal_injects_context():
    st = task_planner.SubTask(2, "导航到指定地点", app="高德地图", context_needed="地点=天安门")
    goal = task_planner.build_exec_goal(st)
    assert goal.startswith("导航到指定地点")
    assert "地点=天安门" in goal

    plain = task_planner.SubTask(1, "打开微信")
    assert task_planner.build_exec_goal(plain) == "打开微信"


def test_prompt_has_no_dangling_semantic_reference():
    """关闭语义分析后，prompt 不能再要求以【语义分析】为准"""
    agent = object.__new__(screen_agent.ScreenAgent)
    memory = TaskMemory("打开设置看 WiFi")

    prompt = agent._build_prompt(memory, has_prev=False, ocr_result="", semantic_analysis="")
    assert "语义分析" not in prompt
    assert "根据历史观察判断任务是否完成" in prompt

    with_semantic = agent._build_prompt(memory, has_prev=False, ocr_result="", semantic_analysis="已完成 1/3")
    assert "语义分析" in with_semantic


def write_run(tmp_path, records, judgement):
    (tmp_path / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    (tmp_path / "judgement.json").write_text(
        json.dumps(judgement, ensure_ascii=False), encoding="utf-8")
    records, judgement = metrics.load_records(tmp_path)
    return metrics.group_by_task(metrics.resolve_success(records, judgement))


def task_attempts(task_id="T01"):
    return [
        {"task_id": task_id, "level": 1, "apps": ["微信"], "theory_steps": 5,
         "attempt": i, "success": None, "steps": 5, "elapsed": 10.0}
        for i in (1, 2, 3)
    ]


def test_task_level_judgement_does_not_fake_cs(tmp_path):
    """任务级布尔只能算 TSR；CS 必须为 N/A，而不是恒等于 TSR"""
    grouped = write_run(tmp_path, task_attempts(), {"T01": True})
    result = metrics.summarize(grouped)

    assert result["TSR"] == 100.0
    assert result["CS"] is None


def test_attempt_level_judgement_computes_cs(tmp_path):
    grouped = write_run(tmp_path, task_attempts(), {"T01": [True, False, True]})
    result = metrics.summarize(grouped)

    assert result["TSR"] == 100.0
    assert result["CS"] == 0.0
