"""CSV 写入器：便于无 Excel 环境下检查，语义与 xlsx 完全一致。"""

from __future__ import annotations

import csv
import os
from datetime import datetime

from ..models import ResolvedRecord
from ..settings import DEADLINE_COLUMNS
from .base import DeadlineWriter


class CsvWriter(DeadlineWriter):
    def _write(self, rows: list[ResolvedRecord], events: list[dict],
               timestamp: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(DEADLINE_COLUMNS)
            for record in rows:
                writer.writerow([
                    record.task_name,
                    record.course or "",
                    record.deadline_iso or "",
                    record.status,
                    record.source,
                    f"{record.confidence:.2f}",
                    "; ".join(record.evidence_ids),
                    record.updated_at or "",
                ])
        os.replace(tmp_path, self.path)

    def _read(self) -> list[ResolvedRecord]:
        records: list[ResolvedRecord] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not row.get("Task Name"):
                    continue
                records.append(
                    ResolvedRecord(
                        task_name=row["Task Name"],
                        course=row.get("Course") or None,
                        deadline_iso=row.get("Deadline") or None,
                        status=row.get("Status") or "uncertain",
                        source=row.get("Source") or "",
                        confidence=float(row.get("Confidence") or 0.0),
                        evidence_ids=[item for item in (row.get("Evidence") or "").split("; ") if item],
                        updated_at=row.get("Last Updated") or None,
                    )
                )
        return records
