"""数据源连接器接口。

上层只依赖 ``MessageSource`` 协议：任何来源（本地 .eml、聊天归档、Microsoft Graph、
手机 GUI 自动化）都实现同一个 ``fetch(plan)``，因此评测可以任意替换而不改流水线。
"""

from __future__ import annotations

from typing import Protocol

from ..extract.rules import find_courses, normalize_course
from ..models import RawMessage, RetrievalPlan, SourceType


class SourceError(RuntimeError):
    """单个来源检索失败；调用方应记录告警并继续其他来源。"""


class SourceNotConfigured(SourceError):
    """来源需要凭据/设备但当前不可用。"""


class MessageSource(Protocol):
    name: str
    source_type: SourceType

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        ...


def message_in_plan(message: RawMessage, plan: RetrievalPlan) -> bool:
    """按课程做软过滤。

    课程过滤是「软」的：消息若完全没提到任何课程代码则保留，
    避免把「同学在群里问 HW2 截止」这类关键证据丢掉。

    时间窗不在这里过滤：用户问「下周的 DDL」指的是截止时间，
    而消息发布时间可能更早；时间窗在冲突解决后再筛最终记录。
    """
    if plan.course_filters:
        wanted = {normalize_course(item) for item in plan.course_filters}
        found = {
            normalize_course(code)
            for code in find_courses(f"{message.subject}\n{message.body}")
        }
        if found and not (found & wanted):
            return False
    return True
