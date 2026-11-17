"""DDL Agent 数据模型。

所有跨模块传递的结构都在这里定义，避免模块之间互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

SourceType = Literal[
    "official_email",       # 教务/教师/助教正式邮件
    "course_announcement",  # 课程平台/群公告
    "instructor_chat",      # 教师在群里直接发的消息
    "student_chat",         # 同学之间的消息
]

RecordStatus = Literal["confirmed", "uncertain", "missing_deadline"]

SourceKind = Literal["email", "chat"]

SourceProvider = Literal["eml", "graph", "chat_archive", "mobile"]


@dataclass(frozen=True)
class SourceSpec:
    """一个可检索的数据源配置。"""

    name: str
    kind: SourceKind
    provider: SourceProvider
    locator: str = ""  # 目录、群名、邮箱文件夹等由 provider 解释的定位串
    source_type: SourceType = "official_email"
    enabled: bool = True


@dataclass
class RawMessage:
    """从任一来源取到的一条原始消息。"""

    message_id: str
    source_name: str
    source_type: SourceType
    sender: str
    published_at: datetime
    subject: str
    body: str
    evidence_path: str | None = None
    thread_id: str | None = None


@dataclass
class RetrievalPlan:
    """一次检索的计划。"""

    query: str
    sources: list[SourceSpec] = field(default_factory=list)
    course_filters: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    # 追加检索轮次：0 表示首轮；>0 表示针对某个冲突任务定向补查
    round_index: int = 0
    focus_task: str | None = None


@dataclass
class NormalizedTime:
    """时间归一化结果。"""

    iso: str | None
    matched_text: str | None
    unresolved: bool
    note: str = ""
    default_time_assumed: bool = False


@dataclass
class DeadlineCandidate:
    """从一条消息里抽出的单条 DDL 候选。"""

    candidate_id: str
    task_name_raw: str
    course: str | None
    deadline_raw: str | None
    deadline_iso: str | None
    source_type: SourceType
    source_name: str
    published_at: datetime
    message_id: str
    message_subject: str = ""
    missing_fields: list[str] = field(default_factory=list)
    unresolved_time: bool = False

    @property
    def has_deadline(self) -> bool:
        return bool(self.deadline_iso)


@dataclass
class TaskCluster:
    """同一任务的候选集合（去重结果）。"""

    cluster_id: str
    task_name: str
    course: str | None
    candidates: list[DeadlineCandidate] = field(default_factory=list)


@dataclass
class ResolvedRecord:
    """冲突解决后的最终记录，也是写入文档的一行。"""

    task_name: str
    course: str | None
    deadline_iso: str | None
    status: RecordStatus
    source: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    resolution_note: str = ""
    alternatives: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass
class WriteResult:
    ok: bool
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    rolled_back: bool = False
    path: str | None = None
    error: str | None = None


@dataclass
class AgentRunResult:
    query: str
    records: list[ResolvedRecord] = field(default_factory=list)
    plan: RetrievalPlan | None = None
    write_result: WriteResult | None = None
    warnings: list[str] = field(default_factory=list)
    trace: dict = field(default_factory=dict)
