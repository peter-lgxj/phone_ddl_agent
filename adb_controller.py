"""Android 17 回退驱动：只用系统 adb 完成读屏与基础操作。

uiautomator2 在 Android 17（API 37）上第一次 ``deviceInfo`` RPC 会抛
``ApplicationSharedMemory not initialized``（openatx/uiautomator2#1209），
截至 3.7.1 仍未修复。本后端改用系统自带的 ``uiautomator dump`` 取控件树、
``screencap`` 取截图，接口与 :class:`phone_controller.PhoneController` 对齐。

只读保护在驱动层再拦一道：``type_text`` / ``press_enter`` / ``send`` /
``tap_send_button`` 在只读模式下直接抛 ``PermissionError``，即使上层漏判也不会
在群里发言或改动输入内容。
"""

from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

import config
from adb_utils import ADB

READ_ONLY_BLOCKED_METHODS = {"type_text", "press_enter", "send", "tap_send_button"}


class InputMethodUnavailable(RuntimeError):
    """中文输入所需的 ADBKeyboard 未安装或未启用。"""


class InputNotApplied(RuntimeError):
    """输入命令已发送，但输入框内容没有变化。"""


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """把 ``[x1,y1][x2,y2]`` 解析成整数四元组。"""
    match = _BOUNDS_RE.search(bounds or "")
    if not match:
        return None
    return tuple(int(value) for value in match.groups())


def bounds_center(bounds: str) -> tuple[int, int] | None:
    box = parse_bounds(bounds)
    if not box:
        return None
    x1, y1, x2, y2 = box
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def map_point(x: float, y: float, source_size: tuple[int, int],
              target_size: tuple[int, int]) -> tuple[int, int]:
    """按比例把截图坐标映射到控件树/设备坐标，并裁到目标范围内。"""
    source_w, source_h = source_size
    target_w, target_h = target_size
    if source_w <= 0 or source_h <= 0 or target_w <= 0 or target_h <= 0:
        return int(round(x)), int(round(y))
    mapped_x = int(round(float(x) * target_w / source_w))
    mapped_y = int(round(float(y) * target_h / source_h))
    return (
        min(max(mapped_x, 0), target_w - 1),
        min(max(mapped_y, 0), target_h - 1),
    )


def iter_nodes(xml_text: str):
    """解析 dump XML，逐个返回 ``node`` 元素。"""
    root = ET.fromstring(_strip_to_xml(xml_text))
    return list(root.iter("node"))


def _strip_to_xml(xml_text: str) -> str:
    text = xml_text or ""
    start = text.find("<")
    end = text.rfind(">")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def interactive_elements(xml_text: str) -> list[dict]:
    """把 dump XML 转成与 ``OCRLocator`` 一致的元素结构。

    保留有文字的可点击/可聚焦元素与所有 ``EditText``；坐标来自控件树，置信度
    固定为 1.0。
    """
    elements: list[dict] = []
    for node in iter_nodes(xml_text):
        text = (node.get("text") or "").strip()
        content_desc = (node.get("content-desc") or "").strip()
        clickable = node.get("clickable", "false") == "true"
        focusable = node.get("focusable", "false") == "true"
        node_class = (node.get("class") or "").split(".")[-1]
        label = text or content_desc
        is_input = "EditText" in node_class
        bounds = node.get("bounds") or ""
        center = bounds_center(bounds)
        if not center:
            continue
        if not (is_input or (label and (clickable or focusable))):
            continue
        elements.append({
            "text": label or f"[{node_class}]",
            "center": center,
            "bbox": parse_bounds(bounds),
            "confidence": 1.0,
            "source": "xml",
            "type": "input" if is_input else "control",
            "class": node_class,
            "resource_id": node.get("resource-id") or "",
            "bounds": bounds,
        })
    return elements


def screen_text(xml_text: str) -> str:
    """按控件树顺序提取屏幕文字，去空去重。"""
    lines: list[str] = []
    seen: set[str] = set()
    for node in iter_nodes(xml_text):
        text = (node.get("text") or "").strip()
        if not text and (node.get("content-desc") or "").strip():
            text = (node.get("content-desc") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return "\n".join(lines)


class AdbDumpController:
    """基于 ``uiautomator dump`` + ``screencap`` 的控制器。"""

    kind = "adb_dump"

    def __init__(self, locator=None, serial: str | None = None,
                 read_only: bool | None = None, probe: bool = True):
        self.locator = locator
        self.serial = (serial or os.getenv("ANDROID_SERIAL") or "").strip()
        self.read_only = (
            getattr(config, "READ_ONLY_MODE", True) if read_only is None else read_only
        )
        self.device = None  # 兼容少数按 ``phone.device`` 取字段的调用方
        self._last_dump: str | None = None
        self._last_dump_at = 0.0
        # 控件树缓存指纹：同一帧截图内复用 dump；动作后清空
        self._frame_fingerprint: str | None = None
        self._dump_fingerprint: str | None = None
        self.dump_count = 0  # 实际执行的 uiautomator dump 次数，供步级统计
        self.dump_reuse_count = 0  # 按帧指纹命中缓存、未重复 dump 的次数
        if probe:
            self._probe()

    def warmup(self) -> None:
        """启动 adb server 并确认设备在线，避免第一次 dump/screencap 付启动成本。"""
        try:
            self._run("start-server", timeout=20)
            self._run("get-state", timeout=10)
        except Exception as e:
            print(f"  ⚠️  adb 通道预热失败：{e}")

    # ------------------------------------------------------------------ #
    #  adb 基础调用
    # ------------------------------------------------------------------ #
    def _cmd(self, *args: str) -> list[str]:
        command = [ADB]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        return command

    def _run(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._cmd(*args),
            capture_output=True,
            timeout=timeout,
        )

    def _exec_out(self, *args: str, timeout: float = 30.0) -> bytes:
        result = self._run("exec-out", *args, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
                f"adb exec-out {' '.join(args)} 失败："
                f"{(result.stderr or b'').decode('utf-8', 'replace').strip()}"
            )
        return result.stdout or b""

    def _probe(self) -> None:
        try:
            result = self._run("get-state", timeout=10)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ADB 回退驱动找不到 adb：请安装 Android Platform Tools 并加入 PATH，"
                "或设置 ADB_BINARY 指向 adb 可执行文件。"
            ) from exc
        state = (result.stdout or b"").decode("utf-8", "replace").strip()
        if result.returncode != 0 or state != "device":
            raise RuntimeError(
                "ADB 回退驱动无法连接手机：请确认 `adb devices` 能看到已授权设备，"
                "并检查 ANDROID_SERIAL 是否指向正确的序列号。"
                f"（adb 返回：{state or '空'}）"
            )

    # ------------------------------------------------------------------ #
    #  读屏
    # ------------------------------------------------------------------ #
    def screenshot(self, save_path: str = "screen.png"):
        """用 ``screencap`` 截图并保存为 PNG。"""
        data = self._exec_out("screencap", "-p")
        png_start = data.find(b"\x89PNG")
        if png_start > 0:
            data = data[png_start:]
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
        image.save(save_path)
        print(f"截图已保存：{save_path}，尺寸：{image.size}（adb_dump）")
        return image

    def set_frame_fingerprint(self, fingerprint: str | None) -> None:
        """记录当前截图的指纹，供 dump 复用判断；指纹变化即视为界面变化。"""
        self._frame_fingerprint = fingerprint

    def invalidate_dump(self) -> None:
        """动作执行后调用：界面已变，旧控件树不得再复用。"""
        self._last_dump = None
        self._last_dump_at = 0.0
        self._dump_fingerprint = None
        self._frame_fingerprint = None

    def capture_with_dump(self, save_path: str = "screen.png") -> tuple[Image, str | None, dict]:
        """并行发起 ``screencap`` 与 ``uiautomator dump``，互不阻塞。

        降级：
        - dump 失败：仍返回截图，``report['dump_error']`` 记录原因，控件树为空，
          下一步需要时再单独重试（不写入缓存）；
        - 截图失败：截图是视觉决策的必需输入，串行重试一次后仍失败则抛出；
        - 关闭 ``PARALLEL_CAPTURE`` 时退回串行，行为与旧版一致。
        """
        report: dict = {"parallel": False, "errors": {}}
        if not bool(getattr(config, "PARALLEL_CAPTURE", True)):
            image = self.screenshot(save_path)
            xml = self.dump_hierarchy()
            report["parallel"] = False
            return image, xml, report

        report["parallel"] = True
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            shot_future = pool.submit(self.screenshot, save_path)
            dump_future = pool.submit(self.dump_hierarchy)
            image = None
            xml = None
            try:
                image = shot_future.result()
            except Exception as e:  # 截图失败：稍后串行重试
                report["errors"]["screenshot"] = str(e)
            try:
                xml = dump_future.result()
            except Exception as e:  # dump 失败：交回上层按需重试
                report["errors"]["dump"] = str(e)
        report["elapsed"] = round(time.perf_counter() - started, 3)

        if image is None:
            # 视觉决策必须有截图；并行失败不影响串行重试
            image = self.screenshot(save_path)
            report["screenshot_retry"] = True
        if xml is None:
            print(f"  ⚠️  并行 dump 失败，本步退回按需重试：{report['errors'].get('dump', '')}")
        return image, xml, report

    def dump_hierarchy(self, use_cache: bool = False, ttl: float = 0.5) -> str:
        """用系统 ``uiautomator dump`` 读取控件树 XML。

        复用条件：同一帧指纹（``set_frame_fingerprint``）或短 TTL 内。
        任一动作（tap/swipe/back/home/type/open_app）都会清空缓存。
        """
        now = time.monotonic()
        if use_cache and self._last_dump:
            same_frame = (
                self._dump_fingerprint is not None
                and self._dump_fingerprint == self._frame_fingerprint
            )
            fresh = self._dump_fingerprint is None and now - self._last_dump_at <= ttl
            if same_frame or fresh:
                self.dump_reuse_count += 1
                return self._last_dump
        # 每次用唯一路径：dump 失败时绝不能回落到上一次遗留的 window_dump.xml，
        # 否则会把上一轮界面当成当前界面喂给规则快路。
        remote = f"/sdcard/window_dump_{time.time_ns()}.xml"
        last_error = ""
        for attempt in (1, 2):
            result = self._run("shell", "uiautomator", "dump", remote, timeout=30)
            if result.returncode == 0:
                break
            last_error = (
                (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
            )
            print(f"  ⚠️  uiautomator dump 返回非零（第 {attempt} 次）：{last_error}")
            if attempt == 1:
                time.sleep(0.3)
        else:
            raise RuntimeError(
                "adb_dump 无法读取控件树：`uiautomator dump` 连续两次返回非零"
                f"（{last_error or '无错误输出'}）。"
                "请确认系统自带的 uiautomator 可用（`adb shell uiautomator dump`）。"
            )
        try:
            try:
                data = self._exec_out("cat", remote)
            except RuntimeError:
                data = self._run("shell", "cat", remote, timeout=30).stdout or b""
            xml_text = data.decode("utf-8", "replace")
            if "<hierarchy" not in xml_text and "<node" not in xml_text:
                raise RuntimeError(
                    "adb_dump 无法读取控件树：`uiautomator dump` 没有返回 XML。"
                    "请确认系统自带的 uiautomator 可用（`adb shell uiautomator dump`）。"
                )
        finally:
            # 清理本次临时文件；失败也不影响已读到的内容
            self._run("shell", "rm", "-f", remote, timeout=10)
        self._last_dump = xml_text
        self._last_dump_at = now
        self._dump_fingerprint = self._frame_fingerprint
        self.dump_count += 1
        return xml_text

    def screen_text(self) -> str:
        """返回当前界面的可见文字（按控件树顺序）。"""
        return screen_text(self.dump_hierarchy(use_cache=True))

    def tree_elements(self) -> list[dict]:
        return interactive_elements(self.dump_hierarchy(use_cache=True))

    # ------------------------------------------------------------------ #
    #  操作
    # ------------------------------------------------------------------ #
    def _ensure_writable(self, method: str) -> None:
        if self.read_only and method in READ_ONLY_BLOCKED_METHODS:
            raise PermissionError(
                f"只读模式拒绝动作「{method}」：读屏任务不允许发言或改动输入内容"
            )

    def tap(self, x, y):
        self.invalidate_dump()
        self._run("shell", "input", "tap", str(int(x)), str(int(y)))
        print(f"点击：({int(x)}, {int(y)})（adb_dump）")
        time.sleep(0.8)

    def swipe(self, x1, y1, x2, y2, duration=0.3):
        self.invalidate_dump()
        self._run(
            "shell", "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
            str(int(float(duration) * 1000)),
        )
        print(f"滑动：({x1},{y1}) → ({x2},{y2})（adb_dump）")
        time.sleep(0.8)

    def long_tap(self, x, y, duration=1.0):
        self.swipe(x, y, x, y, duration=duration)
        print(f"长按：({x}, {y})（adb_dump）")

    def press_back(self):
        self.invalidate_dump()
        self._run("shell", "input", "keyevent", "4")
        print("按下返回键（adb_dump）")
        time.sleep(0.5)

    def press_home(self):
        self.invalidate_dump()
        self._run("shell", "input", "keyevent", "3")
        print("按下 Home 键（adb_dump）")
        time.sleep(0.5)

    def press_enter(self):
        self._ensure_writable("press_enter")
        self._run("shell", "input", "keyevent", "66")
        print("按下回车键（adb_dump）")
        time.sleep(0.5)

    def send(self):
        self._ensure_writable("send")
        self.press_enter()

    @staticmethod
    def _has_non_ascii(text: str) -> bool:
        return any(ord(char) > 127 for char in str(text))

    def _ensure_adb_keyboard(self) -> None:
        """确认 ADBKeyboard 已安装并设为当前输入法，否则给出可执行的失败原因。"""
        try:
            listed = self._run("shell", "ime", "list", "-s", timeout=15)
            default = self._run(
                "shell", "settings", "get", "secure", "default_input_method",
                timeout=15,
            )
        except Exception as exc:
            raise InputMethodUnavailable(
                f"中文输入需要 ADBKeyboard，但查询输入法失败：{exc}；"
                "请确认 adb shell 可用，或改用列表滚动定位"
            ) from exc
        installed = "com.android.adbkeyboard" in (
            (listed.stdout or b"").decode("utf-8", "replace").lower()
        )
        enabled = "adbkeyboard" in (
            (default.stdout or b"").decode("utf-8", "replace").lower()
        )
        if enabled:
            return
        if installed:
            raise InputMethodUnavailable(
                "已安装 ADBKeyboard 但未设为当前输入法：请执行 "
                "`adb shell ime enable com.android.adbkeyboard/.AdbIME` 和 "
                "`adb shell ime set com.android.adbkeyboard/.AdbIME`；"
                "或改用列表滚动定位"
            )
        raise InputMethodUnavailable(
            "中文输入需要安装 ADBKeyboard（com.android.adbkeyboard）并设为当前输入法；"
            "未安装时请改用列表滚动定位，不要重复输入中文"
        )

    def _input_texts(self) -> list[str]:
        """读取当前控件树里所有输入框的实际文字。"""
        xml_text = self.dump_hierarchy(use_cache=False)
        values: list[str] = []
        for node in iter_nodes(xml_text):
            node_class = (node.get("class") or "").split(".")[-1]
            if "EditText" in node_class:
                values.append((node.get("text") or "").strip())
        if not any(values):
            # WebView/自绘搜索框不一定把文字写在 EditText.text 上，
            # 没有可用输入框文字时退化为全屏文字检查。
            values = [screen_text(xml_text)]
        return values

    def _input_contains(self, text: str) -> bool:
        needle = str(text).strip()
        if not needle:
            return True
        try:
            values = self._input_texts()
        except Exception as exc:
            print(f"  ⚠️  输入校验读取控件树失败：{exc}")
            return False
        return any(needle in value for value in values if value)

    def _type_non_ascii(self, text: str) -> None:
        """非 ASCII 输入：ADBKeyboard 的 base64 广播 + 控件树校验。

        ``adb shell input text`` 只可靠支持 ASCII；中文必须由 ADBKeyboard
        接收广播后写入当前聚焦输入框。base64 避免命令行/shell 编码问题。
        """
        self._ensure_adb_keyboard()
        payload = base64.b64encode(str(text).encode("utf-8")).decode("ascii")
        result = self._run(
            "shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
            "--es", "msg", payload, timeout=20,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise InputNotApplied(
                f"ADBKeyboard 广播失败（returncode={result.returncode}）："
                f"{stderr or '无输出'}；请改用列表滚动定位"
            )
        time.sleep(0.5)
        if not self._input_contains(text):
            raise InputNotApplied(
                f"ADBKeyboard 已接收「{text}」但输入框内容未变化；"
                "不要重复相同输入，改用列表滚动定位"
            )
        print(f"输入文字：{text}（ADBKeyboard 广播）")

    def type_text(self, text: str, allow_search: bool = False):
        # 允许搜索框输入：不经过 _ensure_writable，但调用方必须证明是搜索框
        if not allow_search:
            self._ensure_writable("type_text")
        if not text:
            return
        self.invalidate_dump()
        if self._has_non_ascii(text):
            self._type_non_ascii(text)
            time.sleep(0.5)
            return
        escaped = str(text).replace(" ", "%s")
        self._run("shell", "input", "text", escaped)
        print(f"输入文字：{text}（adb_dump）")
        time.sleep(0.5)

    def tap_input_box(self) -> bool:
        """从控件树找 EditText，点击其中心。"""
        elements = self.tree_elements()
        for element in elements:
            if element.get("type") == "input":
                x, y = element["center"]
                self.tap(x, y)
                print("点击输入框（adb_dump）")
                return True
        print("未在控件树中找到输入框（adb_dump）")
        return False

    def open_app(self, package_name: str):
        self.invalidate_dump()
        self._run(
            "shell", "monkey", "-p", package_name,
            "-c", "android.intent.category.LAUNCHER", "1",
        )
        print(f"启动 App：{package_name}（adb_dump）")
        time.sleep(2.5)

    def is_keyboard_shown(self) -> bool:
        result = self._run("shell", "dumpsys", "input_method")
        stdout = (result.stdout or b"").decode("utf-8", "ignore")
        return "mInputShown=true" in stdout

    def tap_send_button(self):
        self._ensure_writable("tap_send_button")
        for element in self.tree_elements():
            if element.get("text") in ("Send", "发送"):
                x, y = element["center"]
                self.tap(x, y)
                print(f"点击发送按钮：{element['text']}（adb_dump）")
                return
        self.press_enter()
