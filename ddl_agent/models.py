"""DDL Agent 数据模型。

所有跨模块传递的结构都在这里定义，避免模块之间互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

SourceType = Literal[
    "official_email",       # 教务/教师/助教正式邮件
    "email",                # 兼容只写了 email 的旧数据/回放脚本
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
    published_at: datetime | None
    subject: str
    body: str
    evidence_path: str | None = None
    thread_id: str | None = None
    # 读屏无法解析真实发送者/发布时间时置位，供抽取与输出标记复核
    needs_review: bool = False
    review_reason: str = ""
    # 置顶状态元数据（群列表/聊天界面「置顶了一条消息」横幅可得时写入）
    pinned: bool = False


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
    # 规划器决定冲突时是否允许追加检索（LLM 规划可关闭）
    allow_supplementary: bool = True
    # 规划理由，写进 trace 便于人工检查
    reason: str = ""


@dataclass
class NormalizedTime:
    """时间归一化结果。"""

    iso: str | None
    matched_text: str | None
    unresolved: bool
    note: str = ""
    default_time_assumed: bool = False
    # 「月底前/下月初」这类相对表述是推断出来的，必须显式标注依据
    inferred: bool = False
    inference_basis: str = ""


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
    published_at: datetime | None
    message_id: str
    message_subject: str = ""
    missing_fields: list[str] = field(default_factory=list)
    unresolved_time: bool = False
    # LLM 抽取的置信度与是否需要人工复核（规则抽取保持 None/False）
    extractor_confidence: float | None = None
    needs_review: bool = False
    review_reason: str = ""
    # 课程代码来源：body / subject / source_name / model；用于审计兜底是否生效
    course_from: str | None = None
    # 同一来源内的读取顺序（从 1 递增）；发布时间相同时用它判断先后
    source_order: int | None = None
    # 原文是否包含「更正/延期/改为/以最新为准」等覆盖语义
    is_correction: bool = False
    correction_marker: str | None = None
    # 被同源更新记录取代时，指向新记录的 candidate_id
    superseded_by: str | None = None
    # 截止时间是否由相对表述推断（月底前/下月初），以及推断依据
    inferred: bool = False
    inference_basis: str = ""

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
    source: str                    # 获胜证据的来源描述
    evidence_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # 所有原始来源（跨来源去重后）
    confidence: float = 0.0
    resolution_note: str = ""
    alternatives: list[str] = field(default_factory=list)
    # 同源更正/延期被覆盖的旧记录（只输出生效值，旧值留在这里审计）
    superseded_by: list[dict] = field(default_factory=list)
    # 跨来源冲突记录：两侧原文、各自时间与裁决依据
    conflicts: list[dict] = field(default_factory=list)
    updated_at: str | None = None
    # 最近一次证据的发布时间与读取顺序，供「最近一条作业」类查询聚焦
    latest_published_at: str | None = None
    latest_source_order: int | None = None
    # 截止时间是否由相对表述推断（与候选同义，供输出/审计直接读取）
    inferred: bool = False
    inference_basis: str = ""


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
    # 读取为空/读取失败/读到但解析不出时间时置位，和「确实读过但没有目标」的 no_match 区分
    needs_review: bool = False
    review_reason: str = ""
