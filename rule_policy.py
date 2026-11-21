"""规则快路：控件树能证明的动作不调用视觉模型。

只覆盖四类可被控件树或系统约定证明的动作：

- ``open_group``：任务里用引号点名的群/会话，在控件树里有唯一可点击行时直接点开；
- ``back_after_miss``：上一步目标未定位时先返回上一级，避免在错误界面继续猜；
- ``scroll``：已进入目标会话后按固定手势向上翻，直到文字不再变化或达到上限；
- ``tap_input``：已知 resource-id / EditText 的输入框直接点，不需要视觉判断。

任何需要理解截图语义的场景都返回 ``None``，交回 VLM 决策。这里不产生
``send`` / ``type`` / ``press_enter``，只读保护仍在 ``ScreenAgent._execute``
和驱动层各拦一道。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# 任务里用「」、“”、"" 或 '' 点名的目标，例如「COMP7503 作业通知群」
_QUOTED_RE = re.compile(r"[「“\"]([^」”\"]{2,40})[」”\"]|‘([^’]{2,40})’")

# 界面已经翻到底/顶的提示词；命中后停止规则滚动，交回模型做语义判断
_END_MARKERS = ("没有更多", "已加载全部", "查看更多消息", "没有更多消息")

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


@dataclass
class RuleContext:
    task: str
    step: int
    read_only: bool
    xml: str
    last_action: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    max_scrolls: int = 2
    known_input_resource_ids: tuple[str, ...] = ()
    screen_size: tuple[int, int] | None = None


def extract_quoted_targets(task: str) -> list[str]:
    """取出任务里被引号点名的目标，保持出现顺序并去重。"""
    targets: list[str] = []
    for match in _QUOTED_RE.finditer(task or ""):
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


def screen_text(xml_text: str, limit: int = 60) -> str:
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


def find_target_row(xml_text: str, needle: str) -> dict | None:
    """在控件树里找 needle 对应的可点击行。

    完全匹配优先；只有一个候选时才返回，多个候选说明有相似群名，交给模型判断。
    """
    root = parse_xml(xml_text)
    if root is None:
        return None
    exact, partial = _collect_matches(root, needle)
    for bucket in (exact, partial):
        candidates = [item for item in bucket if item["clickable"]]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # 同一行可能同时命中 text 与 content-desc，按 bounds 去重后仍是多个才算歧义
            distinct = {item["bounds"] for item in candidates}
            return candidates[0] if len(distinct) == 1 else None
    return None


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


def _action(action_type: str, rule: str, description: str, **extra) -> dict:
    payload = {
        "action_type": action_type,
        "description": description,
        "target": "",
        "thought": description,
        "rule": {"name": rule},
    }
    payload.update(extra)
    if payload["action_type"] in _MUTATING_ACTIONS:
        raise ValueError(f"规则快路不得产生写动作：{payload['action_type']}")
    return payload


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

    # 2) 开群/打开会话：任务里点名的目标在控件树里有唯一可点击行
    if targets and not state.get("entered_group"):
        found = []
        for target in targets:
            row = find_target_row(ctx.xml, target)
            if row is not None:
                found.append((target, row))
        if len(found) == 1:
            target, row = found[0]
            state["entered_group"] = row["text"] or target
            state["scrolls"] = 0
            state["last_scroll_text"] = ""
            state["stagnant"] = 0
            x, y = row["center"]
            return _action(
                "tap", "open_group", f"规则快路：打开「{row['text'] or target}」",
                x=x, y=y, target=row["text"] or target,
            )

    # 3) 点已知 resource-id 的输入框（只读模式下不点，避免下一步必然被拒的输入）
    if (not ctx.read_only and "搜索" in (ctx.task or "")
            and not state.get("input_tapped")):
        box = find_input(ctx.xml, ctx.known_input_resource_ids)
        if box is not None:
            state["input_tapped"] = True
            x, y = box["center"]
            return _action(
                "tap", "tap_input", "规则快路：点击已知输入框",
                x=x, y=y, target=box["text"] or "输入框",
                resource_id=box["resource_id"],
            )

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
            x=width // 2, y=int(height * 0.75),
            x2=width // 2, y2=int(height * 0.30),
        )

    return None
