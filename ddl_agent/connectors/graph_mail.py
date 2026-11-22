"""Microsoft Graph 邮件连接器（骨架）。

**状态：未经真实数据验证。** 需要真实 Azure 应用注册与 ``Mail.Read`` 权限；
没有凭据时抛 ``SourceNotConfigured``，不静默退回本地数据。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable

from ..models import RawMessage, RetrievalPlan, SourceType
from ..settings import TZ
from .base import SourceError, SourceNotConfigured, message_in_plan

GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"


class GraphMailConnector:
    def __init__(self, name: str = "outlook",
                 token_provider: Callable[[], str | None] | None = None,
                 folder: str = "inbox", top: int = 50,
                 source_type: SourceType = "official_email"):
        self.name = name
        self.source_type = source_type
        self.token_provider = token_provider
        self.folder = folder
        self.top = top

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        token = self.token_provider() if self.token_provider else None
        if not token:
            raise SourceNotConfigured(
                "Microsoft Graph 未配置访问令牌（需要 Mail.Read 权限）；"
                "本实现未经真实数据验证，离线评测请使用 LocalEmlConnector"
            )

        params = [
            f"$top={self.top}",
            "$select=id,subject,from,receivedDateTime,bodyPreview,conversationId",
            "$orderby=receivedDateTime desc",
        ]
        url = f"{GRAPH_MESSAGES_URL}?{'&'.join(params)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SourceError(f"Microsoft Graph 请求失败：{exc}") from exc

        messages: list[RawMessage] = []
        for item in payload.get("value", []):
            received = item.get("receivedDateTime")
            try:
                published_at = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                review_reason = ""
            except (TypeError, ValueError):
                # 解析不到真实收到时间时留空并标记，不能用抓取时刻冒充发布时间
                published_at = None
                review_reason = "邮件收到时间解析失败，发布时间未知"
            message = RawMessage(
                message_id=str(item.get("id") or ""),
                source_name=self.name,
                source_type=self.source_type,
                sender=str((item.get("from") or {}).get("emailAddress", {}).get("address") or "unknown"),
                published_at=published_at,
                subject=str(item.get("subject") or ""),
                body=str(item.get("bodyPreview") or ""),
                thread_id=item.get("conversationId"),
                needs_review=bool(review_reason),
                review_reason=review_reason,
            )
            if message_in_plan(message, plan):
                messages.append(message)
        return messages
