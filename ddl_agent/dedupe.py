"""跨来源去重：关键词 + 语义相似度。

默认实现完全离线（词法 + 字符级相似度），确定性可复现；
需要更强语义匹配时可以注入自己的 ``similarity`` 函数（如 embedding 余弦）。
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime
from typing import Callable

from .extract.normalizer import cn_number
from .models import DeadlineCandidate, TaskCluster
from .settings import (
    DEDUPE_CHAR_WEIGHT,
    DEDUPE_THRESHOLD,
    DEDUPE_TOKEN_WEIGHT,
    TZ,
)

_MIN_TIME = datetime.min.replace(tzinfo=TZ)

_CN_TASK_MAP = [
    (re.compile(r"大作业\s*([一二三四五六七八九十\d]+)?"), "project"),
    (re.compile(r"作业\s*([一二三四五六七八九十\d]+)?"), "assignment"),
    (re.compile(r"实验\s*([一二三四五六七八九十\d]+)?"), "lab"),
    (re.compile(r"报告\s*([一二三四五六七八九十\d]+)?"), "report"),
    (re.compile(r"论文\s*([一二三四五六七八九十\d]+)?"), "essay"),
    (re.compile(r"考试\s*([一二三四五六七八九十\d]+)?"), "exam"),
    (re.compile(r"项目\s*([一二三四五六七八九十\d]+)?"), "project"),
]

_EN_TASK_PATTERNS = [
    (re.compile(r"\bhomework\s*0*(\d+)\b", re.IGNORECASE), "assignment"),
    (re.compile(r"\bhw\s*0*(\d+)\b", re.IGNORECASE), "assignment"),
    (re.compile(r"\bassign(?:ment)?\s*0*(\d+)\b", re.IGNORECASE), "assignment"),
    (re.compile(r"\bproj(?:ect)?\s*0*(\d+)\b", re.IGNORECASE), "project"),
    (re.compile(r"\blab\s*0*(\d+)\b", re.IGNORECASE), "lab"),
]

_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_SPACE_RE = re.compile(r"\s+")
# 重复读取同一置顶消息时，任务名常被模型改写成「提交/截止/提醒」等流程词，
# 判重前先去掉这些词，避免同一条 DDL 被当成两个任务。
_MERGE_NOISE_RE = re.compile(r"提交|截止|提醒|通知|deadline|due|ddl", re.IGNORECASE)


def normalize_task_name(name: str) -> str:
    """把 ``HW1`` / ``Homework 1`` / ``Assignment 1`` / ``作业一`` 统一成同一形式。"""
    text = (name or "").lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()

    for pattern, word in _CN_TASK_MAP:
        def _replace(match: re.Match, word: str = word) -> str:
            number = cn_number(match.group(1) or "")
            return f"{word} {number}" if number is not None else word

        text = pattern.sub(_replace, text)

    for pattern, word in _EN_TASK_PATTERNS:
        text = pattern.sub(lambda m, w=word: f"{w} {int(m.group(1))}", text)

    # 「机器学习作业三」→「assignment 3」：去掉紧邻数字任务名的中文科目前缀，
    # 否则群里的「机器学习作业三」无法和邮件的「Assignment 3」合并成同一任务。
    text = re.sub(
        r"[\u4e00-\u9fff]+\s*(?=(?:assignment|project|lab|essay|exam)\s+\d+)",
        "",
        text,
    )

    text = re.sub(r"\b(?:no|number)\s+(\d+)\b", r"\1", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def duplicate_name_key(name: str) -> str:
    """判重用任务名：在通用归一化后再去掉「提交/截止/提醒」等流程词。

    最后去掉全部空白：中文任务名与英文单词的分词方式不一致，
    ``小组作业 report`` 与 ``小组作业提交（report）`` 去掉流程词后
    只差一个空格，不能因此判成两个任务。
    """
    text = normalize_task_name(name)
    text = _MERGE_NOISE_RE.sub(" ", text)
    return _SPACE_RE.sub("", text)


def _merge_rank(candidate: DeadlineCandidate) -> tuple[float, int, str]:
    """同组重复候选的保留优先级：置信度高、读取更早、id 稳定者胜出。"""
    confidence = (
        candidate.extractor_confidence
        if candidate.extractor_confidence is not None
        else -1.0
    )
    # source_order 越小越早读到；缺省时排在已知顺序之后
    earlier = -(candidate.source_order if candidate.source_order is not None else 10**9)
    return (confidence, earlier, candidate.candidate_id)


def merge_duplicate_candidates(
    candidates: list[DeadlineCandidate],
) -> tuple[list[DeadlineCandidate], list[dict]]:
    """合并同来源、同截止时间、任务名归一化后一致的重复候选。

    读屏超时后重读同一置顶消息，会产出两条截止时间相同、任务名只差
    「提交」等流程词的记录。这些重复读取不能变成两条 DDL：保留一条，
    其余设置 ``merged_duplicate_of`` 并记录合并轨迹。
    """
    groups: dict[tuple[str, str, str], list[DeadlineCandidate]] = {}
    for candidate in candidates:
        if not candidate.deadline_iso or not candidate.task_name_raw.strip():
            continue
        key = (
            candidate.source_name,
            candidate.deadline_iso,
            duplicate_name_key(candidate.task_name_raw),
        )
        groups.setdefault(key, []).append(candidate)

    dropped: set[str] = set()
    merged: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=_merge_rank)
        for candidate in members:
            if candidate is keeper:
                continue
            candidate.merged_duplicate_of = keeper.candidate_id
            dropped.add(candidate.candidate_id)
            merged.append(
                {
                    "kept": keeper.candidate_id,
                    "merged": candidate.candidate_id,
                    "merged_duplicate_of": keeper.candidate_id,
                    "source_name": candidate.source_name,
                    "deadline": candidate.deadline_iso,
                    "task_name_raw": candidate.task_name_raw,
                    "kept_task_name_raw": keeper.task_name_raw,
                    "reason": "同来源、同截止时间、任务名归一化后一致（重复读取）",
                }
            )

    if not dropped:
        return list(candidates), merged
    return [c for c in candidates if c.candidate_id not in dropped], merged


def task_similarity(left: str, right: str) -> float:
    """0~1 的相似度：词面 Jaccard 与字符序列相似度的加权。"""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    left_tokens, right_tokens = set(left.split()), set(right.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    char_ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return DEDUPE_TOKEN_WEIGHT * jaccard + DEDUPE_CHAR_WEIGHT * char_ratio


def _courses_compatible(left: str | None, right: str | None) -> bool:
    if left and right:
        return left.upper() == right.upper()
    return True  # 聊天记录常缺课程代码，名称匹配即可合并


class Deduplicator:
    def __init__(self, threshold: float = DEDUPE_THRESHOLD,
                 similarity: Callable[[str, str], float] | None = None,
                 raw_inputs: bool = False):
        self.threshold = threshold
        self.similarity = similarity or task_similarity
        # 语义模型需要原始文本（归一化会把中文任务名混成中英混合），词法默认用归一化文本
        self.raw_inputs = raw_inputs

    def cluster(self, candidates: list[DeadlineCandidate]) -> list[TaskCluster]:
        ordered = sorted(
            [c for c in candidates if c.task_name_raw.strip()],
            key=lambda c: (
                normalize_task_name(c.task_name_raw),
                c.published_at or _MIN_TIME,
                c.source_name,
                c.candidate_id,
            ),
        )

        clusters: list[TaskCluster] = []
        for candidate in ordered:
            normalized = normalize_task_name(candidate.task_name_raw)
            best_index, best_score = None, 0.0
            for index, cluster in enumerate(clusters):
                if not _courses_compatible(cluster.course, candidate.course):
                    continue
                left = candidate.task_name_raw if self.raw_inputs else normalized
                right = cluster.task_name if self.raw_inputs else normalize_task_name(
                    cluster.task_name
                )
                score = self.similarity(left, right)
                if score > best_score:
                    best_index, best_score = index, score

            if best_index is not None and best_score >= self.threshold:
                cluster = clusters[best_index]
                cluster.candidates.append(candidate)
                if not cluster.course and candidate.course:
                    cluster.course = candidate.course
                if self._is_better_representative(candidate, cluster.candidates):
                    cluster.task_name = candidate.task_name_raw
            else:
                clusters.append(
                    TaskCluster(
                        cluster_id=f"cluster-{len(clusters) + 1:04d}",
                        task_name=candidate.task_name_raw,
                        course=candidate.course,
                        candidates=[candidate],
                    )
                )

        return clusters

    @staticmethod
    def _is_better_representative(candidate: DeadlineCandidate, members: list[DeadlineCandidate]) -> bool:
        current = max(members[:-1], key=_rank_key, default=None)
        return current is not None and _rank_key(candidate) > _rank_key(current)


def clusters_without_dedup(candidates: list[DeadlineCandidate]) -> list[TaskCluster]:
    """消融用：每条候选单独成簇，不做任何合并。"""
    usable = [candidate for candidate in candidates if candidate.task_name_raw.strip()]
    return [
        TaskCluster(
            cluster_id=f"cluster-{position:04d}",
            task_name=candidate.task_name_raw,
            course=candidate.course,
            candidates=[candidate],
        )
        for position, candidate in enumerate(usable, start=1)
    ]


def _rank_key(candidate: DeadlineCandidate) -> tuple[object, int]:
    """代表性排序只看发布时间与读取顺序，不再给某类来源固定高低先验。"""
    return (
        candidate.published_at or _MIN_TIME,
        candidate.source_order or 0,
    )
