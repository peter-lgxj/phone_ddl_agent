import json
import os
import time

STATE_FILE = "agent_state.json"

def init_state(task: str):
    """初始化状态"""
    state = {
        "task": task,
        "status": "running",
        "current_step": 0,
        "total_steps": 0,
        "steps": [],
        "current_screenshot": "",
        "observe_screenshot": "",
        "last_action": {},
        "updated_at": time.time()
    }
    _save(state)
    return state

def update_step(step: int, thought: str, action: dict, observation: str = ""):
    """更新当前步骤"""
    state = load()
    state["current_step"] = step
    state["last_action"] = action
    state["steps"].append({
        "step": step,
        "thought": thought,
        "action_type": action.get("action_type", ""),
        "description": action.get("description", ""),
        "observation": observation,
        "x": action.get("x"),
        "y": action.get("y"),
    })
    state["current_screenshot"] = "screen_current.png"
    state["observe_screenshot"] = "screen_observe.png"
    state["updated_at"] = time.time()
    _save(state)

def update_observation(observation: str):
    """更新 observation"""
    state = load()
    if state["steps"]:
        state["steps"][-1]["observation"] = observation
        state["observe_screenshot"] = "screen_observe.png"
    state["updated_at"] = time.time()
    _save(state)

def set_status(status: str, summary: str = ""):
    """设置任务状态：running / finished / error"""
    state = load()
    state["status"] = status
    state["summary"] = summary
    state["updated_at"] = time.time()
    _save(state)

def load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)