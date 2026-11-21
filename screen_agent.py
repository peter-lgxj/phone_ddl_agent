import json
import base64
import hashlib
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from phone_driver import create_phone_controller
from app_launcher import AppLauncher
from memory import TaskMemory, format_steps_compact
from ocr_locator import (OCRLocator, mark_text_in_input_boxes, resolve_action_element,
                         canonical_action_target, should_trigger_ocr, snap_action_coords,
                         enforce_tap_bounds)
import config
import image_preprocess
import rule_policy
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


def make_http_client() -> httpx.Client:
    """视觉与规划共用的 HTTP 连接池；keep-alive 避免多步任务每步重新握手。"""
    max_connections = int(getattr(config, "HTTP_MAX_CONNECTIONS", 8))
    return httpx.Client(
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=float(getattr(config, "HTTP_KEEPALIVE_SECONDS", 90)),
        ),
        timeout=httpx.Timeout(120.0, connect=10.0),
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


def consecutive_unresolved_targets(steps: list[dict], target: str) -> int:
    """统计最近连续多少次未定位到同一个目标，用于决定是否回退到坐标点击。"""
    wanted = canonical_action_target({"target": target})
    if not wanted:
        return 0
    count = 0
    for item in reversed(steps):
        action = item.get("action") or {}
        if (action.get("localization_failed")
                and canonical_action_target(action) == wanted):
            count += 1
        else:
            break
    return count


def effective_action_type(action: dict) -> str:
    """多动作序列按最后一个子动作判断类型（观察问题、滚动 OCR 都依赖它）。"""
    if not isinstance(action, dict):
        return ""
    if action.get("action_type") == "sequence":
        items = action.get("actions") or []
        return str((items[-1] if items else {}).get("action_type") or "").lower()
    return str(action.get("action_type") or "").lower()


def build_observe_question(last_action: dict, memory) -> str:
    """按动作类型构造观察问题。

    滚动类动作不能只让模型说一句“这是某界面”，否则聊天正文永远进不了
    observation；这里要求逐条列出可见消息，作为 OCR 之外的第一层文本来源。
    """
    action_type = effective_action_type(last_action)
    if last_action.get("action_type") == "sequence":
        items = last_action.get("actions") or []
        if items:
            last_action = items[-1]
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


READ_ONLY_BLOCKED_ACTIONS = {"send", "press_enter", "type"}

# 这些规则步骤只改变导航位置，观察用一次控件树 dump 即可，不必截图+settle
_LIGHT_RULE_OBSERVE = {
    "open_group", "search_entry", "search_type", "search_result",
    "tap_input", "back_after_miss",
}


class ScreenAgent:

    ACTIONS = """
- open_app(app_name)：打开 App，如"微信"、"美团"、"设置"
- tap(x, y)：点击屏幕上的按钮、图标、列表项等非输入框元素，坐标来自 OCR
- tap_input_box()：专门用于点击输入框，内置 AI 视觉定位，比 tap 更准确
- swipe(x, y, x2, y2)：从 (x,y) 滑动到 (x2,y2)，四个坐标必须提供
- type(text)：输入文字，必须在 tap_input_box 之后调用
- send()：发送消息或提交
- press_enter()：按回车（等同 send）
- back()：返回键
- home()：回桌面
- wait()：等待加载
- finish(summary)：任务完成
"""

    def __init__(self, max_steps: int = None, read_only: bool | None = None):
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
        # 视觉与规划共用同一个 HTTP 连接池：同 provider 时少一次 TLS 握手，
        # 多步任务复用 keep-alive 连接，不再每步重新建连。
        self._http_client = make_http_client()
        self.client = OpenAI(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            http_client=self._http_client,
        )

        # 纯文字大模型：负责语义分析、纠正 VL 的理解偏差
        planner_name = getattr(config, "PLANNER_PROVIDER", provider_name)
        planner_cfg  = config.PROVIDERS[planner_name]
        _require_api_key(planner_name, planner_cfg, role="规划模型")
        self.text_client = OpenAI(
            api_key=planner_cfg["api_key"],
            base_url=planner_cfg["base_url"],
            http_client=self._http_client,
        )
        self.text_model = planner_cfg["model"]
        self.text_extra_body = planner_cfg.get("extra_body")

        # 控件树由当前驱动提供：u2 用 dump_hierarchy()，Android 17 回退到 adb dump
        self.ocr      = OCRLocator(device=None)
        if getattr(config, "OCR_PREHEAT", True):
            started = time.perf_counter()
            try:
                self.ocr.warmup()
                print(f"OCR 预热完成：{time.perf_counter() - started:.1f}s")
            except Exception as e:
                print(f"  ⚠️  OCR 预热失败，首次 OCR 时再加载：{e}")
        self.phone    = create_phone_controller(self.ocr)
        self.driver_kind = getattr(self.phone, "kind", "uiautomator2")
        self.ocr.set_tree_provider(self.phone.dump_hierarchy)
        self.launcher = AppLauncher(self.phone)
        if self.driver_kind != "uiautomator2":
            print(
                "  ⚠️  当前使用 adb_dump 回退驱动：截图/控件树/点击可用，"
                "但本次不是完整 uiautomator2 链路"
            )
        # 把 HTTP TLS 握手与设备通道启动挪到任务开始前，不占用第一步
        self._warmup_connections()
        self.max_steps = max_steps or config.MAX_STEPS
        self.read_only = (
            getattr(config, "READ_ONLY_MODE", True) if read_only is None else read_only
        )
        # 规则快路开关由成本核算决定：adb_dump 下规则步要付两次 dump
        self.rule_fastpath_enabled, self._rule_cost = self._resolve_rule_fastpath()
        self.run_dir   = None
        self.last_status = "idle"   # done / timeout / failed / idle
        self._observe_valid = False  # 当前 screen_observe.png 是否是本步真实截图
        self._last_image_meta = None     # 本步发送图的规格与缩放比例
        self._last_model_action = None   # 缩放时模型返回的原始坐标（未反算）
        self._last_decision_timings = {}  # 决策各阶段耗时
        self._last_step_timings = {}      # 整步各阶段耗时（写进 step json）
        self.usage_events = []            # 全部视觉调用 usage，任务结束写汇总
        self._last_step_usage = []        # 本步 usage 事件（可含决策/观察/完成检测）
        self._last_observe_ocr_seconds = 0.0  # 本步滚动 OCR 耗时（未触发为 0）
        self._rule_state = {}             # 规则快路跨步状态（已进入的群、滚动次数等）
        self._rule_hits = []              # 规则命中记录，任务结束写汇总
        self._last_rule_seconds = 0.0     # 本步规则检查（控件树 dump）耗时
        self._total_step_seconds = 0.0    # 全部步骤墙钟耗时合计（统计规则步占比）
        self._model_decision_seconds = []  # 模型步的决策耗时样本
        self._last_frame_hash = None      # 上一帧截图哈希，用于同屏复用
        self._last_frame_bytes = None     # 上一帧编码后的字节
        self._last_frame_meta = None      # 上一帧图片元数据
        self._pending_settle_frame = None # settle 保留的稳定帧，供下一步复用
        self._last_reused_frame = False   # 本步是否复用了上一帧（写进 step JSON）

        print(f"视觉模型：{provider_name} / {self.model}")
        print(f"语义模型：{planner_name} / {self.text_model}")

    def _warmup_connections(self) -> None:
        """启动时预热 HTTP 连接与设备通道。

        - HTTP：复用同一个 httpx 连接池，并发一次 ``models.list()`` 完成 TLS/DNS 握手；
        - 设备：调用驱动的 ``warmup()``（adb 回退时启动 adb server）。
        失败只告警，不阻塞任务；预热耗时写进 ``self._warmup_seconds``，不计入第一步。
        """
        self._warmup_seconds = 0.0
        if not getattr(config, "CONNECTION_WARMUP", True):
            return
        started = time.perf_counter()
        try:
            self.client.models.list()
        except Exception as e:
            print(f"  ⚠️  HTTP 连接预热失败（首步会重新建连）：{e}")
        try:
            warmup = getattr(self.phone, "warmup", None)
            if callable(warmup):
                warmup()
        except Exception as e:
            print(f"  ⚠️  设备通道预热失败（首步会重新握手）：{e}")
        self._warmup_seconds = round(time.perf_counter() - started, 2)
        print(f"连接预热完成：{self._warmup_seconds}s（HTTP keep-alive + 设备通道）")

    def _resolve_rule_fastpath(self) -> tuple[bool, dict]:
        """按实测成本决定规则快路是否启用，并在启动日志说明原因。

        规则一步的本地成本 = 判断用的 dump + 观察用的截图与 dump + 动作执行；
        adb_dump 下单次 dump 实测约 2.5s，合计约 6.3s，已高于一次合并决策请求。
        显式 RULE_FASTPATH=1/0 可覆盖；auto 模式按成本比较结果决定。
        """
        mode = getattr(config, "RULE_FASTPATH_MODE", "auto")
        driver = getattr(self, "driver_kind", "adb_dump")
        if driver == "uiautomator2":
            dump = float(getattr(config, "RULE_DUMP_SECONDS_U2", 0.3))
        else:
            dump = float(getattr(config, "RULE_DUMP_SECONDS", 2.5))
        shot = float(getattr(config, "RULE_SCREENSHOT_SECONDS", 1.0))
        execute = float(getattr(config, "RULE_EXECUTE_SECONDS", 0.3))
        decision = float(getattr(config, "RULE_MODEL_DECISION_SECONDS", 5.6))
        # 开群/搜索等导航规则用轻观察（一次 dump，不截图/不 settle）；
        # 滚动规则仍保留截图，按 2 次 dump + 截图估算。
        estimate = round(dump * 2 + execute, 2)
        estimate_with_shot = round(dump * 2 + shot + execute, 2)
        enabled = mode != "off" and (mode == "on" or estimate <= decision)
        cost = {
            "mode": mode,
            "driver": driver,
            "dump_seconds": dump,
            "rule_step_estimate": estimate,
            "rule_step_estimate_with_screenshot": estimate_with_shot,
            "model_decision_seconds": decision,
            "enabled": enabled,
        }
        if mode == "off":
            print("规则快路：已按 RULE_FASTPATH=0 关闭")
        elif mode == "on":
            print(f"规则快路：已按 RULE_FASTPATH=1 强制开启（估算规则步 {estimate:.1f}s）")
        elif enabled:
            print(f"规则快路：自动开启（规则步估算 {estimate:.1f}s <= "
                  f"一次模型决策 {decision:.1f}s）")
        else:
            print(f"规则快路：默认关闭——{driver} 单次 dump 实测约 {dump:.1f}s，"
                  f"规则步估算 {estimate:.1f}s 高于一次模型决策 {decision:.1f}s；"
                  f"需要时设 RULE_FASTPATH=1 强制开启")
        return enabled, cost

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

    def _build_prompt(self, memory, history_images, history_text, ocr_result,
                      semantic_analysis, merged: bool = False) -> str:
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

        merged_section = ""
        observation_schema = ""
        if merged:
            merged_section = """
    ## 本步同时完成观察
    当前截图是上一步动作执行后的界面（第 1 步是初始界面）。先用 observation 描述截图里
    与任务相关的关键事实（页面/会话名、消息发送者与时间、是否找到目标等），再给出下一步 action。
    observation 只写截图事实，不要编造；找不到就写“未看到”。
"""
            observation_schema = '    "observation": "当前截图事实描述（本步动作执行前）",\n'
        sent_width, sent_height = (getattr(self, "_last_image_meta", None) or {}).get(
            "sent_size", [1080, 2400]
        )
        swipe_example = (
            f'{{"action_type":"swipe","x":{sent_width // 2},'
            f'"y":{sent_height * 3 // 4},"x2":{sent_width // 2},'
            f'"y2":{sent_height // 3},"task_done":false}}'
        )

        if ocr_result:
            ocr_section = ocr_result
            coord_rule = "1. tap 坐标根据当前截图估计，可参考 OCR；程序会按目标文字再次吸附"
        else:
            ocr_section = "（本步未预跑 OCR；请依据截图估计元素位置，tap 落点会再做校正）"
            coord_rule = "1. tap 坐标请根据截图中元素的像素位置估计，尽量落在元素中心"

        return f"""你是一个手机操作智能体，通过观察手机截图决定下一步操作。

    ## 总体任务
    {memory.task}

    ## 已执行的历史步骤（原图只保留最近若干张，更早步骤为文字摘要）
    {history_text}
{semantic_section}{merged_section}
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
    4. 默认每次只返回一个动作；只有连续动作彼此不依赖时（例如连续两次 swipe、swipe 后 back）才可用 "actions" 数组一次返回，程序按顺序执行、仍只算一步；需要看执行结果再决定的一律只返回一个动作
    5. 可直接打开的 App：{known_apps}
    6. 搜索框输入后，查看搜索结果并选择目标；聊天消息输入后才使用 send
    7. 历史中验证结果包含"输入成功"只表示文字已输入，不表示搜索或发送已完成
    8. tap 时 target 只填目标元素的文字本身（如"昊"），元素类型写在 description（如"点击联系人昊"）
    {duty_rule}
    10. 查找已知名称的联系人、群或内容时，先检查截图顶部的放大镜图标或搜索框。放大镜就是搜索入口；只要它可见且目标不在当前列表，本步必须 tap 搜索入口（target 填"搜索"），下一步输入目标名称。此时禁止用 swipe 翻列表。只有截图中确实没有搜索入口时才允许 swipe。
    11. 当前发送图尺寸为 {sent_width}x{sent_height}；tap 必须填写 x、y；swipe 必须填写 x、y、x2、y2。坐标均为当前发送图内的整数像素坐标。不要使用 x1/y1 作为起点字段。
    12. 向上滚动列表的合法示例：{swipe_example}

    ## 何时调用 finish
    {finish_rules}

    ## 只返回 JSON：
    {{
{observation_schema}    "thought": "当前截图显示...，下一步应该...",
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
    "task_done": 布尔值（整项任务是否已全部完成，未完成填 false）,
    "actions": [{{"action_type": "swipe", "x": 数字, "y": 数字, "x2": 数字, "y2": 数字}}]
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

    def _think_and_act(self, memory: TaskMemory, merged: bool = False) -> dict | None:
        timings = {}
        self._last_merged_parse_failed = False
        self._last_merged_observation = ""
        self._last_merged_ocr_text = ""
        started = time.time()
        current_bytes, image_meta, reused_frame = self._capture_frame(timings)
        self._last_image_meta = image_meta
        self._last_model_action = None
        current_b64 = base64.b64encode(current_bytes).decode("utf-8")
        if not reused_frame:
            print(f"  🖼  发送图：{image_preprocess.describe(image_meta)}")
            with open(image_preprocess.sent_filename("screen_current.png", image_meta), "wb") as f:
                f.write(current_bytes)

        started = time.time()
        ocr_mode = getattr(config, "OCR_MODE", "on_demand")
        ocr_result = ""
        if ocr_mode == "always":
            ocr_result = self.ocr.format_for_prompt("screen_current.png")
        # 合并观察时，上一步若为滚动动作，仍然把当前屏幕 OCR 原文并入 observation。
        # OCR 与预编码/历史图准备并行，等 prompt 要拼 OCR 时再取结果。
        ocr_executor = None
        ocr_future = None
        ocr_started = time.perf_counter()
        if merged and memory.steps:
            prev_type = effective_action_type(memory.steps[-1].get("action") or {})
            if (getattr(config, "OBSERVE_OCR_ENABLED", False)
                    and prev_type in getattr(config, "OBSERVE_OCR_ACTIONS", {"swipe", "scroll"})):
                ocr_executor = ThreadPoolExecutor(max_workers=1)
                ocr_future = ocr_executor.submit(self.ocr.detect_ocr, "screen_current.png")
        timings["ocr_triggered"] = ocr_mode == "always"
        timings["ocr_reason"] = "always" if ocr_mode == "always" else ""

        started = time.time()
        if ocr_future is None:
            timings["ocr_pre"] = 0.0

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
        timings["preprocess"] = round(
            timings.get("preprocess", 0.0) + (time.time() - started), 2
        )

        # 预编码与历史图准备做完后再取 OCR：此时通常已并行跑完，只剩很短的等待
        if ocr_future is not None:
            wait_started = time.perf_counter()
            try:
                elements = ocr_future.result()
                text = format_ocr_observation(elements)
                self._last_merged_ocr_text = text
                if text:
                    ocr_result = (ocr_result + "\n[上一动作后的 OCR]\n" + text).strip()
            except Exception as e:
                print(f"  ⚠️  合并观察 OCR 失败，保留模型描述：{e}")
            finally:
                ocr_executor.shutdown(wait=False)
            timings["ocr_wait"] = round(time.perf_counter() - wait_started, 2)
            timings["ocr_pre"] = round(time.perf_counter() - ocr_started, 2)
        timings["ocr"] = timings["ocr_pre"]

        # ── 文字大模型先做语义分析（有历史才分析，默认关闭）──
        started = time.time()
        semantic_analysis = ""
        if memory.steps and config.ENABLE_SEMANTIC_ANALYSIS:
            semantic_analysis = self._analyze_history(memory.task, memory.steps)
        timings["semantic"] = round(time.time() - started, 2)

        prompt = self._build_prompt(memory, history_images, history_text,
                                    ocr_result, semantic_analysis, merged=merged)

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
        self._record_usage("decision", response)

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
                if merged:
                    # 合并响应不合法：本步回退旧的两段式，先不执行任何动作
                    self._last_merged_parse_failed = True
                    self._last_decision_timings = timings
                    return None
                action = {"action_type": "wait", "description": "解析异常，等待重试"}

        if merged:
            if (not isinstance(action, dict) or "observation" not in action
                    or not (action.get("action_type") or action.get("actions"))):
                print(f"  ⚠️  合并响应缺少 observation/action，回退两段式")
                self._last_merged_parse_failed = True
                self._last_decision_timings = timings
                return None
            self._last_merged_observation = str(action.pop("observation") or "").strip()
            if (self._last_merged_ocr_text
                    and self._last_merged_ocr_text not in self._last_merged_observation):
                self._last_merged_observation = (
                    f"{self._last_merged_observation}\n[OCR]\n{self._last_merged_ocr_text}"
                ).strip()

        # 一次响应可返回多个连续动作（步骤语义仍按请求计）
        raw_actions = None
        if isinstance(action.get("actions"), list):
            raw_actions = [
                item for item in action["actions"]
                if isinstance(item, dict) and item.get("action_type")
            ]
        if raw_actions:
            limit = max(1, int(getattr(config, "MAX_ACTIONS_PER_STEP", 3)))
            raw_actions = raw_actions[:limit]
            cache = {}
            mapped_actions = []
            for item in raw_actions:
                item = image_preprocess.map_action_coords(item, image_meta)
                mapped_actions.append(
                    self._snap_mapped_action(item, timings, memory, cache))
            if image_meta.get("resized"):
                self._last_model_action = {"actions": raw_actions}
            timings["sequence_len"] = len(mapped_actions)
            mapped = {
                "action_type": "sequence",
                "actions": mapped_actions,
                "thought": action.get("thought", ""),
                "description": "；".join(
                    item.get("description") or item.get("action_type", "")
                    for item in mapped_actions
                ),
                "target": "",
                "task_done": bool(
                    action.get("task_done")
                    or any(item.get("task_done") for item in mapped_actions)
                ),
            }
            self._last_decision_timings = timings
            return mapped

        # 缩放过才反算；未缩放时 map_action_coords 原样返回，不引入偏移
        mapped = image_preprocess.map_action_coords(action, image_meta)
        keys = ("x", "y", "x2", "y2")
        if image_meta.get("resized") and any(k in action for k in keys):
            self._last_model_action = action
            print(f"  🧭 坐标反算：{ {k: action[k] for k in keys if k in action} } -> "
                  f"{ {k: mapped[k] for k in keys if k in mapped} }")

        # tap 后统一收集候选：单个匹配直接吸附，多个匹配按模型坐标就近吸附。
        mapped = self._snap_mapped_action(mapped, timings, memory)

        self._last_decision_timings = timings
        return mapped

    def _snap_mapped_action(self, mapped: dict, timings: dict, memory: TaskMemory,
                            cache: dict | None = None) -> dict:
        """对 tap 做控件树/OCR 吸附；多动作序列共享同一次树与 OCR 结果。"""
        ocr_mode = getattr(config, "OCR_MODE", "on_demand")
        if not should_trigger_ocr(ocr_mode, mapped.get("action_type", "")):
            return mapped
        cache = cache if cache is not None else {}
        snap_mode = getattr(config, "OCR_SNAP_MODE", "tree_first")
        max_distance = int(getattr(config, "OCR_SNAP_MAX_DISTANCE", 80))
        snap_source = "none"
        moves = []
        candidates = []
        tree_elements = []
        tree_elapsed = ocr_elapsed = 0.0
        yolo_elapsed = 0.0
        tree_count = ocr_count = 0
        ocr_meta = {}

        if snap_mode in ("tree_first", "tree_only"):
            if "tree" not in cache:
                started = time.time()
                try:
                    cache["tree"] = self.ocr.detect_tree()
                except Exception as e:
                    print(f"  ⚠️  控件树读取失败，继续走 OCR：{e}")
                    cache["tree"] = []
                cache["tree_elapsed"] = round(time.time() - started, 2)
            tree_elements = cache["tree"]
            tree_elapsed = cache["tree_elapsed"]
            tree_count = len(tree_elements)
            candidates.extend(tree_elements)

        target = str(mapped.get("target") or "").strip()
        description = str(mapped.get("description") or "")
        wants_search = "搜索" in target or "放大镜" in target
        wants_contact = any(word in description + target
                            for word in ("联系人", "群聊", "聊天项"))
        needs_input_boxes = wants_contact and not any(
            item.get("type") == "input" for item in tree_elements)
        if (snap_mode == "tree_first"
                and getattr(config, "SEARCH_ICON_DETECTION", True)
                and (wants_search or needs_input_boxes)):
            started = time.time()
            try:
                detector = getattr(self, "_gui_detector", None)
                if detector is None:
                    from input_detector import InputDetector
                    detector = self._gui_detector = InputDetector()
                detections = detector.detect("screen_current.png", conf=0.45)
                candidates.extend(item for item in detections
                                  if (wants_search and item.get("class_name") == "search_icon")
                                  or (needs_input_boxes and item.get("class_name") == "input_box"
                                      and item.get("confidence", 0) >= 0.65))
            except Exception as e:
                print(f"  ⚠️  YOLO 元素检测失败，继续走 OCR：{e}")
            yolo_elapsed = round(time.time() - started, 2)

        if snap_mode in ("tree_first", "ocr_only"):
            if "ocr_raw" not in cache:
                started = time.time()
                try:
                    cache["ocr_raw"] = self.ocr.detect_ocr("screen_current.png")
                except Exception as e:
                    print(f"  ⚠️  OCR 失败，保留模型坐标：{e}")
                    cache["ocr_raw"] = []
                cache["ocr_elapsed"] = round(time.time() - started, 2)
                cache["ocr_meta"] = dict(getattr(self.ocr, "last_meta", {}) or {})
            ocr_elements = mark_text_in_input_boxes(cache["ocr_raw"], candidates)
            ocr_elapsed = cache["ocr_elapsed"]
            ocr_count = len(ocr_elements)
            candidates.extend(ocr_elements)
            ocr_meta = cache["ocr_meta"]

        found = resolve_action_element(mapped, candidates, max_distance)
        if found:
            snap_source = found[0].get("source", "unknown")
            mapped, moves = snap_action_coords(mapped, candidates, max_distance)

        # 用控件树 bounds 复核 tap 落点：群列表/会话列表禁止按比例换算定位，
        # 越界、目标歧义或落点不在目标元素内一律拒绝执行并记录原因。
        screen_size = tuple(
            (getattr(self, "_last_image_meta", None) or {}).get("orig_size")
            or (1080, 2374)
        )
        guarded, guard = enforce_tap_bounds(mapped, candidates, screen_size)
        timings["coordinate_guard"] = guard
        if guarded is None:
            reason = guard.get("reason", "rejected")
            print(f"  ⛔ 坐标校验拒绝点击：{reason}（{guard.get('target', '')}）")
            return {
                **mapped,
                "action_type": "wait",
                "coordinate_rejected": reason,
                "thought": f"坐标校验拒绝：{reason}，保持当前界面重新观察",
                "description": f"拒绝在 {guard.get('target') or '未知目标'} 上按比例坐标点击",
            }
        mapped = guarded
        if guard.get("moved"):
            print(f"  📐 采用控件树 bounds：{guard.get('model_point')} -> "
                  f"{[mapped.get('x'), mapped.get('y')]}（{guard.get('target')}）")

        timings["ocr"] = round(
            timings.get("ocr", 0.0) + tree_elapsed + yolo_elapsed + ocr_elapsed, 2
        )
        timings["ocr_triggered"] = True
        timings["ocr_reason"] = "tap"
        timings["snap_source"] = snap_source
        timings["snap_tree"] = {"elapsed": tree_elapsed, "elements": tree_count}
        timings["snap_yolo"] = {"elapsed": yolo_elapsed}
        timings["snap_ocr"] = {"elapsed": ocr_elapsed, "elements": ocr_count,
                               "scale": ocr_meta.get("scale"),
                               "scaled_size": ocr_meta.get("scaled_size"),
                               "engine": ocr_meta.get("engine")}
        timings["ocr_snap"] = {"moves": moves}
        if moves:
            print(f"  🎯 {snap_source} 吸附：{moves}")
        if target and not found:
            mapped = dict(mapped)
            timings["target_unresolved"] = target
            prior = consecutive_unresolved_targets(memory.steps, target)
            if prior >= 1 and mapped.get("x") is not None and mapped.get("y") is not None:
                # 连续两轮都定位不到（常见于返回键、发送键等无文字图标）：
                # 不再终止任务，回退到模型给出的坐标继续，保留可恢复路径。
                mapped["localization_fallback"] = True
                mapped["thought"] = (
                    f"连续 {prior + 1} 次未匹配到目标「{target}」的文字元素，"
                    "回退到模型坐标继续尝试。"
                )
                mapped["description"] = f"目标「{target}」未定位，按原坐标点击"
                timings["localization_fallback"] = True
                print(f"  ⚠️  {mapped['thought']}")
            else:
                mapped["action_type"] = "wait"
                mapped["localization_failed"] = True
                mapped["thought"] = (
                    f"未找到与目标「{target}」文字及类型匹配的元素，避免误点。"
                )
                mapped["description"] = f"目标「{target}」未定位，保持当前界面并重新观察"
                print(f"  ⚠️  {mapped['thought']}")
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

    def _settle_screen(self) -> float:
        """等待界面稳定：adaptive 时截图哈希连续两次相同就返回，最多等上限。"""
        max_wait = float(getattr(config, "OBSERVE_SETTLE_SECONDS", 1.0))
        if max_wait <= 0:
            return 0.0
        started = time.perf_counter()
        if getattr(config, "OBSERVE_SETTLE_MODE", "adaptive") != "adaptive":
            time.sleep(max_wait)
            return max_wait

        poll = max(float(getattr(config, "OBSERVE_SETTLE_POLL_SECONDS", 0.15)), 0.02)
        previous = None
        digest = None
        try:
            while time.perf_counter() - started < max_wait:
                self.phone.screenshot("screen_settle.png")
                with open("screen_settle.png", "rb") as f:
                    digest = hashlib.md5(f.read()).hexdigest()
                if digest == previous:
                    break
                previous = digest
                time.sleep(poll)
        except Exception as e:
            print(f"  ⚠️  自适应 settle 失败，回退固定等待：{e}")
            remaining = max_wait - (time.perf_counter() - started)
            if remaining > 0:
                time.sleep(remaining)
        finally:
            # 保留 settle 的最后一帧：下一步若哈希未变，可直接复用，省一次截图与重编码
            if digest is not None and os.path.exists("screen_settle.png"):
                try:
                    os.replace("screen_settle.png", "screen_settle_last.png")
                    self._pending_settle_frame = {
                        "path": "screen_settle_last.png", "hash": digest,
                    }
                except OSError:
                    pass
            else:
                try:
                    os.remove("screen_settle.png")
                except OSError:
                    pass
        return round(time.perf_counter() - started, 2)

    def _capture_frame(self, timings: dict) -> tuple[bytes, dict, bool]:
        """截取当前帧；能用上一帧时跳过重新截图与重编码。

        返回 (发送字节, 图片元数据, 是否复用上一帧)。哈希未变时 reused=True，
        并在 step JSON 里写 reused_frame=true。
        """
        started = time.perf_counter()
        pending = getattr(self, "_pending_settle_frame", None)
        self._pending_settle_frame = None
        reuse_capture = False
        if pending and os.path.exists(pending["path"]):
            digest = pending["hash"]
            try:
                shutil.copyfile(pending["path"], "screen_current.png")
                os.remove(pending["path"])
                reuse_capture = True
            except OSError as e:
                print(f"  ⚠️  settle 帧复用失败，改为重新截图：{e}")
                digest = None

        if not reuse_capture:
            if hasattr(self.phone, "capture_with_dump"):
                # screencap 与 uiautomator dump 并行发起，互不阻塞；
                # dump 结果留在驱动缓存里，本步需要控件树时按帧指纹复用。
                _, xml, report = self.phone.capture_with_dump("screen_current.png")
                timings["parallel_capture"] = report
                if xml is None:
                    timings["capture_dump"] = "failed"
            else:
                self.phone.screenshot("screen_current.png")
            with open("screen_current.png", "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()

        # 截图指纹供控件树缓存复用；界面变了指纹就变，旧 dump 自动失效
        if hasattr(self.phone, "set_frame_fingerprint"):
            self.phone.set_frame_fingerprint(digest)

        last_hash = getattr(self, "_last_frame_hash", None)
        last_bytes = getattr(self, "_last_frame_bytes", None)
        last_meta = getattr(self, "_last_frame_meta", None)
        reused = (digest == last_hash and last_bytes is not None)
        self._last_reused_frame = bool(reused)
        timings["screenshot"] = round(time.perf_counter() - started, 2)
        timings["reused_frame"] = bool(reused)
        timings["reuse_reason"] = "hash" if reused else ("settle" if reuse_capture else "")

        if reused:
            print("  ♻️  画面未变，复用上一帧（跳过截图与重编码）")
            timings["preprocess"] = 0.0
            return last_bytes, last_meta, True

        encode_started = time.perf_counter()
        data, meta = image_preprocess.prepare_image_bytes("screen_current.png")
        timings["preprocess"] = round(time.perf_counter() - encode_started, 2)
        self._last_frame_hash = digest
        self._last_frame_bytes = data
        self._last_frame_meta = meta
        return data, meta, False

    def _observe(self, last_action: dict, memory: TaskMemory) -> str:
        """执行动作后截图，VL 模型描述结果；滚动类动作再叠加 OCR 文本。"""
        self._settle_screen()
        self._clear_observe_shot()   # 先删旧图：截图失败时不会留下过期画面
        self._last_observe_ocr_seconds = 0.0
        self.phone.screenshot("screen_observe.png")
        observe_bytes, observe_meta = image_preprocess.prepare_image_bytes("screen_observe.png")
        image_b64 = base64.b64encode(observe_bytes).decode("utf-8")

        action_type = effective_action_type(last_action)
        verify_question = build_observe_question(last_action, memory)

        # 截图与 OCR 互不依赖：OCR 先起一个线程，和视觉观察请求并行跑。
        # OCR 失败只影响补充文本，不影响截图与本次观察。
        ocr_actions = getattr(config, "OBSERVE_OCR_ACTIONS", {"swipe", "scroll"})
        want_ocr = (
            getattr(config, "OBSERVE_OCR_ENABLED", False)
            and action_type in ocr_actions
        )
        executor = ThreadPoolExecutor(max_workers=1) if want_ocr else None
        ocr_started = time.perf_counter()
        ocr_future = (executor.submit(self.ocr.detect_ocr, "screen_observe.png")
                      if executor else None)

        try:
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
            self._record_usage("observe", response)
            observation = response.choices[0].message.content.strip()
        finally:
            if executor is not None:
                executor.shutdown(wait=False)

        if ocr_future is not None:
            try:
                elements = ocr_future.result()
                elapsed = round(time.perf_counter() - ocr_started, 2)
                self._last_observe_ocr_seconds = elapsed
                ocr_text = format_ocr_observation(elements)
                if ocr_text and ocr_text not in observation:
                    observation = f"{observation}\n[OCR]\n{ocr_text}"
                print(f"│  🔎  滚动 OCR：{len(elements)} 个文本框，{elapsed}s（与观察并行）")
            except Exception as e:
                # OCR 只是补充通道，失败不能中断滚动或让整步失败
                print(f"│  ⚠️  滚动 OCR 失败，保留模型描述：{e}")
        return observation

    # ══════════════════════════════════════════════════════════
    #  执行（保持原样，不改动）
    # ══════════════════════════════════════════════════════════

    def _execute(self, action: dict) -> bool:
        t = action.get("action_type", "wait")
        search_type_allowed = False

        if self.read_only and t in READ_ONLY_BLOCKED_ACTIONS:
            rule_name = (action.get("rule") or {}).get("name")
            # 只放行「搜索框输入」：它只改搜索框，不产生外发；
            # 消息输入框的 type、send、press_enter 仍一律拒绝。
            allowed_search = (
                t == "type"
                and action.get("search_input") is True
                and rule_name == "search_type"
            )
            if t == "type" and not allowed_search:
                # 模型自己选的搜索输入：按当前控件树判断，不看坐标
                try:
                    current_xml = self.phone.dump_hierarchy()
                except Exception:
                    current_xml = ""
                allowed_search = rule_policy.is_search_input(current_xml)
            if allowed_search:
                search_type_allowed = True
                assert not action.get("send") and "发送" not in str(
                    action.get("target") or ""), "搜索输入不得携带发送语义"
                print("  🔎  只读模式放行搜索框输入（不产生外发）")
            else:
                reason = f"只读模式拒绝动作「{t}」：读屏任务不允许发言或改动输入内容"
                action["blocked"] = True
                action["blocked_reason"] = reason
                print(f"  ⛔  {reason}")
                return False

        if t == "open_app":
            self.launcher.launch(action.get("app_name", ""))
        elif t == "tap_input_box":
            self.phone.tap_input_box()
        elif t == "type":
            text = action.get("text")
            if not text:
                print("  ⚠️  type 动作缺少 text 字段，跳过本步")
                return False
            self.phone.type_text(
                text, allow_search=search_type_allowed or bool(action.get("search_input")))
        elif t == "send":
            self.phone.send()
            print("回车发送")
        elif t == "press_enter":
            self.phone.press_enter()
            print("按下回车")
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
            start_x = action.get("x", action.get("x1"))
            start_y = action.get("y", action.get("y1"))
            if start_x is None or start_y is None or action.get("x2") is None or action.get("y2") is None:
                print("  ⚠️  swipe 动作缺少坐标，跳过本步")
                return False
            x  = self._parse_coord(start_x)
            y  = self._parse_coord(start_y)
            x2 = self._parse_coord(action["x2"])
            y2 = self._parse_coord(action["y2"])
            self.phone.swipe(x, y, x2, y2)

        return False

    def _execute_sequence(self, actions: list[dict]) -> bool:
        """顺序执行一次响应里的多个连续动作；任一步被只读拦截则停止后续动作。"""
        for item in actions:
            if self._execute(item):
                return True
            if item.get("blocked"):
                return False
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
        self._record_usage("completion", response)

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
    #  规则快路：控件树能证明的动作不调用视觉模型
    # ══════════════════════════════════════════════════════════

    def _rule_action(self, memory: TaskMemory) -> dict | None:
        """尝试用规则决定本步动作；None 表示需要 VLM 判断。

        命中时省掉一次决策请求（合并观察模式下也就是本步唯一的一次视觉调用）。
        """
        self._last_rule_seconds = 0.0
        if not getattr(self, "rule_fastpath_enabled",
                       getattr(config, "RULE_FASTPATH", True)):
            return None
        state = getattr(self, "_rule_state", None)
        if state is None:
            self._rule_state = state = {}
        if state.get("disabled"):
            return None

        task = memory.task or ""
        # 没有引号目标、也不含搜索意图时规则不会命中：跳过控件树 dump，不增加开销
        if not rule_policy.extract_quoted_targets(task) and "搜索" not in task:
            return None

        started = time.time()
        try:
            xml = self.phone.dump_hierarchy()
        except Exception as e:
            print(f"  ⚠️  规则快路读取控件树失败，交回视觉模型：{e}")
            state["disabled"] = True
            return None
        self._last_rule_seconds = time.time() - started

        last_action = dict(memory.steps[-1].get("action") or {}) if memory.steps else {}
        action = rule_policy.decide(rule_policy.RuleContext(
            task=task,
            step=len(memory.steps) + 1,
            read_only=self.read_only,
            xml=xml,
            last_action=last_action,
            state=state,
            max_scrolls=int(getattr(config, "RULE_MAX_SCROLLS", 2)),
            search_ascii_only=self.driver_kind != "uiautomator2",
            search_force=bool(getattr(config, "RULE_SEARCH_FORCE", False)),
            search_allowed=(
                self.driver_kind == "uiautomator2"
                or bool(getattr(config, "RULE_SEARCH_FORCE", False))
            ),
            known_input_resource_ids=(
                getattr(config, "WECHAT_INPUT_RESOURCE_ID", ""),
            ),
        ))
        if action is None:
            return None
        if action.get("action_type") == "tap":
            # 只读保护之外的硬约束：规则 tap 绝不允许落在发送按钮文案或坐标区域
            reason = rule_policy.tap_hits_send(
                xml, int(action["x"]), int(action["y"]),
                str(action.get("target") or ""))
            if reason:
                raise AssertionError(
                    f"规则快路 tap 落在发送按钮区域（{reason}）：{action}")
        elif action.get("action_type") == "type":
            # 规则只允许搜索框输入；缺 search_input 或换了规则名直接失败
            assert action.get("search_input") is True, "规则 type 只能是搜索框输入"
            assert (action.get("rule") or {}).get("name") == "search_type", \
                "规则 type 必须来自 search_type"
            assert rule_policy.is_search_input(xml, str(action.get("resource_id") or "")), \
                "当前界面不是搜索框，拒绝规则输入"

        record = {"name": action["rule"]["name"], "step": len(memory.steps) + 1,
                  "saved_calls": 1}
        self._rule_hits.append(record)
        print(f"  ⚡ 规则快路命中：{record['name']}"
              f"（跳过视觉决策，省 {record['saved_calls']} 次模型调用）")
        return action

    def _rule_observe(self, action: dict, memory: TaskMemory) -> str:
        """规则步骤的观察：截图 + 控件树文字，不调用视觉模型。

        开群/搜索这类导航步骤只要知道「现在到了哪个界面」，用一次控件树 dump
        就够；截图与 settle 留给滚动手势（需要视觉证据判断是否到顶/到底）。
        """
        head = f"[规则] {action.get('description') or action.get('action_type')}"
        rule_name = (action.get("rule") or {}).get("name")
        if rule_name in _LIGHT_RULE_OBSERVE:
            try:
                text = rule_policy.screen_text(self.phone.dump_hierarchy())
            except Exception as e:
                text = ""
                print(f"  ⚠️  规则步骤读取控件树失败：{e}")
            return f"{head}\n{text}".strip() if text else head

        self._settle_screen()
        self._clear_observe_shot()
        try:
            self.phone.screenshot("screen_observe.png")
            self._observe_valid = True
        except Exception as e:
            self._observe_valid = False
            print(f"  ⚠️  规则步骤截图失败：{e}")
            return head

        try:
            text = rule_policy.screen_text(self.phone.dump_hierarchy())
        except Exception as e:
            text = ""
            print(f"  ⚠️  规则步骤读取控件树失败：{e}")
        return f"{head}\n{text}".strip() if text else head

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
                self._save_usage_summary(memory)
                ui_state.set_status("finished", summary)
                self.last_status = "done"
                return

            print(f"└{'─'*42}")
            time.sleep(0.5)

        print(f"\n⚠️  已达最大步数 {self.max_steps} 步")
        self._save_log(memory)
        self._save_usage_summary(memory)
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
        self._last_reused_frame = False
        self._last_step_usage = []
        self._step_started = time.time()
        raw_dumps = getattr(self.phone, "dump_count", 0)
        self._step_dump_start = raw_dumps if isinstance(raw_dumps, int) else 0
        merged_active = (
            getattr(config, "MERGED_OBSERVE", True)
            and getattr(config, "COMPLETION_CHECK", "decision") == "decision"
        )
        self._last_observation_scope = "pre_action" if merged_active else "post_action"
        started = time.time()
        rule_action = self._rule_action(memory) if merged_active else None
        if rule_action is not None:
            action = rule_action
            timings["rule_hit"] = True
            timings["rule_name"] = action["rule"]["name"]
            timings["rule_saved_calls"] = 1
            timings["rule_check"] = round(self._last_rule_seconds, 2)
            observation = ""
        elif merged_active:
            action = self._decide(step, memory, merged=True)
            if action is None:
                # 合并响应不合法：本步回退旧的两段式（先观察当前屏幕，再决策）
                timings["merged_fallback"] = True
                print("│  ↩️  合并观察回退为两段式")
                prev_action = dict(memory.steps[-1].get("action") or {}) if memory.steps else {}
                try:
                    observation = self._observe(prev_action, memory)
                    self._observe_valid = True
                except Exception as e:
                    observation = f"观察失败（回退两段式，未执行动作）：{e}"
                    self._clear_observe_shot()
                    print(f"│  ⚠️  {observation}")
                action = self._decide(step, memory, merged=False)
            else:
                observation = self._last_merged_observation or "（合并响应未提供观察）"
                self._observe_valid = True
        else:
            action = self._decide(step, memory)
        if rule_action is None:
            # 合并本步决策阶段（截图/OCR/预编码/模型）的真实耗时，
            # 避免 step JSON 沿用上一轮的阶段数据
            timings.update(getattr(self, "_last_decision_timings", {}) or {})
        timings["decide_total"] = round(time.time() - started, 2)

        # 动作只执行一次，不随观察/完成检测失败而重放
        # 动作执行后旧截图作废，观察成功才重新有效
        self._observe_valid = False
        started = time.time()
        if action.get("action_type") == "sequence":
            finished = self._execute_sequence(action.get("actions") or [])
            timings["actions"] = len(action.get("actions") or [])
        else:
            finished = self._execute(action)
        timings["execute"] = round(time.time() - started, 2)

        if merged_active:
            timings["observe"] = 0.0
            if finished:
                observation = "任务完成"
            elif timings.get("rule_hit"):
                # 规则步骤没有模型返回的 observation，用截图 + 控件树文字本地补齐
                started = time.time()
                observation = self._rule_observe(action, memory)
                timings["observe"] = round(time.time() - started, 2)
            else:
                # 合并模式下这一步没有独立的观察请求；等待界面稳定，供下一步截图
                started = time.time()
                timings["settle"] = self._settle_screen()
            print(f"│  👁   {observation}")
            ui_state.update_observation(observation)
            memory.add_step(
                thought=action.get("thought", ""),
                action=action,
                observation=observation
            )
            done = False
            started = time.time()
            if not finished and step >= 3 and action.get("task_done"):
                done = True
                print("│  🔍  决策模型标记任务完成")
            timings["completion"] = round(time.time() - started, 2)
            self._finalize_step_metrics(timings)
            self._last_step_timings = timings
            self._archive_step(step, action, observation)
            return self._step_summary(finished, done, action, memory)

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

        self._finalize_step_metrics(timings)
        self._last_step_timings = timings
        self._archive_step(step, action, observation)

        return self._step_summary(finished, done, action, memory)

    def _finalize_step_metrics(self, timings: dict) -> None:
        """记录本步墙钟耗时，供规则步/模型步分组统计（写进 step timings 与汇总）。"""
        elapsed = round(time.time() - getattr(self, "_step_started", time.time()), 2)
        timings["step_seconds"] = elapsed
        raw_dumps = getattr(self.phone, "dump_count", None)
        if isinstance(raw_dumps, int):
            timings["dumps"] = raw_dumps - int(getattr(self, "_step_dump_start", 0))
        self._total_step_seconds = round(
            getattr(self, "_total_step_seconds", 0.0) + elapsed, 2)
        if timings.get("rule_hit"):
            if self._rule_hits:
                self._rule_hits[-1]["seconds"] = elapsed
            return
        decision = timings.get("decision")
        if isinstance(decision, (int, float)):
            samples = getattr(self, "_model_decision_seconds", None)
            if samples is None:
                self._model_decision_seconds = samples = []
            samples.append(float(decision))

    @staticmethod
    def _step_summary(finished, done, action, memory):
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

    def _decide(self, step: int, memory: TaskMemory, merged: bool = False) -> dict | None:
        """只对决策调用重试：此时还没有任何设备副作用"""
        last_error = None
        for attempt in range(1, config.STEP_RETRIES + 2):
            try:
                action = (self._think_and_act(memory, merged=True) if merged
                          else self._think_and_act(memory))
                if action is None and merged:
                    # 合并响应不合法不在此重试；交给 _execute_step 回退两段式
                    return None
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

    @staticmethod
    def _usage_from_response(response) -> dict | None:
        """读取 provider 返回的 usage；字段缺失时返回 None，不抛异常。"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if prompt is None and completion is None:
            return None
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = getattr(usage, "total_tokens", None)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(total) if total is not None else prompt + completion,
        }

    def _record_usage(self, kind: str, response) -> None:
        event = {
            "kind": kind,
            "provider": getattr(config, "ACTIVE_PROVIDER", "unknown"),
            "model": getattr(self, "model", ""),
            "usage": self._usage_from_response(response),
        }
        if getattr(self, "_last_step_usage", None) is None:
            self._last_step_usage = []
        self._last_step_usage.append(event)
        if getattr(self, "usage_events", None) is None:
            self.usage_events = []
        self.usage_events.append(event)

    @staticmethod
    def _summarize_usage_events(events: list[dict]) -> dict | None:
        if not events:
            return None
        valid = [e["usage"] for e in events if e.get("usage")]
        if not valid:
            return None
        prompt = sum(item["prompt_tokens"] for item in valid)
        completion = sum(item["completion_tokens"] for item in valid)
        total = sum(item["total_tokens"] for item in valid)
        return {
            "calls": events,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "missing_usage_calls": len(events) - len(valid),
        }

    def _usage_summary(self, steps: int) -> dict:
        events = list(getattr(self, "usage_events", []) or [])
        valid = [e["usage"] for e in events if e.get("usage")]
        prompt = sum(item["prompt_tokens"] for item in valid)
        completion = sum(item["completion_tokens"] for item in valid)
        total = sum(item["total_tokens"] for item in valid)
        by_provider: dict[str, dict] = {}
        for event in events:
            provider = event.get("provider") or "unknown"
            bucket = by_provider.setdefault(provider, {
                "calls": 0, "missing_usage_calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            })
            bucket["calls"] += 1
            usage = event.get("usage")
            if usage:
                bucket["prompt_tokens"] += usage["prompt_tokens"]
                bucket["completion_tokens"] += usage["completion_tokens"]
                bucket["total_tokens"] += usage["total_tokens"]
            else:
                bucket["missing_usage_calls"] += 1
        denominator = max(int(steps), 1)
        driver = getattr(self, "driver_kind", "uiautomator2")
        rule_hits = list(getattr(self, "_rule_hits", []) or [])
        rule_by_name: dict[str, int] = {}
        for item in rule_hits:
            name = item.get("name", "unknown")
            rule_by_name[name] = rule_by_name.get(name, 0) + 1
        rule_seconds = round(
            sum(float(item.get("seconds") or 0.0) for item in rule_hits), 2)
        total_seconds = round(float(getattr(self, "_total_step_seconds", 0.0)), 2)
        decision_samples = list(getattr(self, "_model_decision_seconds", []) or [])
        return {
            "provider": getattr(config, "ACTIVE_PROVIDER", "unknown"),
            "model": getattr(self, "model", ""),
            # 回退驱动下任务摘要要明确标注“不是完整链路”
            "driver": driver,
            "driver_mode": getattr(config, "SCREEN_DRIVER", "auto"),
            "full_pipeline": driver == "uiautomator2",
            "driver_note": (
                "" if driver == "uiautomator2"
                else "ADB dump 回退：截图/控件树/点击可用，但本次不是完整 uiautomator2 链路"
            ),
            "steps": int(steps),
            "calls": len(events),
            "missing_usage_calls": len(events) - len(valid),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "per_step_prompt_tokens": round(prompt / denominator, 2),
            "per_step_completion_tokens": round(completion / denominator, 2),
            "per_step_total_tokens": round(total / denominator, 2),
            # 启动期连接预热耗时（不计入第一步）
            "warmup_seconds": getattr(self, "_warmup_seconds", 0.0),
            # 规则快路：命中几次、省掉几次模型调用、按规则拆分、启动时的成本核算
            "rule_fastpath": {
                "hits": len(rule_hits),
                "saved_calls": sum(int(item.get("saved_calls", 0)) for item in rule_hits),
                "by_rule": rule_by_name,
                "rule_hit_steps": len(rule_hits),
                "rule_step_seconds": rule_seconds,
                "total_step_seconds": total_seconds,
                "rule_time_ratio": (
                    round(rule_seconds / total_seconds, 3) if total_seconds else 0.0
                ),
                "cost": getattr(self, "_rule_cost", {}) or {},
            },
            # 模型步决策耗时均值：与规则步耗时对比，判断规则快路是否真的省时间
            "model_decision_seconds_mean": (
                round(sum(decision_samples) / len(decision_samples), 2)
                if decision_samples else 0.0
            ),
            "by_provider": by_provider,
            "calls_detail": events,
        }

    def _save_usage_summary(self, memory: TaskMemory) -> None:
        if self.run_dir is None:
            return
        try:
            summary = self._usage_summary(len(memory.steps))
            with open(self.run_dir / "usage_summary.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            if not summary["full_pipeline"]:
                print(f"  ⚠️  {summary['driver_note']}")
            if summary["calls"]:
                print(
                    f"  💰 tokens：prompt={summary['prompt_tokens']}, "
                    f"completion={summary['completion_tokens']}, "
                    f"total={summary['total_tokens']} "
                    f"(每步均值 {summary['per_step_total_tokens']})"
                )
        except Exception as e:
            print(f"  ⚠️  usage 汇总写入失败：{e}")

    def _archive_step(self, step: int, action: dict, observation: str):
        """把本步动作（含原始坐标）和前后截图写入 run_dir，失败不影响主流程"""
        if self.run_dir is None:
            return
        try:
            record = {"step": step, "action": action, "observation": observation,
                      "driver": getattr(self, "driver_kind", "uiautomator2"),
                      "reused_frame": bool(getattr(self, "_last_reused_frame", False)),
                      "observation_scope": getattr(self, "_last_observation_scope", "post_action")}
            if getattr(self, "_last_image_meta", None):
                # 记录本次发送图的规格与缩放比例，便于事后核对坐标
                record["image"] = self._last_image_meta
            if getattr(self, "_last_model_action", None):
                record["model_action"] = self._last_model_action
            if getattr(self, "_last_step_timings", None):
                # 每阶段耗时，便于事后定位慢在哪一层
                record["timings"] = self._last_step_timings
            # usage 缺失时明确写 null，而不是省略字段
            record["usage"] = self._summarize_usage_events(
                getattr(self, "_last_step_usage", []) or []
            )
            with open(self.run_dir / f"step_{step:03d}.json", "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            if self._last_image_meta:
                sent_path = image_preprocess.sent_filename(
                    "screen_current.png", self._last_image_meta)
                if os.path.exists(sent_path):
                    ext = os.path.splitext(sent_path)[1]
                    shutil.copy(sent_path, self.run_dir / f"step_{step:03d}_sent{ext}")
            for src, suffix in (("screen_current.png", "current"),
                                ("screen_observe.png", "observe")):
                # finish 没有执行动作，screen_observe.png 是上一步的，归档会造成证据错位
                if suffix == "observe" and (
                        action.get("action_type") == "finish"
                        or getattr(self, "_last_observation_scope", "") == "pre_action"):
                    continue
                if os.path.exists(src):
                    shutil.copy(src, self.run_dir / f"step_{step:03d}_{suffix}.png")
        except Exception as e:
            print(f"  ⚠️  证据归档失败：{e}")
