"""语义相似度：本地 embedding + 词法相似度，可离线降级。

不依赖外部 API。模型只在真正需要时加载；模型缺失、未下载或加载失败时，
自动退回纯词法路径，并记录原因供报告展示。
"""

from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path

from .settings import (
    DEDUPE_THRESHOLD,
    EMBEDDING_CACHE_DIR,
    EMBEDDING_MODEL,
    EMBEDDING_THRESHOLD,
)


class EmbeddingBackend:
    """sentence-transformers 的懒加载封装，缓存目录默认放在 F 盘。"""

    def __init__(self, model_name: str = EMBEDDING_MODEL,
                 cache_dir: str | Path = EMBEDDING_CACHE_DIR,
                 local_files_only: bool = False):
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self.local_files_only = local_files_only
        self._model = None
        self._error: str | None = None
        self._vectors: dict[str, list[float]] = {}

    @property
    def installed(self) -> bool:
        return importlib.util.find_spec("sentence_transformers") is not None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def error(self) -> str | None:
        return self._error

    def _load(self):
        if self._model is not None or self._error is not None:
            return self._model
        if not self.installed:
            self._error = "未安装 sentence-transformers"
            return None

        # 尽量把模型与框架缓存都留在指定盘符，避免写到 C 盘用户目录
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.cache_dir))
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(self.cache_dir))
        os.environ.setdefault("TORCH_HOME", str(self.cache_dir / "torch"))
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name,
                cache_folder=str(self.cache_dir),
                local_files_only=self.local_files_only,
            )
        except Exception as exc:  # noqa: BLE001 - 下载/加载失败都应降级而不是中断
            self._error = f"{type(exc).__name__}: {exc}"
            self._model = None
        return self._model

    def encode(self, text: str) -> list[float] | None:
        cached = self._vectors.get(text)
        if cached is not None:
            return cached
        model = self._load()
        if model is None:
            return None
        vector = model.encode(
            [text], normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        values = [float(item) for item in vector]
        self._vectors[text] = values
        return values

    def similarity(self, left: str, right: str) -> float | None:
        left_vector = self.encode(left)
        right_vector = self.encode(right)
        if left_vector is None or right_vector is None:
            return None
        cosine = sum(a * b for a, b in zip(left_vector, right_vector))
        return max(0.0, min(1.0, cosine))


class SemanticMatcher:
    """三种策略：lexical / embedding / combined。"""

    STRATEGIES = ("lexical", "embedding", "combined")

    def __init__(self, strategy: str = "lexical",
                 model_name: str = EMBEDDING_MODEL,
                 cache_dir: str | Path = EMBEDDING_CACHE_DIR,
                 threshold: float | None = None,
                 backend: EmbeddingBackend | None = None,
                 local_files_only: bool = False):
        if strategy not in self.STRATEGIES:
            raise ValueError(f"未知相似度策略：{strategy}")
        self.strategy = strategy
        self.threshold = (
            threshold if threshold is not None
            else (DEDUPE_THRESHOLD if strategy == "lexical" else EMBEDDING_THRESHOLD)
        )
        self.backend = backend or EmbeddingBackend(
            model_name=model_name, cache_dir=cache_dir,
            local_files_only=local_files_only,
        )

    @property
    def available(self) -> bool:
        if self.strategy == "lexical":
            return True
        return self.backend.similarity("测试", "测试") is not None

    def similarity(self, left: str, right: str) -> float:
        from .dedupe import normalize_task_name, task_similarity

        lexical = task_similarity(normalize_task_name(left), normalize_task_name(right))
        if self.strategy == "lexical":
            return lexical
        embedding = self.backend.similarity(left, right)
        if embedding is None:
            return lexical
        if self.strategy == "embedding":
            return embedding
        # 组合：词法或 embedding 任一强匹配即可通过阈值。
        # 这样 embedding 才能真正救回“期中报告 / midterm report”这类零词法重叠的写法。
        return max(lexical, embedding)

    def info(self) -> dict:
        available = self.available
        return {
            "strategy": self.strategy,
            "model": self.backend.model_name,
            "cache_dir": str(self.backend.cache_dir),
            "threshold": self.threshold,
            "available": available,
            "fallback_reason": None if available else (self.backend.error or "embedding 不可用"),
        }


def combined_similarity(left: str, right: str) -> float:
    """便捷函数：词法与 embedding 取较高分；模型不可用时退回词法。"""
    matcher = SemanticMatcher(strategy="combined")
    return matcher.similarity(left, right)


def cosine(left: list[float], right: list[float]) -> float:
    """纯 Python 余弦，供外部测试或轻量场景使用。"""
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))
