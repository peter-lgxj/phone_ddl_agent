"""Markdown 写入器：适合直接贴进课程项目文档或 README。"""

from __future__ import annotations

import os
import re
from datetime import datetime

from ..models import ResolvedRecord
from ..settings import DEADLINE_COLUMNS
from .base import DeadlineWriter


class MarkdownWriter(DeadlineWriter):
    def _write(self, rows: list[ResolvedRecord], events: list[dict],
               timestamp: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Deadline Tracker",
            "",
            f"_Last updated: {timestamp.isoformat(timespec='seconds')}_",
            "",
            "| " + " | ".join(DEADLINE_COLUMNS) + " |",
            "| " + " | ".join(["---"] * len(DEADLINE_COLUMNS)) + " |",
        ]
        for record in rows:
            values = [
                record.task_name,
                record.course or "-",
                record.deadline_iso or "-",
                record.status,
                record.source,
                "; ".join(record.sources) or "-",
                f"{record.confidence:.2f}",
                ", ".join(record.evidence_ids) or "-",
                record.updated_at or "-",
            ]
            lines.append("| " + " | ".join(_escape(value) for value in values) + " |")

        if events:
            lines += ["", "## Audit", ""]
            for event in events:
                lines.append(
                    f"- {event.get('timestamp') or timestamp.isoformat(timespec='seconds')} "
                    f"`{event.get('action')}` {event.get('task_name', '')}："
                    f"{event.get('reason', '')}"
                )

        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

    def _read(self) -> list[ResolvedRecord]:
        records: list[ResolvedRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped.strip("|"))]
            if len(cells) != len(DEADLINE_COLUMNS) or cells[0] in ("Task Name", "---"):
                continue
            if set(cells[0]) <= {"-"}:
                continue
            records.append(
                ResolvedRecord(
                    task_name=_unescape(cells[0]),
                    course=None if cells[1] in ("-", "") else _unescape(cells[1]),
                    deadline_iso=None if cells[2] in ("-", "") else cells[2],
                    status=cells[3],
                    source=_unescape(cells[4]),
                    sources=[item.strip() for item in cells[5].split(";") if item.strip() and item != "-"],
                    confidence=float(cells[6]) if cells[6] else 0.0,
                    evidence_ids=[item.strip() for item in cells[7].split(",") if item.strip() and item != "-"],
                    updated_at=None if cells[8] in ("-", "") else cells[8],
                )
            )
        return records


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|")


def _unescape(value: str) -> str:
    return value.replace("\\|", "|")
