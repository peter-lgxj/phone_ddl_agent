"""LLM 客户端：复用根目录 ``config.py`` 的 provider 配置，密钥只从环境/.env 读取。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..settings import LLM_INPUT_PRICE_PER_1K, LLM_OUTPUT_PRICE_PER_1K


@dataclass
class UsageTracker:
    """累计每次调用的 token、耗时，供评测报告统计。"""

    events: list[dict] = field(default_factory=list)

    def record(self, kind: str, elapsed: float, prompt_tokens: int, completion_tokens: int) -> None:
        self.events.append({
            "kind": kind,
            "elapsed": round(elapsed, 4),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })

    def snapshot(self) -> dict:
        prompt = sum(event["prompt_tokens"] for event in self.events)
        completion = sum(event["completion_tokens"] for event in self.events)
        return {
            "llm_calls": len(self.events),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "tokens_estimate": prompt + completion,
            "llm_seconds": round(sum(event["elapsed"] for event in self.events), 4),
            "cost_usd": round(
                prompt / 1000 * LLM_INPUT_PRICE_PER_1K
                + completion / 1000 * LLM_OUTPUT_PRICE_PER_1K,
                6,
            ),
        }

    def since(self, snapshot: dict) -> dict:
        current = self.snapshot()
        return {
            key: round(current[key] - snapshot.get(key, 0), 6)
            for key in ("llm_calls", "prompt_tokens", "completion_tokens",
                        "tokens_estimate", "llm_seconds", "cost_usd")
        }


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    """最小封装：一次文本补全、一次 JSON 补全、用量累计。"""

    def __init__(self, client: Any, model: str, extra_body: dict | None = None,
                 tracker: UsageTracker | None = None, max_attempts: int = 2):
        self.client = client
        self.model = model
        self.extra_body = extra_body or None
        self.tracker = tracker or UsageTracker()
        self.max_attempts = max_attempts

    @classmethod
    def from_config(cls, provider: str | None = None,
                    tracker: UsageTracker | None = None) -> "LLMClient | None":
        provider = provider or os.getenv("DDL_LLM_PROVIDER", "deepseek")
        try:
            import config as root_config  # 手机层的 provider 配置
        except ImportError:
            return None

        spec = root_config.PROVIDERS.get(provider)
        if not spec or not spec.get("api_key"):
            return None

        try:
            from openai import OpenAI
        except ImportError:
            return None

        client = OpenAI(
            api_key=spec["api_key"],
            base_url=spec["base_url"],
            timeout=float(os.getenv("DDL_LLM_TIMEOUT", "60")),
        )
        return cls(client, spec["model"], spec.get("extra_body"), tracker)

    # ------------------------------------------------------------------ #
    def complete(self, prompt: str, system: str | None = None,
                 kind: str = "completion") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error: Exception | None = None
        for _attempt in range(self.max_attempts):
            started = time.perf_counter()
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.0,
                    messages=messages,
                    extra_body=self.extra_body,
                )
            except Exception as exc:  # noqa: BLE001 - 网络/额度/解析异常统一重试一次
                last_error = exc
                continue

            usage = getattr(response, "usage", None)
            self.tracker.record(
                kind,
                time.perf_counter() - started,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            )
            return (response.choices[0].message.content or "").strip()

        raise LLMUnavailable(f"LLM 调用失败：{last_error}")

    def complete_json(self, prompt: str, system: str | None = None,
                      kind: str = "json") -> Any:
        raw = self.complete(prompt, system=system, kind=kind)
        return parse_json(raw)


def parse_json(raw: str) -> Any:
    """从模型输出里抠 JSON；解析失败返回 None，由调用方降级。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
