"""语义相似度：组合公式、降级路径与去重接入（离线）。

真实 embedding 模型用例默认跳过，设置 RUN_EMBEDDING_TESTS=1 才运行。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from ddl_agent.dedupe import Deduplicator, task_similarity
from ddl_agent.models import DeadlineCandidate
from ddl_agent.semantic import SemanticMatcher
from ddl_agent.settings import TZ

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)


class _FakeBackend:
    model_name = "fake-embedding"
    cache_dir = Path("F:/models/fake")

    def __init__(self, value: float | None, error: str | None = None):
        self.value = value
        self.error = error

    def similarity(self, left: str, right: str) -> float | None:
        return self.value


def _candidate(candidate_id: str, name: str) -> DeadlineCandidate:
    return DeadlineCandidate(
        candidate_id=candidate_id,
        task_name_raw=name,
        course="COMP7608",
        deadline_raw="2026-11-20 23:59",
        deadline_iso="2026-11-20T23:59:00+08:00",
        source_type="student_chat",
        source_name="课程群",
        published_at=NOW,
        message_id=candidate_id,
    )


def test_lexical_strategy_matches_existing_similarity():
    matcher = SemanticMatcher(strategy="lexical", backend=_FakeBackend(1.0))
    left, right = "assignment 1", "lab 2"
    assert matcher.similarity(left, right) == task_similarity(left, right)
    assert matcher.info()["available"] is True


def test_combined_takes_the_stronger_evidence():
    matcher = SemanticMatcher(strategy="combined", backend=_FakeBackend(1.0))
    left, right = "assignment 1", "lab 2"
    assert matcher.similarity(left, right) == 1.0

    matcher = SemanticMatcher(strategy="combined", backend=_FakeBackend(0.2))
    assert matcher.similarity(left, right) == 0.2


def test_embedding_strategy_can_lower_similarity():
    matcher = SemanticMatcher(strategy="embedding", backend=_FakeBackend(0.1))
    assert matcher.similarity("assignment 1", "assignment 1") == pytest.approx(0.1)


def test_unavailable_embedding_falls_back_to_lexical():
    matcher = SemanticMatcher(
        strategy="combined",
        backend=_FakeBackend(None, error="模型未下载"),
    )
    left, right = "assignment 1", "lab 2"
    assert matcher.similarity(left, right) == task_similarity(left, right)
    info = matcher.info()
    assert info["available"] is False
    assert "模型未下载" in info["fallback_reason"]


def test_deduplicator_accepts_semantic_similarity():
    matcher = SemanticMatcher(strategy="embedding", backend=_FakeBackend(0.99))
    dedupe = Deduplicator(similarity=matcher.similarity)
    clusters = dedupe.cluster([_candidate("c1", "HW1"), _candidate("c2", "Lab 2")])
    assert len(clusters) == 1  # 假 embedding 把所有任务名都判为高度相似


@pytest.mark.skipif(
    os.getenv("RUN_EMBEDDING_TESTS") != "1",
    reason="真实 embedding 模型用例默认跳过；设置 RUN_EMBEDDING_TESTS=1 后运行",
)
def test_real_multilingual_embedding_is_available():
    pytest.importorskip("sentence_transformers")
    matcher = SemanticMatcher(strategy="combined")
    assert matcher.available
    assert matcher.similarity("作业一", "homework 1") > 0.5
