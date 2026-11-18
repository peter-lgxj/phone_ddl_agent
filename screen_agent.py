import json
import base64
import os
import shutil
import time
import subprocess
from pathlib import Path
from phone_controller import PhoneController
from app_launcher import AppLauncher
from memory import TaskMemory, format_steps_compact
from ocr_locator import OCRLocator, should_trigger_ocr, snap_action_coords
import config
import image_preprocess
from adb_utils import ADB
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


def make_run_dir(name: str) -> Path:
    """为一次执行创建唯一证据目录。

    同一秒内启动多次（重跑 / 多子任务）时追加序号，避免已有证据被覆盖。
    """
    base = Path(config.ARTIFACT_DIR)
    path = base / name
    suffix = 1
    while path.exists():
        path = base / f"{name}_{suffix}"
        suffix += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_observe_question(last_action: dict, memory) -> str:
    """按动作类型构造观察问题。

    滚动类动作不能只让模型说一句“这是某界面”，否则聊天正文永远进不了
    observation；这里要求逐条列出可见消息，作为 OCR 之外的第一层文本来源。
    """
    action_type = str(last_action.get("action_type") or "").lower()
    action_desc = last_action.get("description", "")

    if action_type == "type":
        text = last_action.get("text", "")
        return (
            f"输入框里是否有文字'{text}'？"
            f"有则说'输入成功，输入框显示:{text}'，没有则说'输入失败，输入框为空'"
        )
    if action_type in ("send", "press_enter"):
        sent_text = ""
        for step in reversed(getattr(memory, "steps", [])):
            if step.get("action_type") == "type":
                sent_text = step.get("text", "")
                break
        return (
            f"聊天记录最底部是否出现了内容为'{sent_text}'的消息？"
            f"出现了说'发送成功，消息内容:{sent_text}'，没有说'发送失败'"
        )
    if action_type == "tap":
        return (
            f"点击'{action_desc}'后：\n"
            f"1. 当前在什么界面？\n"
            f"2. 屏幕上有哪些重要的文字内容？请完整列出原文\n"
            f"请用'当前界面:[界面名]，关键内容:[完整原文]'的格式回复"
        )
    if action_type == "open_app":
        app = last_action.get("app_name", "")
        return f"{app}是否已打开？用一句话描述当前界面。"
    if action_type in ("swipe", "scroll"):
        return (
            f"执行“{action_desc or action_type}”后，请逐条列出当前屏幕上可见的消息，"
            "每条尽量包含发送者、时间和消息原文；按屏幕从上到下的顺序输出，"
            "不要概括、不要合并多条。忽略状态栏、输入框、底部导航和按钮等系统 UI 文字。"
            "如果没有任何消息文本，只回复“未看到消息文本”。"
        )
    return "用一句话描述当前手机屏幕是什么界面。"


def format_ocr_observation(elements: list[dict]) -> str:
    """把 OCR 元素按屏幕阅读顺序拼成文本，供 observation 使用。"""
    def _key(item: dict) -> tuple[int, int]:
        bbox = item.get("bbox") or (0, 0, 0, 0)
        return (int(bbox[1]), int(bbox[0]))

    lines: list[str] = []
    seen: set[str] = set()
    for item in sorted(elements, key=_key):
        text = str(item.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return "\n".join(lines)


class ScreenAgent:

    ACTIONS = """
- open_app(app_name)：打开 App，如"微信"、"美团"、"设置"
- tap(x, y)：点击屏幕上的按钮、图标、列表项等非输入框元素，坐标来自 OCR
- tap_input_box()：专门用于点击输入框，内置 AI 视觉定位，比 tap 更准确
- swipe(x1, y1, x2, y2)：滑动屏幕
- type(text)：输入文字，必须在 tap_input_box 之后调用
- send()：发送消息或提交
- press_enter()：按回车（等同 send）
- back()：返回键
- home()：回桌面
- wait()：等待加载
- finish(summary)：任务完成
"""

    def __init__(self, max_steps: int = None):
        provider_name = config.ACTIVE_PROVIDER
        provider      = config.PROVIDERS[provider_name]

        if provider_name in config.TEXT_ONLY_PROVIDERS:
            vision_providers = sorted(set(config.PROVIDERS) - config.TEXT_ONLY_PROVIDERS)
            raise RuntimeError(
                f"provider「{provider_name}」不支持图片输入，不能作为执行模型；"
                f"请把 config.ACTIVE_PROVIDER 改为支持视觉的 provider 之一："
                f"{' / '.join(vision_providers)}。"
            )
        _require_api_key(provider_name, provider, role="执行模型")

        self.model               = provider["model"]
        self.support_multi_image = provider.get("support_multi_image", False)
        # provider 级别的额外请求参数（如 deepseek 的 thinking 开关），None 时 SDK 不发送
        self.extra_body          = provider.get("extra_body")

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
        self.text_extra_body = planner_cfg.get("extra_body")

        self.ocr      = OCRLocator()
        self.phone    = PhoneController(self.ocr)
        self.launcher = AppLauncher(self.phone)
        self.max_steps = max_steps or config.MAX_STEPS
        self.run_dir   = None
        self.last_status = "idle"   # done / timeout / failed / idle
        self._observe_valid = False  # 当前 screen_observe.png 是否是本步真实截图
        self._last_image_meta = None     # 本步发送图的规格与缩放比例
        self._last_model_action = None   # 缩放时模型返回的原始坐标（未反算）
        self._last_decision_timings = {}  # 决策各阶段耗时
        self._last_step_timings = {}      # 整步各阶段耗时（写进 step json）
        self._last_observe_ocr_seconds = 0.0  # 本步滚动 OCR 耗时（未触发为 0）

        print(f"视觉模型：{provider_name} / {self.model}")
        print(f"语义模型：{planner_name} / {self.text_model}")

    # ══════════════════════════════════════════════════════════
    #  语义分析：在决策前，用文字大模型理解历史 observation
    # ══════════════════════════════════════════════════════════

    def _collect_history_images(self, memory: TaskMemory) -> list[dict]:
        """按 HISTORY_IMAGE_WINDOW 收集最近的历史原图（当前屏幕另行加入）。

        返回 [{"step": n, "label": "...", "b64": "...", "mime": "..."}]，数量不超过窗口值。
        优先用证据目录里归档的 step_NNN_observe.png；没有归档时退回 screen_observe.png。
        """
        window = max(0, int(getattr(config, "HISTORY_IMAGE_WINDOW", 1)))
        if not self.support_multi_image or window == 0 or not memory.steps:
            return []

        candidates = []
        if self.run_dir is not None:
            for path in sorted(Path(self.run_dir).glob("step_*_observe.png")):
                try:
                    step_no = int(path.name.split("_")[1])
                except (IndexError, ValueError):
                    continue
                candidates.append((step_no, path))
        if not candidates and self._observe_valid and os.path.exists("screen_observe.png"):
            candidates = [(len(memory.steps), Path("screen_observe.png"))]

        images = []
        for step_no, path in candidates[-window:]:
            try:
                data, meta = image_preprocess.prepare_image_bytes(str(path))
                images.append({
                    "step": step_no,
                    "label": f"第{step_no}步执行后的屏幕",
                    "b64": base64.b64encode(data).decode("utf-8"),
                    "mime": meta["mime"],
                })
            except OSError as e:
                print(f"  ⚠️  历史截图读取失败（{path}）：{e}")
        return images

    def _build_history_text(self, memory: TaskMemory, image_steps: set[int]) -> str:
        """没有原图可带的历史步骤压成文字摘要，保留动作类型、关键参数与观察结论"""
        if not memory.steps:
            return "尚未执行任何操作，刚刚开始。"

        limit = max(0, int(getattr(config, "HISTORY_TEXT_STEPS", 8)))
        older = [s for s in memory.steps if s.get("step") not in image_steps]
        if limit == 0:
            return "（历史原图已省略；文字摘要窗口为 0）"
        if not older:
            return "（更早步骤已省略，仅保留最近原图）"
        return format_steps_compact(older[-limit:])

    def _build_prompt(self, memory, history_images, history_text, ocr_result, semantic_analysis) -> str:
        """构建单步决策 prompt；关掉语义分析时不再出现任何【语义分析】引用"""
        known_apps = "、".join(AppLauncher.KNOWN_APPS.keys())

        semantic_section = ""
        if semantic_analysis:
            semantic_section = f"""
    ## 语义分析（已由专业模型校正，请直接采用，不要自行重新解读）
    {semantic_analysis}
"""

        if semantic_analysis:
            progress_rule = "3. 根据【语义分析】判断进度，不重复已完成的操作"
            duty_rule     = "9. 你的职责是：看截图确认界面状态和坐标，结合语义分析决定动作"
            finish_rules  = """以【语义分析】中的「未完成」为准：
    - 未完成为空 → action_type 填 finish，summary 填完成情况
    - 未完成不为空 → 继续执行，做语义分析中「下一步」建议的操作"""
        else:
            progress_rule = "3. 根据历史观察判断进度，不重复已完成的操作"
            duty_rule     = "9. 你的职责是：看截图确认界面状态和坐标，结合历史观察决定动作"
            finish_rules  = """根据历史观察判断任务是否完成：
    - 能确认目标已达成 → action_type 填 finish，summary 写出关键结果（名称/价格/地点等）
    - 仍缺步骤 → 继续执行下一步操作"""

        image_lines = [
            f"    - 第{i + 1}张：{img['label']}"
            for i, img in enumerate(history_images)
        ]
        image_lines.append(
            f"    - 第{len(history_images) + 1}张：当前屏幕（本次需要决策）"
        )
        image_section = "\n".join(image_lines)

        if ocr_result:
            ocr_section = ocr_result
            coord_rule = "1. tap操作必须使用 OCR 结果里的坐标"
        else:
            ocr_section = "（本步未预跑 OCR；请依据截图估计元素位置，tap 落点会再做校正）"
            coord_rule = "1. tap 坐标请根据截图中元素的像素位置估计，尽量落在元素中心"

        return f"""你是一个手机操作智能体，通过观察手机截图决定下一步操作。

    ## 总体任务
    {memory.task}

    ## 已执行的历史步骤（原图只保留最近若干张，更早步骤为文字摘要）
    {history_text}
{semantic_section}
    ## 当前屏幕 OCR 识别结果
    {ocr_section}

    ## 图片说明
{image_section}

    ## 可用动作
    {self.ACTIONS}

    ## 操作规则
    {coord_rule}
    2. 已打开的 App 不要重复 open_app
    {progress_rule}
    4. 每次只返回一个动作
    5. 可直接打开的 App：{known_apps}
    6. 需要输入文字固定两步：type → send
    7. 历史中验证结果包含"输入成功"时，下一步必须 send
    8. tap 时在 target 填上目标元素的文字（或 resource-id），便于精确落点
    {duty_rule}

    ## 何时调用 finish
    {finish_rules}

    ## 只返回 JSON：
    {{
    "thought": "当前截图显示...，下一步应该...",
    "action_type": "open_app/tap/tap_input_box/type/send/press_enter/swipe/back/home/wait/finish",
    "app_name": "（open_app时填）",
    "x": 数字,
    "y": 数字,
    "x2": 数字,
    "y2": 数字,
    "text": "（type时填）",
    "summary": "（finish时填）",
    "description": "本步操作一句话描述",
    "target": "（tap时填）要点击元素的文字或描述，用于精确定位",
    "task_done": 布尔值（整项任务是否已全部完成，未完成填 false）
    }}

    ## 只返回 JSON，注意：
    - x/y 坐标必须是单个整数
    - 所有标点必须是英文标点
    - 禁止在最后一个字段后加逗号
    """

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
                extra_body=getattr(self, "text_extra_body", None),
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
        timings = {}
        started = time.time()
        self.phone.screenshot("screen_current.png")
        timings["screenshot"] = round(time.time() - started, 2)

        started = time.time()
        ocr_mode = getattr(config, "OCR_MODE", "on_demand")
        ocr_result = ""
        if ocr_mode == "always":
            ocr_result = self.ocr.format_for_prompt("screen_current.png")
        timings["ocr_pre"] = round(time.time() - started, 2)
        timings["ocr"] = timings["ocr_pre"]
        timings["ocr_triggered"] = ocr_mode == "always"
        timings["ocr_reason"] = "always" if ocr_mode == "always" else ""

        started = time.time()
        # 截图后先预处理：JPEG 编码 / 可选等比缩放；缩放比例用于把模型坐标反算回设备坐标
        current_bytes, image_meta = image_preprocess.prepare_image_bytes("screen_current.png")
        self._last_image_meta = image_meta
        self._last_model_action = None
        print(f"  🖼  发送图：{image_preprocess.describe(image_meta)}")
        with open(image_preprocess.sent_filename("screen_current.png", image_meta), "wb") as f:
            f.write(current_bytes)
        current_b64 = base64.b64encode(current_bytes).decode("utf-8")

        # 历史原图按窗口裁剪；当前屏幕无论如何都要带图
        history_images = self._collect_history_images(memory)
        content = [
            {"type": "image_url",
             "image_url": {"url": f"data:{img['mime']};base64,{img['b64']}"}}
            for img in history_images
        ]
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_meta['mime']};base64,{current_b64}"}
        })

        # 没带原图的早期步骤压成文字摘要
        image_steps = {img["step"] for img in history_images}
        history_text = self._build_history_text(memory, image_steps)
        timings["preprocess"] = round(time.time() - started, 2)

        # ── 文字大模型先做语义分析（有历史才分析，默认关闭）──
        started = time.time()
        semantic_analysis = ""
        if memory.steps and config.ENABLE_SEMANTIC_ANALYSIS:
            semantic_analysis = self._analyze_history(memory.task, memory.steps)
        timings["semantic"] = round(time.time() - started, 2)

        prompt = self._build_prompt(memory, history_images, history_text,
                                    ocr_result, semantic_analysis)

        content.append({"type": "text", "text": prompt})

        started = time.time()
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": content}],
            extra_body=getattr(self, "extra_body", None),
        )
        timings["decision"] = round(time.time() - started, 2)
        self._last_decision_timings = timings

        raw = response.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            action = json.loads(raw)
        except json.JSONDecodeError:
            fixed = _fix_json(raw)
            try:
                action = json.loads(fixed)
                print(f"  ⚠️  JSON 修复成功")
            except json.JSONDecodeError:
                print(f"  ⚠️  解析失败：{raw}")
                action = {"action_type": "wait", "description": "解析异常，等待重试"}

        # 缩放过才反算；未缩放时 map_action_coords 原样返回，不引入偏移
        mapped = image_preprocess.map_action_coords(action, image_meta)
        keys = ("x", "y", "x2", "y2")
        if image_meta.get("resized") and any(k in action for k in keys):
            self._last_model_action = action
            print(f"  🧭 坐标反算：{ {k: action[k] for k in keys if k in action} } -> "
                  f"{ {k: mapped[k] for k in keys if k in mapped} }")

        # 按需定位：控件树优先、OCR 兜底；只在需要精确落点（tap）时触发
        if should_trigger_ocr(ocr_mode, mapped.get("action_type", "")):
            snap_mode = getattr(config, "OCR_SNAP_MODE", "tree_first")
            max_distance = int(getattr(config, "OCR_SNAP_MAX_DISTANCE", 80))
            snap_source = "none"
            moves = []
            tree_elapsed = ocr_elapsed = 0.0
            tree_count = ocr_count = 0
            ocr_meta = {}

            if snap_mode in ("tree_first", "tree_only"):
                started = time.time()
                try:
                    tree_elements = self.ocr.detect_tree()
                except Exception as e:
                    print(f"  ⚠️  控件树读取失败，继续走 OCR：{e}")
                    tree_elements = []
                tree_elapsed = round(time.time() - started, 2)
                tree_count = len(tree_elements)
                if tree_elements:
                    snapped, moves = snap_action_coords(mapped, tree_elements, max_distance)
                    if moves:
                        snap_source = "tree"
                        mapped = snapped

            if snap_source == "none" and snap_mode in ("tree_first", "ocr_only"):
                started = time.time()
                try:
                    ocr_elements = self.ocr.detect_ocr("screen_current.png")
                except Exception as e:
                    print(f"  ⚠️  OCR 失败，保留模型坐标：{e}")
                    ocr_elements = []
                ocr_elapsed = round(time.time() - started, 2)
                ocr_count = len(ocr_elements)
                ocr_meta = dict(getattr(self.ocr, "last_meta", {}) or {})
                if ocr_elements:
                    snapped, moves = snap_action_coords(mapped, ocr_elements, max_distance)
                    if moves:
                        snap_source = "ocr"
                        mapped = snapped

            timings["ocr"] = round(timings.get("ocr", 0.0) + tree_elapsed + ocr_elapsed, 2)
            timings["ocr_triggered"] = True
            timings["ocr_reason"] = "tap"
            timings["snap_source"] = snap_source
            timings["snap_tree"] = {"elapsed": tree_elapsed, "elements": tree_count}
            timings["snap_ocr"] = {"elapsed": ocr_elapsed, "elements": ocr_count,
                                   "scale": ocr_meta.get("scale"),
                                   "scaled_size": ocr_meta.get("scaled_size"),
                                   "engine": ocr_meta.get("engine")}
            timings["ocr_snap"] = {"moves": moves}
            if moves:
                print(f"  🎯 {snap_source} 吸附：{moves}")

        self._last_decision_timings = timings
        return mapped

    # ══════════════════════════════════════════════════════════
    #  观察（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def _clear_observe_shot(self):
        """删除上一轮的观察截图，避免截图/模型失败后旧图被当作当前屏幕"""
        try:
            os.remove("screen_observe.png")
        except FileNotFoundError:
            pass
        except OSError as e:
            # Windows 上被前端读取时会 PermissionError；清理失败不应终止任务
            print(f"│  ⚠️  旧观察截图无法删除（{e}），完成检测前会再次尝试")

    def _observe(self, last_action: dict, memory: TaskMemory) -> str:
        """执行动作后截图，VL 模型描述结果；滚动类动作再叠加 OCR 文本。"""
        time.sleep(float(getattr(config, "OBSERVE_SETTLE_SECONDS", 1.0)))
        self._clear_observe_shot()   # 先删旧图：截图失败时不会留下过期画面
        self._last_observe_ocr_seconds = 0.0
        self.phone.screenshot("screen_observe.png")
        observe_bytes, observe_meta = image_preprocess.prepare_image_bytes("screen_observe.png")
        image_b64 = base64.b64encode(observe_bytes).decode("utf-8")

        action_type = str(last_action.get("action_type") or "").lower()
        verify_question = build_observe_question(last_action, memory)

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=config.LLM_TEMPERATURE,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{observe_meta['mime']};base64,{image_b64}"}
                        },
                        {
                            "type": "text",
                            "text": f"请观察截图，{verify_question}只回复要求的内容，不要多余文字。"
                        }
                    ]
                }
            ],
            extra_body=getattr(self, "extra_body", None),
        )
        observation = response.choices[0].message.content.strip()

        ocr_actions = getattr(config, "OBSERVE_OCR_ACTIONS", {"swipe", "scroll"})
        if (
            getattr(config, "OBSERVE_OCR_ENABLED", False)
            and action_type in ocr_actions
        ):
            try:
                started = time.perf_counter()
                elements = self.ocr.detect_ocr("screen_observe.png")
                ocr_text = format_ocr_observation(elements)
                elapsed = round(time.perf_counter() - started, 2)
                self._last_observe_ocr_seconds = elapsed
                if ocr_text and ocr_text not in observation:
                    observation = f"{observation}\n[OCR]\n{ocr_text}"
                print(f"│  🔎  滚动 OCR：{len(elements)} 个文本框，{elapsed}s")
            except Exception as e:
                # OCR 只是补充通道，失败不能中断滚动或让整步失败
                print(f"│  ⚠️  滚动 OCR 失败，保留模型描述：{e}")
        return observation

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
            text = action.get("text")
            if not text:
                print("  ⚠️  type 动作缺少 text 字段，跳过本步")
                return False
            self.phone.type_text(text)
        elif t == "send":
            subprocess.run([ADB, "shell", "input", "keyevent", "66"])
            print("ADB 回车发送")
        elif t == "press_enter":
            subprocess.run([ADB, "shell", "input", "keyevent", "66"])
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
            if action.get("x") is None or action.get("y") is None:
                print("  ⚠️  tap 动作缺少坐标，跳过本步")
                return False
            x = self._parse_coord(action["x"])
            y = self._parse_coord(action["y"])
            self.phone.tap(x, y)
        elif t == "swipe":
            if any(action.get(k) is None for k in ("x", "y", "x2", "y2")):
                print("  ⚠️  swipe 动作缺少坐标，跳过本步")
                return False
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
        # 没有本步有效截图时不判定完成（旧图/删图失败都会导致误判）
        if not self._observe_valid:
            return False
        done_bytes, done_meta = image_preprocess.prepare_image_bytes("screen_observe.png")
        image_b64 = base64.b64encode(done_bytes).decode("utf-8")

        history_text = memory.format_for_prompt()

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=config.LLM_TEMPERATURE,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{done_meta['mime']};base64,{image_b64}"}
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
            ],
            extra_body=getattr(self, "extra_body", None),
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
        self.last_memory = memory   # 供实验脚本统计实际步数
        ui_state.init_state(task)

        # 每次运行一个独立目录，逐步截图 + 动作/观察 JSON 全量留证，
        # 便于事后定位失败步骤和论文里的典型案例分析
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = make_run_dir(stamp)

        print(f"\n{'='*52}")
        print(f"  🎯  任务：{task}")
        print(f"  证据目录：{self.run_dir}")
        print(f"{'='*52}")

        for step in range(1, self.max_steps + 1):
            print(f"\n┌─ 第 {step} 步 {'─'*38}")

            try:
                finished, done, summary = self._execute_step(step, memory)
            except Exception as e:
                # 单步重试次数用尽：落状态后交给上层（Web 端会推 error 事件）
                self.last_status = "failed"
                ui_state.set_status("error", f"第 {step} 步失败：{e}")
                raise

            if finished or done:
                print(f"\n{'='*52}")
                print(f"  ✅  {summary}")
                print(f"{'='*52}")
                print(memory.summary())
                self._save_log(memory)
                ui_state.set_status("finished", summary)
                self.last_status = "done"
                return

            print(f"└{'─'*42}")
            time.sleep(0.5)

        print(f"\n⚠️  已达最大步数 {self.max_steps} 步")
        self._save_log(memory)
        # 超步数不等于完成：实验统计必须能区分
        self.last_status = "timeout"
        ui_state.set_status("finished", f"达到最大步数 {self.max_steps}")

    def _execute_step(self, step: int, memory: TaskMemory):
        """
        执行一步：决策（可重试）→ 动作（只执行一次）→ 观察 → 记录。重试只覆盖无副作用的决策，动作执行后失败不再重放。
        返回 (finished, done, summary)：finished 表示模型主动 finish，
        done 表示完成检测判定已完成，summary 为结束说明。
        """
        timings = dict(getattr(self, "_last_decision_timings", {}) or {})
        started = time.time()
        action = self._decide(step, memory)
        timings["decide_total"] = round(time.time() - started, 2)

        # 动作只执行一次，不随观察/完成检测失败而重放
        # 动作执行后旧截图作废，观察成功才重新有效
        self._observe_valid = False
        started = time.time()
        finished = self._execute(action)
        timings["execute"] = round(time.time() - started, 2)

        if finished:
            observation = "任务完成"
        else:
            started = time.time()
            try:
                observation = self._observe(action, memory)
                self._observe_valid = True
            except Exception as e:
                observation = f"观察失败（动作已执行，未重试）：{e}"
                # 旧截图必须作废，否则完成检测/下一步会把上一步画面当成当前屏幕
                self._clear_observe_shot()
                print(f"│  ⚠️  {observation}")
            timings["observe"] = round(time.time() - started, 2)
            timings["observe_ocr"] = round(
                getattr(self, "_last_observe_ocr_seconds", 0.0), 2
            )
        print(f"│  👁   {observation}")
        ui_state.update_observation(observation)

        memory.add_step(
            thought=action.get("thought", ""),
            action=action,
            observation=observation
        )

        # 完成判断：默认直接用决策响应里的 task_done，省掉每步一次带图请求
        done = False
        started = time.time()
        if not finished and step >= 3:
            mode = getattr(config, "COMPLETION_CHECK", "decision")
            if mode == "every_step":
                try:
                    done = self._check_task_done(memory)
                except Exception as e:
                    # 完成检测失败只是无法确认完成，不能因此中断或重放动作
                    print(f"│  ⚠️  完成检测失败，按未完成处理：{e}")
            elif mode == "decision" and action.get("task_done"):
                done = True
                print("│  🔍  决策模型标记任务完成")
        timings["completion"] = round(time.time() - started, 2)

        self._last_step_timings = timings
        self._archive_step(step, action, observation)

        if action.get("summary"):
            summary = action["summary"]
        elif finished:
            # finish 动作没有观察，别把"任务完成"再拼一遍
            summary = "模型未提供 finish 摘要"
        elif done:
            # 保留最后观察，context_needed / 重规划要靠它传递跨 App 信息
            summary = f"任务已完成：{memory.get_last_observation()}"
        else:
            summary = ""
        return finished, done, summary

    def _decide(self, step: int, memory: TaskMemory) -> dict:
        """只对决策调用重试：此时还没有任何设备副作用"""
        last_error = None
        for attempt in range(1, config.STEP_RETRIES + 2):
            try:
                action = self._think_and_act(memory)
                print(f"│  💭  {action.get('thought', '')}")
                print(f"│  ▶   {action.get('description', '')}  [{action.get('action_type')}]")
                # Streamlit 控制台读的就是这个状态文件，之前只在 init_state 写过一次，
                # 导致页面永远停在「执行中」并无限自动刷新
                ui_state.update_step(step, action.get("thought", ""), action)
                return action
            except Exception as e:
                last_error = e
                print(f"│  ⚠️  第 {step} 步第 {attempt} 次决策失败：{e}")
                if attempt <= config.STEP_RETRIES:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"第 {step} 步决策重试 {config.STEP_RETRIES} 次后仍失败：{last_error}") from last_error

    def _save_log(self, memory: TaskMemory):
        """保存任务日志，并复制一份到本次运行的证据目录"""
        memory.save()
        if self.run_dir is not None:
            memory.save(path=str(self.run_dir / "task_log.json"))

    def _archive_step(self, step: int, action: dict, observation: str):
        """把本步动作（含原始坐标）和前后截图写入 run_dir，失败不影响主流程"""
        if self.run_dir is None:
            return
        try:
            record = {"step": step, "action": action, "observation": observation}
            if getattr(self, "_last_image_meta", None):
                # 记录本次发送图的规格与缩放比例，便于事后核对坐标
                record["image"] = self._last_image_meta
            if getattr(self, "_last_model_action", None):
                record["model_action"] = self._last_model_action
            if getattr(self, "_last_step_timings", None):
                # 每阶段耗时，便于事后定位慢在哪一层
                record["timings"] = self._last_step_timings
            with open(self.run_dir / f"step_{step:03d}.json", "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            sent_path = image_preprocess.sent_filename("screen_current.png", self._last_image_meta)
            if os.path.exists(sent_path):
                ext = os.path.splitext(sent_path)[1]
                shutil.copy(sent_path, self.run_dir / f"step_{step:03d}_sent{ext}")
            for src, suffix in (("screen_current.png", "current"),
                                ("screen_observe.png", "observe")):
                # finish 没有执行动作，screen_observe.png 是上一步的，归档会造成证据错位
                if suffix == "observe" and action.get("action_type") == "finish":
                    continue
                if os.path.exists(src):
                    shutil.copy(src, self.run_dir / f"step_{step:03d}_{suffix}.png")
        except Exception as e:
            print(f"  ⚠️  证据归档失败：{e}")