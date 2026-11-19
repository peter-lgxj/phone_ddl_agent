from memory import TaskMemory
from screen_agent import ScreenAgent
import config


def _memory(n_steps: int) -> TaskMemory:
    memory = TaskMemory("测试任务")
    for i in range(1, n_steps + 1):
        memory.add_step(
            thought=f"thought{i}",
            action={
                "action_type": "tap" if i % 2 else "type",
                "x": 100 + i,
                "y": 200 + i,
                "text": f"文本{i}" if i % 2 == 0 else "",
                "description": f"第{i}步动作",
            },
            observation=f"第{i}步观察结果：界面发生了变化",
        )
    return memory


def _agent(tmp_path):
    agent = object.__new__(ScreenAgent)
    agent.support_multi_image = True
    agent.run_dir = tmp_path
    agent._observe_valid = False
    return agent


def _write_shots(tmp_path, n):
    from PIL import Image

    for i in range(1, n + 1):
        # 真实 PNG：预处理会解码图片，假字节会被当作损坏文件跳过
        Image.new("RGB", (120, 200), (10 * i, 40, 80)).save(tmp_path / f"step_{i:03d}_observe.png")


def test_history_image_window_limits_images(tmp_path, monkeypatch):
    _write_shots(tmp_path, 5)
    memory = _memory(5)
    agent = _agent(tmp_path)

    monkeypatch.setattr(config, "HISTORY_IMAGE_WINDOW", 1)
    images = agent._collect_history_images(memory)
    assert [img["step"] for img in images] == [5]

    monkeypatch.setattr(config, "HISTORY_IMAGE_WINDOW", 3)
    images = agent._collect_history_images(memory)
    assert [img["step"] for img in images] == [3, 4, 5]

    monkeypatch.setattr(config, "HISTORY_IMAGE_WINDOW", 0)
    assert agent._collect_history_images(memory) == []

    # 窗口大于历史长度不报错，返回全部
    monkeypatch.setattr(config, "HISTORY_IMAGE_WINDOW", 99)
    assert len(agent._collect_history_images(memory)) == 5


def test_history_text_keeps_action_params_and_observation(tmp_path, monkeypatch):
    memory = _memory(6)
    agent = _agent(tmp_path)
    monkeypatch.setattr(config, "HISTORY_TEXT_STEPS", 8)

    text = agent._build_history_text(memory, {6})
    assert "第1步" in text
    assert "[tap]" in text
    assert "x=101" in text
    assert "第2步" in text and "text=文本2" in text
    assert "第2步观察结果" in text
    assert "第6步" in text  # 截图不包含已执行的动作，必须保留对应文字


def test_history_text_window_keeps_last_eight_steps_even_with_images(tmp_path, monkeypatch):
    memory = _memory(10)
    agent = _agent(tmp_path)
    monkeypatch.setattr(config, "HISTORY_TEXT_STEPS", 8)

    text = agent._build_history_text(memory, {9, 10})
    assert "第2步" not in text
    assert "第3步" in text
    assert "第10步" in text


def test_history_text_window_zero_no_error(tmp_path, monkeypatch):
    memory = _memory(3)
    agent = _agent(tmp_path)
    monkeypatch.setattr(config, "HISTORY_TEXT_STEPS", 0)
    assert "0" in agent._build_history_text(memory, set())

    empty = TaskMemory("空任务")
    assert "尚未执行" in agent._build_history_text(empty, set())


def test_prompt_always_includes_current_image(tmp_path, monkeypatch):
    memory = _memory(4)
    agent = _agent(tmp_path)
    monkeypatch.setattr(config, "HISTORY_IMAGE_WINDOW", 1)
    images = [{"step": 4, "label": "第4步执行后的屏幕", "b64": "x"}]

    prompt = agent._build_prompt(memory, images, "摘要", "OCR", "")
    assert "第1张：第4步执行后的屏幕" in prompt
    assert "第2张：当前屏幕（本次需要决策）" in prompt

    prompt_no_history = agent._build_prompt(memory, [], "摘要", "OCR", "")
    assert "第1张：当前屏幕（本次需要决策）" in prompt_no_history
    assert "第2张" not in prompt_no_history
