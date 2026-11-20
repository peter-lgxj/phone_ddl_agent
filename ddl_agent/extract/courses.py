"""课程代码的识别与归一化。

抽取器、证据校验与去重共用同一套规则，避免「群名里有 COMP7503、正文里没有」
时各层对课程的理解不一致。
"""

from __future__ import annotations

import re

# 不用 \b：CJK 属于 \w，「COMP7503课程群」里 7503 与「课」之间没有词边界
COURSE_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{2,4})\s?-?\s?(\d{4})([A-Z]?)(?![A-Za-z0-9])"
)


def find_courses(text: str) -> list[str]:
    """抽取课程代码，统一成 ``COMP7608`` 形式。"""
    found: list[str] = []
    for match in COURSE_RE.finditer(text or ""):
        code = f"{match.group(1)}{match.group(2)}{match.group(3)}".upper()
        if code not in found:
            found.append(code)
    return found


def normalize_course(course: str | None) -> str | None:
    if not course:
        return None
    text = course.strip().upper().replace(" ", "")
    match = COURSE_RE.search(text)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}" if match else text
