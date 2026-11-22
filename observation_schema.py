"""读屏观察的结构化字段定义与校验。

视觉模型一步只发一次带图请求；合并观察与独立观察都要求返回 ``messages`` 数组，
每项固定包含 ``sender`` / ``time`` / ``body``。这样读屏结果能直接进入
``mobile_reading`` 的逐条解析，而不是让解析器去猜一段散文。

约定：
- 屏幕上确实没有发送者或时间时，模型写 ``未显示``（不省略字段、不猜测）；
- 字段缺失表示模型没有按结构返回，由调用方走散文兜底并在归档里标出；
- ``messages`` 为空数组表示本屏没有消息，是合法结构。
"""

from __future__ import annotations

from typing import Any

REQUIRED_FIELDS = ("sender", "time", "body")
ABSENT_VALUES = {
    "", "未显示", "未看到", "未知", "无", "none", "null", "-", "—", "/", "n/a",
}


def is_absent(value: Any) -> bool:
    return str(value or "").strip().lower() in ABSENT_VALUES


def clean_field(value: Any) -> str:
    text = str(value or "").strip()
    return "" if is_absent(text) else text


def validate_messages(value: Any) -> tuple[list[dict] | None, str]:
    """校验模型返回的 ``messages``。

    返回 ``(records, reason)``：
    - ``records`` 为 ``None``：格式不合法，``reason`` 说明缺什么；
    - ``records`` 为 ``[]``：格式合法，但本屏没有消息；
    - 否则为逐条结构化记录，缺失的发送者/时间已归一化为空串。
    """
    if not isinstance(value, list):
        return None, "messages 不是数组"
    records: list[dict] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            return None, f"第 {index} 条消息不是对象"
        missing = [field for field in REQUIRED_FIELDS if field not in item]
        if missing:
            return None, f"第 {index} 条消息缺少字段：{'/'.join(missing)}"
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        records.append({
            "sender": clean_field(item.get("sender")),
            "time": clean_field(item.get("time")),
            "body": body.replace("\n", " ").strip(),
            "sender_absent": is_absent(item.get("sender")),
            "time_absent": is_absent(item.get("time")),
        })
    return records, ""


def format_messages(records: list[dict]) -> str:
    """把结构化记录拼成 ``发送者：… 时间：… 原文：…`` 的逐行文本。"""
    lines: list[str] = []
    for record in records:
        lines.append(
            "发送者：{sender} 时间：{time} 原文：{body}".format(
                sender=record.get("sender") or "未显示",
                time=record.get("time") or "未显示",
                body=record.get("body") or "",
            )
        )
    return "\n".join(lines)
