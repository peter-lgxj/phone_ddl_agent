"""全局阈值与常量。

刻意不依赖根目录的 config.py：手机层配置与本包无关，避免互相牵制。
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone

# 中国无夏令时，用固定 +08:00，避免 Windows 上缺 tzdata 时 zoneinfo 失败
TZ = timezone(timedelta(hours=8))

# 只有日期没有时刻时，按当天 23:59 处理（并在 note 中标注）
DEFAULT_DEADLINE_HOUR = 23
DEFAULT_DEADLINE_MINUTE = 59

# 去重相似度阈值：>= 阈值且课程相同才合并
DEDUPE_THRESHOLD = 0.78

# 本地语义相似度（sentence-transformers）。模型与缓存默认放 F 盘，避免占用 C 盘。
# 可用环境变量覆盖：DDL_EMBEDDING_MODEL / DDL_EMBEDDING_CACHE
EMBEDDING_MODEL = os.getenv(
    "DDL_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
EMBEDDING_CACHE_DIR = os.getenv(
    "DDL_EMBEDDING_CACHE", "F:/models/sentence-transformers"
)
# 短文本的 embedding 余弦分数整体偏低，单独给一个可配阈值；词法仍用 DEDUPE_THRESHOLD
EMBEDDING_THRESHOLD = float(os.getenv("DDL_EMBEDDING_THRESHOLD", "0.6"))
DEDUPE_TOKEN_WEIGHT = 0.6
DEDUPE_CHAR_WEIGHT = 0.4

# 冲突无法判定时最多追加检索的轮数
MAX_SUPPLEMENTARY_ROUNDS = 2

# 来源可信度：官方邮件 > 课程公告 > 教师群消息 > 同学消息
SOURCE_CREDIBILITY: dict[str, int] = {
    "official_email": 4,
    "course_announcement": 3,
    "instructor_chat": 2,
    "student_chat": 1,
}

# 各类判定对应的置信度
CONFIDENCE_CONSISTENT = 0.95
CONFIDENCE_SOURCE_PRIORITY = 0.85
CONFIDENCE_NEWER_ANNOUNCEMENT = 0.75
CONFIDENCE_UNCERTAIN = 0.4
CONFIDENCE_MISSING = 0.3

# 文档表头（写入器共用；换写入器不改变语义）
DEADLINE_COLUMNS = [
    "Task Name",
    "Course",
    "Deadline",
    "Status",
    "Source",
    "All Sources",
    "Confidence",
    "Evidence",
    "Last Updated",
]
AUDIT_COLUMNS = ["Timestamp", "Action", "Task Name", "Old Value", "New Value", "Reason"]

# 成本估算用的默认单价（美元 / 1K token），仅用于评测报告中的近似值
DEFAULT_INPUT_PRICE_PER_1K = 0.0002
DEFAULT_OUTPUT_PRICE_PER_1K = 0.0008

# DeepSeek 官方定价（约 $0.27 / 百万输入、$1.10 / 百万输出），仅作近似成本
LLM_INPUT_PRICE_PER_1K = 0.00027
LLM_OUTPUT_PRICE_PER_1K = 0.0011

# 冲突裁决的最低可接受置信度；低于该值一律标 uncertain
LLM_MIN_CONFIDENCE = 0.6
