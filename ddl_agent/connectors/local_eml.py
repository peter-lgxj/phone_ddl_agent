"""本地 ``.eml`` 目录连接器：模拟 Outlook 收件箱，离线可跑。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from email import message_from_binary_file, policy
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..models import RawMessage, RetrievalPlan, SourceType
from ..settings import TZ
from .base import SourceError, message_in_plan


class LocalEmlConnector:
    """把一个目录下的 ``*.eml`` 当成邮件来源。"""

    def __init__(self, name: str, directory: str | Path,
                 source_type: SourceType = "official_email"):
        self.name = name
        self.source_type = source_type
        self.directory = Path(directory)

    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]:
        if not self.directory.exists():
            raise SourceError(f"邮件目录不存在：{self.directory}")

        messages: list[RawMessage] = []
        for path in sorted(self.directory.glob("*.eml")):
            message = self._read(path)
            if message_in_plan(message, plan):
                messages.append(message)
        return messages

    def _read(self, path: Path) -> RawMessage:
        try:
            with path.open("rb") as handle:
                parsed = message_from_binary_file(handle, policy=policy.default)
        except OSError as exc:
            raise SourceError(f"读取邮件失败：{path}：{exc}") from exc

        published_at = self._published_at(parsed, path)
        return RawMessage(
            message_id=parsed.get("Message-ID") or hashlib.sha1(path.name.encode()).hexdigest()[:16],
            source_name=self.name,
            source_type=self.source_type,
            sender=str(parsed.get("From") or "unknown"),
            published_at=published_at,
            subject=str(parsed.get("Subject") or path.stem),
            body=self._body_text(parsed),
            evidence_path=str(path),
            thread_id=parsed.get("Thread-Index"),
        )

    @staticmethod
    def _published_at(parsed, path: Path) -> datetime:
        raw_date = parsed.get("Date")
        if raw_date:
            try:
                parsed_date = parsedate_to_datetime(str(raw_date))
                return parsed_date if parsed_date.tzinfo else parsed_date.replace(tzinfo=TZ)
            except (TypeError, ValueError):
                pass
        return datetime.fromtimestamp(path.stat().st_mtime, tz=TZ)

    @staticmethod
    def _body_text(parsed) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_maintype() != "text":
                    continue
                try:
                    content = part.get_content()
                except (LookupError, ValueError):
                    payload = part.get_payload(decode=True) or b""
                    content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if part.get_content_subtype() == "html":
                    html_parts.append(str(content))
                else:
                    plain_parts.append(str(content))
        else:
            try:
                plain_parts.append(str(parsed.get_content()))
            except (LookupError, ValueError):
                payload = parsed.get_payload(decode=True) or b""
                plain_parts.append(payload.decode(parsed.get_content_charset() or "utf-8",
                                                  errors="replace"))

        if plain_parts:
            return "\n".join(plain_parts).strip()
        return _strip_html("\n".join(html_parts))


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()
