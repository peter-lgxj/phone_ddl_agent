"""跨来源去重：关键词 + 语义相似度。

默认实现完全离线（词法 + 字符级相似度），确定性可复现；
需要更强语义匹配时可以注入自己的 ``similarity`` 函数（如 embedding 余弦）。
"""

from __future__ import annotations

import difflib
import re
from typing import Callable

from .extract.normalizer import cn_number
from .models import DeadlineCandidate, TaskCluster
from .settings import (
    DEDUPE_CHAR_WEIGHT,
    DEDUPE_THRESHOLD,
    DEDUPE_TOKEN_WEIGHT,
    SOURCE_CREDIBILITY,
)

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

    text = re.sub(r"\b(?:no|number)\s+(\d+)\b", r"\1", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


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
                 similarity: Callable[[str, str], float] | None = None):
        self.threshold = threshold
        self.similarity = similarity or task_similarity

    def cluster(self, candidates: list[DeadlineCandidate]) -> list[TaskCluster]:
        ordered = sorted(
            [c for c in candidates if c.task_name_raw.strip()],
            key=lambda c: (
                normalize_task_name(c.task_name_raw),
                c.published_at,
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
                score = self.similarity(normalized, normalize_task_name(cluster.task_name))
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


def _rank_key(candidate: DeadlineCandidate) -> tuple[int, object]:
    return (SOURCE_CREDIBILITY.get(candidate.source_type, 0), candidate.published_at)
