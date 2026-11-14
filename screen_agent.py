import json
import base64
import time
import subprocess
from phone_controller import PhoneController
from app_launcher import AppLauncher
from memory import TaskMemory
from ocr_locator import OCRLocator
import config
from openai import OpenAI
import ui_state


def _fix_json(raw: str) -> str:
    import re
    raw = re.sub(r'"x"\s*:\s*(\d+)\s*,\s*\d+',  r'"x": \1',  raw)
    raw = re.sub(r'"y"\s*:\s*(\d+)\s*,\s*\d+',  r'"y": \1',  raw)
    raw = re.sub(r'"x2"\s*:\s*(\d+)\s*,\s*\d+', r'"x2": \1', raw)
    raw = re.sub(r'"y2"\s*:\s*(\d+)\s*,\s*\d+', r'"y2": \1', raw)
    raw = re.sub(r',\s*}', '}', raw)
    raw = re.sub(r',\s*]', ']', raw)
    raw = raw.replace('\u201c', '"').replace('\u201d', '"')
    raw = raw.replace('\u2018', "'").replace('\u2019', "'")
    return raw


def _require_api_key(provider_name: str, provider: dict, role: str):
    """启动前校验密钥，避免 OpenAI 客户端抛出与根因无关的底层异常"""
    if provider.get("api_key"):
        return
    env_name = config.API_KEY_ENV.get(provider_name, "对应的 API Key 环境变量")
    raise RuntimeError(
        f"未配置{role}密钥：{role}当前为 {provider_name}（{provider.get('model')}），"
        f"需要环境变量 {env_name}。已在项目根目录查找 .env，"
        f"请复制 .env.example 为 .env 并填写后重试。"
    )


class ScreenAgent:

    ACTIONS = """
- open_app(app_name)：打开 App，如"微信"、"美团"、"设置"
- tap(x, y)：点击屏幕上的按钮、图标、列表项等非输入框元素，坐标来自 OCR
- tap_input_box()：专门用于点击输入框，内置 AI 视觉定位，比 tap 更准确
- swipe(x1, y1, x2, y2)：滑动屏幕
- type(text)：输入文字，必须在 tap_input_box 之后调用
- send()：发送消息或提交
- back()：返回键
- home()：回桌面
- wait()：等待加载
- finish(summary)：任务完成
"""

    def __init__(self, max_steps: int = None):
        provider_name = config.ACTIVE_PROVIDER
        provider      = config.PROVIDERS[provider_name]

        if provider_name in config.TEXT_ONLY_PROVIDERS:
            raise RuntimeError(
                f"provider「{provider_name}」不支持图片输入，不能作为执行模型；"
                f"请把 config.ACTIVE_PROVIDER 改为 qwen / zhipu / openai 之一。"
            )
        _require_api_key(provider_name, provider, role="执行模型")

        self.model               = provider["model"]
        self.support_multi_image = provider["support_multi_image"]

        # VL 模型：负责看截图、决定下一步操作
        self.client = OpenAI(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
        )

        # 纯文字大模型：负责语义分析、纠正 VL 的理解偏差
        planner_name = getattr(config, "PLANNER_PROVIDER", provider_name)
        planner_cfg  = config.PROVIDERS[planner_name]
        _require_api_key(planner_name, planner_cfg, role="规划模型")
        self.text_client = OpenAI(
            api_key=planner_cfg["api_key"],
            base_url=planner_cfg["base_url"],
        )
        self.text_model = planner_cfg["model"]

        self.ocr      = OCRLocator()
        self.phone    = PhoneController(self.ocr)
        self.launcher = AppLauncher(self.phone)
        self.max_steps = max_steps or config.MAX_STEPS

        print(f"视觉模型：{provider_name} / {self.model}")
        print(f"语义模型：{planner_name} / {self.text_model}")

    # ══════════════════════════════════════════════════════════
    #  语义分析：在决策前，用文字大模型理解历史 observation
    # ══════════════════════════════════════════════════════════

    def _analyze_history(self, task: str, history_steps: list) -> str:
        """
        在 _think_and_act 决策前调用。
        文字大模型读取原始 observation 历史，输出两件事：
          1. 当前任务进度的准确语义理解（纠正 VL 的实体识别错误）
          2. 下一步应该做什么的建议（辅助 VL 决策）

        VL 模型把这段分析作为额外上下文，专注于「看图确认坐标/状态 → 输出动作」。
        """
        if not history_steps:
            return ""

        # 格式化历史给文字模型看
        lines = []
        for s in history_steps[-6:]:   # 最近 6 步够用，避免 token 过多
            lines.append(
                f"第{s['step']}步 [{s['action_type']}] {s['action_desc']}\n"
                f"  观察结果：{s['observation']}"
            )
        history_text = "\n".join(lines)

        prompt = f"""你是一个手机任务执行的语义分析专家。
视觉模型(VL)已经完成了若干步操作，但它对截图文字的语义理解可能有误。
请你基于任务目标，对历史观察结果做准确的语义解读。

【总任务】
{task}

【历史执行步骤和 VL 模型的原始观察】
{history_text}

请完成以下分析（简洁，每条一两句）：

1. 【已完成的事项】从观察结果中，哪些任务要求已经有明确的完成证据？
2. 【关键信息提取】观察结果中包含哪些对后续步骤有用的具体信息？
   （注意区分实体类型：地名≠餐厅名，标题≠内容，例如"财富又一城的花胶鸡"中财富又一城是地点，花胶鸡是餐厅）
3. 【下一步建议】根据任务目标和当前进度，下一步应该做什么？
4. 【未完成的事项】任务中还有哪些要求尚未完成？

输出格式：
已完成：...
关键信息：...
下一步：...
未完成：..."""

        try:
            resp = self.text_client.chat.completions.create(
                model=self.text_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            analysis = resp.choices[0].message.content.strip()
            print(f"\n  🧠 [语义分析]\n{chr(10).join('     ' + l for l in analysis.splitlines())}")
            return analysis
        except Exception as e:
            print(f"  ⚠️  语义分析失败({e})")
            return ""

    # ══════════════════════════════════════════════════════════
    #  决策：文字模型先分析 → VL 模型看图行动
    # ══════════════════════════════════════════════════════════

    def _think_and_act(self, memory: TaskMemory) -> dict:
        self.phone.screenshot("screen_current.png")
        ocr_result = self.ocr.format_for_prompt("screen_current.png")

        with open("screen_current.png", "rb") as f:
            current_b64 = base64.b64encode(f.read()).decode("utf-8")

        # 上一轮 observe 截图
        content = []
        if len(memory.steps) > 0:
            try:
                with open("screen_observe.png", "rb") as f:
                    prev_b64 = base64.b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{prev_b64}"}
                })
            except FileNotFoundError:
                pass

        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{current_b64}"}
        })

        # ── 文字大模型先做语义分析（有历史才分析）──────────────
        semantic_analysis = ""
        if memory.steps:
            semantic_analysis = self._analyze_history(memory.task, memory.steps)

        known_apps = "、".join(AppLauncher.KNOWN_APPS.keys())

        # ── 把语义分析注入 prompt，VL 模型直接采用 ──────────────
        semantic_section = ""
        if semantic_analysis:
            semantic_section = f"""
    ## 语义分析（已由专业模型校正，请直接采用，不要自行重新解读）
    {semantic_analysis}
"""

        prompt = f"""你是一个手机操作智能体，通过观察手机截图决定下一步操作。

    ## 总体任务
    {memory.task}

    ## 已执行的历史步骤（VL 模型的原始观察，语义可能有偏差）
    {memory.format_for_prompt()}
{semantic_section}
    ## 当前屏幕 OCR 识别结果
    {ocr_result}

    ## 图片说明
    {'- 第一张：上一步执行后的屏幕' if len(memory.steps) > 0 else ''}
    - {'第二张' if len(memory.steps) > 0 else '第一张'}：当前屏幕（本次需要决策）

    ## 可用动作
    {self.ACTIONS}

    ## 操作规则
    1. tap操作必须使用 OCR 结果里的坐标
    2. 已打开的 App 不要重复 open_app
    3. 根据【语义分析】判断进度，不重复已完成的操作
    4. 每次只返回一个动作
    5. 可直接打开的 App：{known_apps}
    6. 需要输入文字固定两步：type → send
    7. 历史中验证结果包含"输入成功"时，下一步必须 send
    8. 你的职责是：看截图确认界面状态和坐标，结合语义分析决定动作

    ## 何时调用 finish
    以【语义分析】中的「未完成」为准：
    - 未完成为空 → action_type 填 finish，summary 填完成情况
    - 未完成不为空 → 继续执行，做语义分析中「下一步」建议的操作

    ## 只返回 JSON：
    {{
    "thought": "语义分析显示...，当前截图显示...，下一步应该...",
    "action_type": "open_app/tap/type/send/swipe/back/home/wait/finish",
    "app_name": "（open_app时填）",
    "x": 数字,
    "y": 数字,
    "x2": 数字,
    "y2": 数字,
    "text": "（type时填）",
    "summary": "（finish时填）",
    "description": "本步操作一句话描述"
    }}

    ## 只返回 JSON，注意：
    - x/y 坐标必须是单个整数
    - 所有标点必须是英文标点
    - 禁止在最后一个字段后加逗号
    """

        content.append({"type": "text", "text": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            fixed = _fix_json(raw)
            try:
                action = json.loads(fixed)
                print(f"  ⚠️  JSON 修复成功")
                return action
            except json.JSONDecodeError:
                print(f"  ⚠️  解析失败：{raw}")
                return {"action_type": "wait", "description": "解析异常，等待重试"}

    # ══════════════════════════════════════════════════════════
    #  观察（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def _observe(self, last_action: dict, memory: TaskMemory) -> str:
        """执行动作后截图，VL 模型描述结果（原始输出，不做额外处理）"""
        time.sleep(1)
        self.phone.screenshot("screen_observe.png")
        with open("screen_observe.png", "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        action_type = last_action.get("action_type", "")
        action_desc = last_action.get("description", "")

        if action_type == "type":
            text = last_action.get("text", "")
            verify_question = (
                f"输入框里是否有文字'{text}'？"
                f"有则说'输入成功，输入框显示:{text}'，没有则说'输入失败，输入框为空'"
            )
        elif action_type in ("send", "press_enter"):
            sent_text = ""
            for step in reversed(memory.steps):
                if step["action_type"] == "type":
                    sent_text = step.get("text", "")
                    break
            verify_question = (
                f"聊天记录最底部是否出现了内容为'{sent_text}'的消息？"
                f"出现了说'发送成功，消息内容:{sent_text}'，没有说'发送失败'"
            )
        elif action_type == "tap":
            verify_question = (
                f"点击'{action_desc}'后：\n"
                f"1. 当前在什么界面？\n"
                f"2. 屏幕上有哪些重要的文字内容？请完整列出原文\n"
                f"请用'当前界面:[界面名]，关键内容:[完整原文]'的格式回复"
            )
        elif action_type == "open_app":
            app = last_action.get("app_name", "")
            verify_question = f"{app}是否已打开？用一句话描述当前界面。"
        else:
            verify_question = "用一句话描述当前手机屏幕是什么界面。"

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                        },
                        {
                            "type": "text",
                            "text": f"请观察截图，{verify_question}只回复要求的内容，不要多余文字。"
                        }
                    ]
                }
            ]
        )
        return response.choices[0].message.content.strip()

    # ══════════════════════════════════════════════════════════
    #  执行（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def _execute(self, action: dict) -> bool:
        t = action.get("action_type", "wait")

        if t == "open_app":
            self.launcher.launch(action.get("app_name", ""))
        elif t == "tap_input_box":
            self.phone.tap_input_box()
        elif t == "type":
            self.phone.type_text(action["text"])
        elif t == "send":
            subprocess.run(["adb", "shell", "input", "keyevent", "66"])
            print("ADB 回车发送")
        elif t == "press_enter":
            subprocess.run(["adb", "shell", "input", "keyevent", "66"])
            print("ADB 回车发送")
        elif t == "back":
            self.phone.press_back()
        elif t == "home":
            self.phone.press_home()
        elif t == "wait":
            time.sleep(2)
        elif t == "finish":
            return True
        elif t == "tap":
            x = self._parse_coord(action["x"])
            y = self._parse_coord(action["y"])
            self.phone.tap(x, y)
        elif t == "swipe":
            x  = self._parse_coord(action["x"])
            y  = self._parse_coord(action["y"])
            x2 = self._parse_coord(action["x2"])
            y2 = self._parse_coord(action["y2"])
            self.phone.swipe(x, y, x2, y2)

        return False

    # ══════════════════════════════════════════════════════════
    #  任务完成检测（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def _check_task_done(self, memory: TaskMemory) -> bool:
        with open("screen_observe.png", "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        history_text = memory.format_for_prompt()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                        },
                        {
                            "type": "text",
                            "text": f"""用户的任务是：{memory.task}

    已执行的历史步骤和验证结果：
    {history_text}

    请根据历史步骤的验证结果和当前截图，判断用户任务是否已经全部完成。
    只回复 YES 或 NO，不要其他内容。"""
                        }
                    ]
                }
            ]
        )

        answer = response.choices[0].message.content.strip().upper()
        if "YES" in answer:
            print("│  🔍  任务完成检测：已完成")
            return True
        return False

    def _parse_coord(self, val) -> int:
        if isinstance(val, list):
            return int(val[0])
        if isinstance(val, str):
            val = val.strip().strip("[]").split(",")[0]
        return int(float(val))

    # ══════════════════════════════════════════════════════════
    #  主循环（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def run(self, task: str):
        memory = TaskMemory(task)
        ui_state.init_state(task)

        print(f"\n{'='*52}")
        print(f"  🎯  任务：{task}")
        print(f"{'='*52}")

        for step in range(1, self.max_steps + 1):
            print(f"\n┌─ 第 {step} 步 {'─'*38}")

            action = self._think_and_act(memory)
            print(f"│  💭  {action.get('thought', '')}")
            print(f"│  ▶   {action.get('description', '')}  [{action.get('action_type')}]")
            # Streamlit 控制台读的就是这个状态文件，之前只在 init_state 写过一次，
            # 导致页面永远停在「执行中」并无限自动刷新
            ui_state.update_step(step, action.get("thought", ""), action)

            finished = self._execute(action)
            observation = "任务完成" if finished else self._observe(action, memory)
            print(f"│  👁   {observation}")
            ui_state.update_observation(observation)

            memory.add_step(
                thought=action.get("thought", ""),
                action=action,
                observation=observation
            )

            if finished:
                print(f"\n{'='*52}")
                print(f"  ✅  {action.get('summary', '任务完成')}")
                print(f"{'='*52}")
                print(memory.summary())
                memory.save()
                ui_state.set_status("finished", action.get("summary", "任务完成"))
                return

            if step >= 3:
                if self._check_task_done(memory):
                    print(f"\n{'='*52}")
                    print(f"  ✅  任务已完成")
                    print(f"{'='*52}")
                    print(memory.summary())
                    memory.save()
                    ui_state.set_status("finished", "任务已完成")
                    return

            print(f"└{'─'*42}")
            time.sleep(0.5)

        print(f"\n⚠️  已达最大步数 {self.max_steps} 步")
        memory.save()
        ui_state.set_status("finished", f"达到最大步数 {self.max_steps}")