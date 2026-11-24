"""规则快路：控件树能证明的动作不调用视觉模型。

只覆盖四类可被控件树或系统约定证明的动作：

- ``open_group``：任务里用引号点名的群/会话，在控件树里有唯一可点击行时直接点开；
- ``back_after_miss``：上一步目标未定位时先返回上一级，避免在错误界面继续猜；
- ``scroll``：已进入目标会话后按固定手势向上翻，直到文字不再变化或达到上限；
- ``search``：目标会话不在当前屏可见行时，点搜索入口 → 在搜索框输入精确名称
  → 点唯一结果，避免在列表里反复滑动；
- ``tap_input``：已知 resource-id / EditText 的输入框直接点，不需要视觉判断。

搜索框输入是唯一允许的 ``type``：它只改搜索框，不产生外发；消息输入框的
``type`` / ``send`` / ``press_enter`` 仍然拒绝。判断依据是控件 resource-id、
class 与提示文本（``搜索``/``搜索指定内容``、id 含 search/kbs），不看坐标。
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# 任务里用「」、“”、"" 或 '' 点名的目标，例如「COMP7503 作业通知群」
_TARGET_QUOTED_RE = re.compile(r"「([^」]{2,40})」")
_QUOTED_RE = re.compile(r"[「“\"]([^」”\"]{2,40})[」”\"]|‘([^’]{2,40})’")

# 界面已经翻到底/顶的提示词；命中后停止规则滚动，交回模型做语义判断
_END_MARKERS = ("没有更多", "已加载全部", "查看更多消息", "没有更多消息")

# 单次上滑的手指起点与位移（相对屏幕高度）：起点 0.82、位移 0.62，比原来的
# 0.75→0.30（位移 0.45）多读约 38% 的屏幕内容，同样的 3 天记录少滑数次。
SCROLL_START_RATIO = 0.82
SCROLL_TRAVEL_RATIO = 0.62

# 会话页标题栏区域：屏幕高度 12% 以内；标题左侧通常是返回键，右侧是群设置
_CHAT_TITLE_ZONE_RATIO = 0.12
_CHAT_CHROME = {
    "返回", "返回消息", "返回上一级", "消息", "消息列表", "QQ", "关闭", "更多",
    "聊天设置", "听筒模式", "发送", "语音", "相册", "拍照", "表情", "更多功能",
    "搜索", "群聊", "联系人", "动态",
}
_CHAT_COUNT_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")

# App 冷启动/恢复时可能停在搜索页或上一次的会话页；这两类页面必须先退回
# 消息列表，否则搜索入口和列表滚动都会在错误页面上空转。
_SEARCH_PAGE_MARKERS = ("搜索综合结果", "AI搜索", "搜索指定内容")

# 任务答案关键词：当前屏幕已经出现这些内容时，继续规则滚动没有收益。
# 「作业通知群」这类群名不会被 _HOMEWORK_RE 命中（作业后面不是序号），
# 避免标题栏里的群名把滚动永远卡在第 0 步。
_HOMEWORK_RE = re.compile(
    r"(?:实验报告|作业|报告|任务|项目|论文|实验|lab|hw|quiz)\s*[一二三四五六七八九十0-9]+",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\d{1,4}[./月-]\d{1,2}(?:[./月-]\d{1,2})?")
_DEADLINE_MARKERS = ("截止", "ddl", "deadline")

# 规则动作黑名单：即使配置或调用方出错，也不可能从规则快路发出写操作
_MUTATING_ACTIONS = {"send", "press_enter", "type"}

# 搜索输入判据：class/resource-id/提示文本，任一命中即可，不依赖坐标
_SEARCH_HINT_RE = re.compile(r"搜索|search", re.IGNORECASE)
_SEARCH_ID_HINTS = ("search", "kbs")

# 发送按钮文案；规则任何 tap 落在这些节点区域或文案上都必须拒绝
_SEND_LABELS = {"发送", "send", "发送消息", "send message"}


@dataclass
class RuleContext:
    task: str
    step: int
    read_only: bool
    xml: str
    last_action: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    max_scrolls: int = 30
    known_input_resource_ids: tuple[str, ...] = ()
    screen_size: tuple[int, int] | None = None
    # adb_dump 的 input text 只支持 ASCII；为 True 时用名称里的 ASCII 唯一片段搜索
    search_ascii_only: bool = False
    # 实验开关：目标在可见行时也强制走搜索，用于对比搜索/列表两条路径
    search_force: bool = False
    # 是否允许走搜索；搜索优先，只有调用方显式关闭时才直接滚动
    search_allowed: bool = True
    # 搜索不可用/未命中时的滚动兜底；滚动过程只滑不点，绝不点非精确群
    list_fallback: bool = True
    list_max_scrolls: int = 8


def extract_quoted_targets(task: str) -> list[str]:
    """取出任务里被引号点名的目标，保持出现顺序并去重。

    任务文案里可能同时出现界面路径（如“联系人 > 群聊”）和目标群名；「」是
    我们约定给目标群用的括号，优先只取它，避免把界面路径当成群名去搜索。
    """
    task = task or ""
    primary = [m.group(1).strip() for m in _TARGET_QUOTED_RE.finditer(task)]
    primary = [value for value in primary
               if value and not any(sep in value for sep in (">", "/", "->"))]
    if primary:
        return list(dict.fromkeys(primary))
    targets: list[str] = []
    for match in _QUOTED_RE.finditer(task):
        value = (match.group(1) or match.group(2) or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _strip_to_xml(xml_text: str) -> str:
    text = xml_text or ""
    start = text.find("<")
    end = text.rfind(">")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def parse_xml(xml_text: str):
    """解析 dump XML；解析失败返回 None，调用方按未命中处理。"""
    try:
        return ET.fromstring(_strip_to_xml(xml_text))
    except (ET.ParseError, ValueError):
        return None


def _bounds(value: str) -> tuple[int, int, int, int] | None:
    numbers = re.findall(r"-?\d+", value or "")
    if len(numbers) != 4:
        return None
    return tuple(int(item) for item in numbers)


def _center(bounds: str) -> tuple[int, int] | None:
    box = _bounds(bounds)
    if not box:
        return None
    x1, y1, x2, y2 = box
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def root_size(xml_text: str) -> tuple[int, int] | None:
    """从根节点 bounds 读取屏幕尺寸，供规则手势使用。"""
    root = parse_xml(xml_text)
    if root is None:
        return None
    for node in root.iter("node"):
        box = _bounds(node.get("bounds") or "")
        if box and box[2] > box[0] and box[3] > box[1]:
            return box[2] - box[0], box[3] - box[1]
    return None


def screen_text(xml_text: str, limit: int = 200) -> str:
    """按控件树顺序提取可见文字，去空去重。"""
    root = parse_xml(xml_text)
    if root is None:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if not text:
            text = (node.get("content-desc") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def task_keywords(task: str) -> list[str]:
    """从任务里抽取“答案关键词”：作业名、截止标记、目标日期。

    引号目标是导航对象（群名/会话名），不参与滚动停止判断——它本身就出现在
    标题栏，算进去会让规则永远认为“已经命中”。
    """
    text = task or ""
    words: list[str] = []
    for match in _HOMEWORK_RE.finditer(text):
        value = _compact(match.group(0))
        if value and value not in words:
            words.append(value)
    lowered = text.lower()
    for marker in _DEADLINE_MARKERS:
        if marker in lowered and marker not in words:
            words.append(marker)
    for match in _DATE_RE.finditer(text):
        value = match.group(0)
        if value not in words:
            words.append(value)
    return words


def screen_has_task_keyword(xml_text: str, task: str,
                            exclude: tuple[str, ...] = ()) -> str:
    """当前控件树文字里是否已出现任务关键词；返回命中的关键词，空串表示未命中。"""
    text = _compact(screen_text(xml_text))
    if not text:
        return ""
    skip = {_compact(item) for item in exclude if item}
    for word in task_keywords(task):
        if word and word not in skip and word in text:
            return word
    return ""


def _in_title_bar(row: dict, screen_height: int) -> bool:
    """命中行是否落在顶部标题栏区域。

    会话页标题文字与任务点名的群名完全一致，且可点击；若把它当群列表行点击，
    会停在原页面空转一步。标题栏高度按屏幕 12% 估算（1080x2374 下约 285px）。
    """
    if screen_height <= 0:
        return False
    return row["center"][1] < screen_height * 0.12


def find_send_bounds(xml_text: str) -> list[tuple[int, int, int, int]]:
    """控件树里发送按钮的区域；文案或 resource-id 命中都算。"""
    root = parse_xml(xml_text)
    if root is None:
        return []
    boxes: list[tuple[int, int, int, int]] = []
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        text = _compact(node.get("text") or node.get("content-desc")).lower()
        resource_id = (node.get("resource-id") or "").lower()
        if text not in _SEND_LABELS and not resource_id.endswith("send_btn"):
            continue
        box = _bounds(node.get("bounds") or "")
        if box:
            boxes.append(box)
    return boxes


def tap_hits_send(xml_text: str, x: int, y: int, target_text: str = "") -> str:
    """规则 tap 是否碰到发送按钮；返回命中原因，空串表示安全。"""
    label = _compact(target_text).lower()
    if label in _SEND_LABELS or "发送" in label:
        return "target_text"
    for x1, y1, x2, y2 in find_send_bounds(xml_text):
        if x1 <= x <= x2 and y1 <= y <= y2:
            return "coords"
    return ""


def _nearest_clickable(node, parents: dict) -> object | None:
    current = node
    while current is not None:
        if current.get("clickable") == "true":
            return current
        current = parents.get(current)
    return None


def _collect_matches(root, needle: str) -> tuple[list[dict], list[dict]]:
    """返回 (完全匹配, 包含匹配) 的可点击行；两种都按可点击祖先去重。"""
    parents = {child: parent for parent in root.iter() for child in parent}
    exact: list[dict] = []
    partial: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if not text:
            text = (node.get("content-desc") or "").strip()
        if not text:
            continue
        if _compact(text) == _compact(needle):
            bucket = exact
        elif _compact(needle) in _compact(text):
            bucket = partial
        else:
            continue
        clickable = _nearest_clickable(node, parents)
        bounds = (clickable if clickable is not None else node).get("bounds") or ""
        key = (bounds, text)
        if key in seen:
            continue
        seen.add(key)
        center = _center(bounds)
        if not center:
            continue
        bucket.append({
            "text": text,
            "center": center,
            "bounds": bounds,
            "clickable": clickable is not None,
        })
    return exact, partial


def find_target_rows(xml_text: str, needle: str) -> list[dict]:
    """返回所有不同的可点击匹配行（完全匹配在前，包含匹配在后）。"""
    root = parse_xml(xml_text)
    if root is None:
        return []
    exact, partial = _collect_matches(root, needle)
    rows: list[dict] = []
    seen: set[str] = set()
    for item in exact + partial:
        if not item["clickable"] or item["bounds"] in seen:
            continue
        seen.add(item["bounds"])
        rows.append(item)
    return rows


_ROW_TITLE_SEP_RE = re.compile(r"[,，\u3000]")


def row_title(text: str) -> str:
    """会话行的标题部分。

    QQ 的 adb dump 会把「群名、未读数、最后一条预览、时间」拼进同一个节点文本，
    标题是第一个分隔符之前的部分；微信等有独立标题节点时原样返回。
    """
    return _ROW_TITLE_SEP_RE.split(text or "", maxsplit=1)[0].strip()


_UNREAD_SUFFIX_RE = re.compile(
    r"^(?:[(（\[【]\s*\d+\s*[)）\]】]|\s+\d+\s*条未读.*)$"
)


def _unread_suffix_ok(rest: str) -> bool:
    """标题后只允许「未读数」这类带括号的后缀，避免把「llm小组2」当同一个群。"""
    return bool(_UNREAD_SUFFIX_RE.match(rest.strip()))


def label_matches_target(label: str, target: str) -> bool:
    """会话行标题是否逐字符等于目标群名（大小写敏感，只放行未读数后缀）。"""
    wanted = _compact(target)
    if not wanted:
        return False
    title = row_title(label)
    if _compact(title) == wanted:
        return True
    name = target.strip()
    if title.startswith(name):
        return _unread_suffix_ok(title[len(name):])
    return False


def chat_title(xml_text: str) -> str:
    """当前会话页标题栏里的群名/会话名；无法判定时返回空串。

    进入会话后用它做一次硬校验：只有标题逐字符等于任务点名的目标群，才允许把
    这一屏的观察算进该来源。返回键、群设置、人数后缀、顶部系统提示都不算标题。
    """
    root = parse_xml(xml_text)
    if root is None:
        return ""
    size = root_size(xml_text)
    height = size[1] if size else 2374
    best = ""
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if not text:
            continue
        text = _CHAT_COUNT_RE.sub("", text).strip()
        if not text or text in _CHAT_CHROME:
            continue
        if _CHAT_COUNT_RE.fullmatch(text) or not re.search(r"[\w\u4e00-\u9fff]", text):
            continue
        center = _center(node.get("bounds") or "")
        if not center or center[1] >= height * _CHAT_TITLE_ZONE_RATIO:
            continue
        # 顶部系统提示（改名/邀请/入群横幅）不是标题：带引号且很长的句子排除
        if len(text) > 20:
            continue
        if len(text) > len(best):
            best = text
    return best


def find_exact_target_rows(xml_text: str, needle: str) -> list[dict]:
    """只返回标题逐字符一致的会话行；近似名、大小写变体一律不返回。

    这是「不得点开非目标群」的执行点：滚动/首屏定位只允许从这里拿坐标。
    """
    root = parse_xml(xml_text)
    if root is None:
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    rows: list[dict] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text or not label_matches_target(text, needle):
            continue
        clickable = _nearest_clickable(node, parents)
        if clickable is None:
            continue
        bounds = clickable.get("bounds") or ""
        center = _center(bounds)
        if not center or bounds in seen:
            continue
        seen.add(bounds)
        rows.append({"text": text, "center": center, "bounds": bounds, "clickable": True})
    return rows


def find_similar_rows(xml_text: str, needle: str, threshold: float = 0.82) -> list[dict]:
    """搜索结果里与目标名高度相似的候选行。

    「2026 计算机班级群」与「2026 计算机班级二群」不是子串关系，靠相似度识别；
    相似候选多于一个时规则不点，交回模型判断。
    """
    root = parse_xml(xml_text)
    if root is None:
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    wanted = _compact(needle)
    rows: list[dict] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text:
            continue
        compacted = _compact(text)
        if compacted == wanted:
            score = 1.0
        elif wanted in compacted:
            score = 0.95
        else:
            score = difflib.SequenceMatcher(None, wanted, compacted).ratio()
        if score < threshold:
            continue
        clickable = _nearest_clickable(node, parents)
        if clickable is None:
            continue
        bounds = clickable.get("bounds") or ""
        center = _center(bounds)
        if not center or bounds in seen:
            continue
        seen.add(bounds)
        rows.append({"text": text, "center": center, "bounds": bounds, "clickable": True})
    return rows


class AmbiguousGroupName(RuntimeError):
    """搜索/列表里没有逐字符一致的群名，只剩大小写差异或近似名。"""

    def __init__(self, target: str, candidates: list[str]):
        self.target = target
        self.candidates = candidates
        listing = "、".join(candidates[:6]) or "（无可展示名称）"
        super().__init__(
            f"群名「{target}」没有逐字符一致的匹配，只有近似候选：{listing}。"
            "已跳过该来源，请选定正确群名后重扫。"
        )


def find_exact_rows(xml_text: str, needle: str) -> list[dict]:
    """只返回逐字符一致（忽略空白/标点、区分大小写）的可点击行。"""
    root = parse_xml(xml_text)
    if root is None:
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    wanted = _compact(needle)
    rows: list[dict] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text or _compact(text) != wanted:
            continue
        clickable = _nearest_clickable(node, parents)
        if clickable is None:
            continue
        bounds = clickable.get("bounds") or ""
        center = _center(bounds)
        if not center or bounds in seen:
            continue
        seen.add(bounds)
        rows.append({"text": text, "center": center, "bounds": bounds, "clickable": True})
    return rows


def find_case_variant_rows(xml_text: str, needle: str) -> list[dict]:
    """仅大小写不同的候选行；用于没有完全一致候选时列出歧义候选。"""
    root = parse_xml(xml_text)
    if root is None:
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    wanted = _compact(needle).casefold()
    rows: list[dict] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text:
            continue
        compacted = _compact(text)
        if compacted == _compact(needle) or compacted.casefold() != wanted:
            continue
        clickable = _nearest_clickable(node, parents)
        if clickable is None:
            continue
        bounds = clickable.get("bounds") or ""
        center = _center(bounds)
        if not center or bounds in seen:
            continue
        seen.add(bounds)
        rows.append({"text": text, "center": center, "bounds": bounds, "clickable": True})
    return rows


def ambiguous_candidates(xml_text: str, needle: str) -> list[str]:
    """没有逐字符一致候选时，列出大小写不同/近似的候选群名。"""
    labels: list[str] = []
    rows = find_case_variant_rows(xml_text, needle) + find_similar_rows(xml_text, needle)
    for row in rows:
        text = str(row.get("text") or "").strip()
        if text and text not in labels:
            labels.append(text)
    return labels[:6]


def find_input(xml_text: str, known_resource_ids: tuple[str, ...] = ()) -> dict | None:
    """找已知 resource-id 的输入框；没有已知 id 时退回 EditText。"""
    root = parse_xml(xml_text)
    if root is None:
        return None
    edit_text: dict | None = None
    known = {item for item in known_resource_ids if item}
    for node in root.iter("node"):
        resource_id = node.get("resource-id") or ""
        node_class = (node.get("class") or "").split(".")[-1]
        is_input = "EditText" in node_class or resource_id in known
        if not is_input:
            continue
        center = _center(node.get("bounds") or "")
        if not center:
            continue
        item = {
            "text": (node.get("text") or node.get("content-desc") or "").strip(),
            "center": center,
            "resource_id": resource_id,
        }
        if resource_id in known:
            return item
        if edit_text is None:
            edit_text = item
    return edit_text


def _ascii_token(text: str) -> str:
    """取名称里最长的 ASCII 片段（长度 >=4），用于 adb_dump 无法输入中文时的搜索词。"""
    tokens = re.findall(r"[A-Za-z0-9]{4,}", text or "")
    return max(tokens, key=len) if tokens else ""


def find_search_entry(xml_text: str) -> dict | None:
    """群/会话列表顶部的搜索入口；按文案「搜索」+ 可点击 + 屏幕上半部定位。"""
    root = parse_xml(xml_text)
    if root is None:
        return None
    size = root_size(xml_text)
    height = size[1] if size else 2374
    for node in root.iter("node"):
        text = _compact(node.get("text") or node.get("content-desc")).lower()
        if text != "搜索" or node.get("clickable") != "true":
            continue
        center = _center(node.get("bounds") or "")
        if center and center[1] < height * 0.4:
            return {
                "text": "搜索",
                "center": center,
                "bounds": node.get("bounds") or "",
                "resource_id": node.get("resource-id") or "",
            }
    return None


def find_search_input(xml_text: str) -> dict | None:
    """当前页面的搜索输入框。

    判据（任一命中，不看坐标猜）：
    - class 含 EditText 且 resource-id/提示文本含 search/搜索；
    - TextView 的 resource-id 含 search/kbs 且提示文本是「搜索/搜索指定内容」。
    消息输入框（底部、resource-id 含 input、文案含发送）一律排除。
    """
    root = parse_xml(xml_text)
    if root is None:
        return None
    size = root_size(xml_text)
    height = size[1] if size else 2374
    for node in root.iter("node"):
        text = _compact(node.get("text") or node.get("content-desc"))
        resource_id = (node.get("resource-id") or "").lower()
        node_class = node.get("class") or ""
        center = _center(node.get("bounds") or "")
        if not center or center[1] > height * 0.5:
            continue
        if "发送" in text or resource_id.endswith(":id/input"):
            continue
        is_edit = "EditText" in node_class
        looks_search = (
            (is_edit and bool(_SEARCH_HINT_RE.search(text + resource_id)))
            or (any(hint in resource_id for hint in _SEARCH_ID_HINTS)
                and "搜索" in text)
        )
        if not looks_search:
            continue
        return {
            "text": text,
            "center": center,
            "bounds": node.get("bounds") or "",
            "resource_id": node.get("resource-id") or "",
        }
    return None


def is_search_input(xml_text: str, resource_id: str = "") -> bool:
    """当前界面是否仍是同一个搜索框；用于执行前的只读判定。"""
    box = find_search_input(xml_text)
    if box is None:
        return False
    return not resource_id or box["resource_id"] == resource_id


def _action(action_type: str, rule: str, description: str, **extra) -> dict:
    payload = {
        "action_type": action_type,
        "description": description,
        "target": "",
        "thought": description,
        "rule": {"name": rule},
    }
    payload.update(extra)
    # 只允许搜索框输入；消息输入框的 type 与 send/press_enter 仍然禁止
    is_search_type = action_type == "type" and bool(extra.get("search_input"))
    if payload["action_type"] in _MUTATING_ACTIONS and not is_search_type:
        raise ValueError(f"规则快路不得产生写动作：{payload['action_type']}")
    return payload


def _tap_action(ctx: RuleContext, rule: str, description: str,
                x: int, y: int, target: str, **extra) -> dict | None:
    """构造规则 tap；落在发送按钮文案或坐标区域时拒绝并记录原因。"""
    reason = tap_hits_send(ctx.xml, x, y, target)
    if reason:
        ctx.state["rejected_tap"] = reason
        return None
    return _action("tap", rule, description, x=x, y=y, target=target, **extra)


def _list_scroll_action(ctx: RuleContext, state: dict) -> dict | None:
    """列表定位兜底：目标不在可见行时向上滚动，直到上限或连续两屏不变。"""
    if state.get("list_disabled"):
        return None
    text = screen_text(ctx.xml)
    scrolls = state.get("list_scrolls", 0)
    if scrolls >= ctx.list_max_scrolls:
        state["list_disabled"] = True
        state["list_failed"] = "max_scrolls"
        return None
    if text:
        if text == state.get("list_last_text"):
            state["list_stagnant"] = state.get("list_stagnant", 0) + 1
            if state["list_stagnant"] >= 2:
                state["list_disabled"] = True
                state["list_failed"] = "stagnant"
                return None
        else:
            state["list_stagnant"] = 0
            state["list_last_text"] = text
    state["list_scrolls"] = scrolls + 1
    width, height = ctx.screen_size or root_size(ctx.xml) or (1080, 2374)
    return _action(
        "swipe", "list_scroll", "规则快路：目标不在可见行，向上滚动群/会话列表",
        x=width // 2, y=int(height * SCROLL_START_RATIO),
        x2=width // 2, y2=int(height * (SCROLL_START_RATIO - SCROLL_TRAVEL_RATIO)),
    )


def _stale_page_action(ctx: RuleContext, targets: list[str]) -> dict | None:
    """处理「打开 App 后不是消息列表」的残留页面。

    - 搜索页：退回消息列表，重新走搜索；
    - 其他会话页：退回消息列表；
    - 恰好是目标群：补记 entered_group 并等待一帧，下一步继续上滑读消息。
    """
    text = screen_text(ctx.xml)
    if any(marker in text for marker in _SEARCH_PAGE_MARKERS):
        ctx.state["search_stage"] = None
        return _action("back", "leave_search",
                       "规则快路：当前停在搜索页，先返回消息列表")
    title = chat_title(ctx.xml)
    if not title:
        return None
    if any(label_matches_target(title, target) for target in targets):
        ctx.state["entered_group"] = title
        ctx.state["scrolls"] = 0
        ctx.state["last_scroll_text"] = ""
        ctx.state["stagnant"] = 0
        return _action("wait", "confirm_group",
                       f"规则快路：当前已在目标群「{title}」，继续读取")
    return _action("back", "leave_stale_chat",
                   f"规则快路：当前停在「{title}」，先返回消息列表")


def decide(ctx: RuleContext) -> dict | None:
    """按优先级判断本步能否走规则；返回动作或 None（交回 VLM）。"""
    state = ctx.state
    if state.get("disabled"):
        return None
    last_action = ctx.last_action or {}

    # 1) 上一步目标未定位：先返回上一级，最多两次，避免在错误界面反复猜
    if last_action.get("localization_failed") and state.get("back_count", 0) < 2:
        state["back_count"] = state.get("back_count", 0) + 1
        state["entered_group"] = None
        return _action("back", "back_after_miss", "规则快路：上一步目标未定位，返回上一级")

    targets = extract_quoted_targets(ctx.task)

    # 1b) 冷启动/重启后可能停在搜索页，或恢复成上一次的会话页：先退回消息列表。
    #     已经停在目标群时补记 entered_group，避免把标题当列表行再点一次。
    #     已处于搜索流程中时不判残留，否则会把搜索落地页当成残留页反复返回。
    if not state.get("entered_group") and not state.get("search_stage"):
        stale = _stale_page_action(ctx, targets)
        if stale is not None:
            return stale

    # 2a) 搜索流程：点搜索入口 → 输入精确名称 → 点唯一结果。
    #     首屏没有逐字符一致的群名、或显式强制搜索时才启用。
    stage = state.get("search_stage")
    if targets and stage:
        target = targets[0]
        if stage == "entry":
            box = find_search_input(ctx.xml)
            if box is None:
                state["search_stage"] = None
                state["search_failed"] = "no_input"
                # 搜索入口没打开搜索框：仍在列表时交给下面的滚动兜底
                if not ctx.list_fallback or find_search_entry(ctx.xml) is None:
                    return None
            else:
                state["search_stage"] = "result"
                query = target
                if ctx.search_ascii_only:
                    token = _ascii_token(target)
                    if not token and not target.isascii():
                        state["search_stage"] = None
                        state["search_failed"] = "non_ascii_input"
                        return None
                    query = token or target
                return _action(
                    "type", "search_type", f"规则快路：搜索框输入「{query}」",
                    target=target, text=query, search_query=query, search_input=True,
                    resource_id=box["resource_id"],
                )
        else:
            rows: list[dict] = []
            seen_bounds: set[str] = set()
            for item in targets:
                # 搜索结果只接受逐字符一致的候选；大小写不同或近似的名字一律不点，
                # 否则「LLM小组」的搜索会进到「llm小组」。
                for row in find_exact_rows(ctx.xml, item):
                    if row["bounds"] in seen_bounds:
                        continue
                    seen_bounds.add(row["bounds"])
                    rows.append(row)
            size = root_size(ctx.xml)
            height = size[1] if size else 0
            state["search_stage"] = None
            if len(rows) == 1 and not _in_title_bar(rows[0], height):
                row = rows[0]
                x, y = row["center"]
                label = row["text"]
                action = _tap_action(
                    ctx, "search_result", f"规则快路：打开搜索结果「{label}」", x, y, label)
                if action is None:
                    return None
                state["entered_group"] = label
                state["scrolls"] = 0
                state["last_scroll_text"] = ""
                state["stagnant"] = 0
                return action
            if not rows:
                # 没有任何逐字符一致的候选：只有大小写差异/近似名时标记歧义，
                # 不自动挑一个，也不让整轮失败（由上层汇总后交给人选定）。
                labels = ambiguous_candidates(ctx.xml, targets[0])
                if labels:
                    raise AmbiguousGroupName(targets[0], labels)
            state["search_failed"] = "ambiguous" if rows else "no_result"
            if state.get("search_backs", 0) < 1:
                # 搜索页没有可用结果：退回列表后改走滚动兜底，不在这里点任何行
                state["search_backs"] = state.get("search_backs", 0) + 1
                return _action(
                    "back", "search_back",
                    "规则快路：搜索没有精确结果，返回列表后滚动兜底")
            return None

    # 2b) 开群：首屏逐字符一致的群名直接进；没有就搜索；搜索不可用才滚动兜底。
    #     滚动只滑动、不点任何行，绝不点开名字相似或只有大小写不同的群。
    #     已经进群（state）、或页面已有输入框（说明已在会话/详情页）时一律不点：
    #     会话页标题栏文字与群名完全相同且可点击，点它等于原地空转一步。
    if (targets and not state.get("entered_group")
            and find_input(ctx.xml) is None):
        rows: list[tuple[str, dict]] = []
        seen_bounds: set[str] = set()
        for target in targets:
            for row in find_exact_target_rows(ctx.xml, target):
                if row["bounds"] in seen_bounds:
                    continue
                seen_bounds.add(row["bounds"])
                rows.append((target, row))
        size = root_size(ctx.xml)
        height = size[1] if size else 0
        if len(rows) == 1 and not ctx.search_force and not _in_title_bar(rows[0][1], height):
            target, row = rows[0]
            x, y = row["center"]
            action = _tap_action(
                ctx, "open_group", f"规则快路：打开「{target}」", x, y, target)
            if action is None:
                return None
            state["entered_group"] = target
            state["scrolls"] = 0
            state["last_scroll_text"] = ""
            state["stagnant"] = 0
            return action
        if len(rows) > 1:
            # 首屏同时出现多个同名行：不猜，交给人选定
            labels: list[str] = []
            for target, row in rows:
                label = row_title(row["text"]) or target
                if label not in labels:
                    labels.append(label)
            raise AmbiguousGroupName(rows[0][0], labels)
        if (ctx.search_allowed and (not rows or ctx.search_force)
                and not state.get("search_attempted")):
            entry = find_search_entry(ctx.xml)
            if entry is not None:
                state["search_attempted"] = True
                state["search_stage"] = "entry"
                x, y = entry["center"]
                return _tap_action(
                    ctx, "search_entry", "规则快路：首屏没有目标群，打开搜索",
                    x, y, "搜索")
        if ctx.list_fallback and (find_search_entry(ctx.xml) is not None
                                  or state.get("search_attempted")):
            return _list_scroll_action(ctx, state)

    # 3) 点已知 resource-id 的输入框（只读模式下不点，避免下一步必然被拒的输入）
    if (not ctx.read_only and "搜索" in (ctx.task or "")
            and not state.get("input_tapped")):
        box = find_input(ctx.xml, ctx.known_input_resource_ids)
        if box is not None:
            x, y = box["center"]
            action = _tap_action(
                ctx, "tap_input", "规则快路：点击已知输入框",
                x, y, box["text"] or "输入框",
                resource_id=box["resource_id"],
            )
            if action is None:
                return None
            state["input_tapped"] = True
            return action

    # 4) 已进入目标会话：固定手势向上翻，直到屏幕已含答案/文字不再变化/达到上限
    if (state.get("entered_group") and not state.get("scroll_disabled")
            and state.get("scrolls", 0) < ctx.max_scrolls):
        text = screen_text(ctx.xml)
        if not text or any(marker in text for marker in _END_MARKERS):
            state["scroll_disabled"] = True
            return None
        keyword = screen_has_task_keyword(
            ctx.xml, ctx.task, exclude=(state.get("entered_group") or "",))
        if keyword:
            # 当前屏幕已经能看到任务要的答案，再滚只会多花一次 dump 和一次手势
            state["scroll_disabled"] = True
            state["scroll_stop_keyword"] = keyword
            return None
        if text == state.get("last_scroll_text"):
            state["stagnant"] = state.get("stagnant", 0) + 1
            if state["stagnant"] >= 2:
                state["scroll_disabled"] = True
                return None
        else:
            state["stagnant"] = 0
            state["last_scroll_text"] = text
        width, height = ctx.screen_size or root_size(ctx.xml) or (1080, 2374)
        state["scrolls"] = state.get("scrolls", 0) + 1
        return _action(
            "swipe", "scroll", "规则快路：向上滚动读更早的消息",
            x=width // 2, y=int(height * SCROLL_START_RATIO),
            x2=width // 2, y2=int(height * (SCROLL_START_RATIO - SCROLL_TRAVEL_RATIO)),
        )

    return None
