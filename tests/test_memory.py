"""任务记忆与 SubTask 数据结构的单元测试。"""

import json

from memory import TaskMemory
from task_planner import SubTask, build_exec_goal


def test_add_step_keeps_full_action():
    m = TaskMemory("打开微信发消息")
    m.add_step(thought="先打开微信", action={"action_type": "open_app", "app_name": "微信", "description": "打开微信"},
               observation="微信已打开")
    m.add_step(thought="点输入框", action={"action_type": "tap", "x": 100, "y": 200, "description": "点输入框"},
               observation="键盘弹出")

    assert len(m.steps) == 2
    assert m.steps[1]["action"]["x"] == 100
    assert m.steps[0]["step"] == 1 and m.steps[1]["step"] == 2


def test_save_writes_json(tmp_path):
    m = TaskMemory("测试任务")
    m.add_step(thought="思考", action={"action_type": "wait", "description": "等待"}, observation="加载完成")
    path = tmp_path / "task_log.json"
    m.save(path=str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task"] == "测试任务"
    assert data["total_steps"] == 1
    assert data["steps"][0]["action_type"] == "wait"


def test_subtask_carries_context_needed():
    st = SubTask(index=2, goal="在高德导航", app="高德地图",
                 depends_on=[1], context_needed="地点=天安门")
    d = st.to_dict()
    assert d["context_needed"] == "地点=天安门"
    assert d["status"] == "pending"
    assert d["index"] == 2


def test_build_exec_goal_injects_context():
    st = SubTask(index=2, goal="导航到指定地点", app="高德地图", context_needed="地点=天安门")
    goal = build_exec_goal(st)
    assert goal.startswith("导航到指定地点")
    assert "地点=天安门" in goal

    plain = SubTask(index=1, goal="打开微信")
    assert build_exec_goal(plain) == "打开微信"
