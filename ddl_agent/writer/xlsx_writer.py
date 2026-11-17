"""默认写入器：xlsx 表格（Deadlines + Audit Log 两个 sheet）。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook

from ..models import ResolvedRecord
from ..settings import AUDIT_COLUMNS, DEADLINE_COLUMNS
from .base import DeadlineWriter


def _row_values(record: ResolvedRecord) -> list:
    return [
        record.task_name,
        record.course or "",
        record.deadline_iso or "",
        record.status,
        record.source,
        round(record.confidence, 2),
        "; ".join(record.evidence_ids),
        record.updated_at or "",
    ]


class XlsxWriter(DeadlineWriter):
    def _write(self, rows: list[ResolvedRecord], events: list[dict],
               timestamp: datetime) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Deadlines"
        sheet.append(DEADLINE_COLUMNS)
        for record in rows:
            sheet.append(_row_values(record))

        audit_sheet = workbook.create_sheet("Audit Log")
        audit_sheet.append(AUDIT_COLUMNS)
        for event in events:
            audit_sheet.append([
                event.get("timestamp") or timestamp.isoformat(timespec="seconds"),
                event.get("action", ""),
                event.get("task_name", ""),
                event.get("old_value", ""),
                event.get("new_value", ""),
                event.get("reason", ""),
            ])

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(f"{self.path}.tmp")
        workbook.save(tmp_path)
        workbook.close()
        os.replace(tmp_path, self.path)

    def _read(self) -> list[ResolvedRecord]:
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            if "Deadlines" not in workbook.sheetnames:
                return []
            sheet = workbook["Deadlines"]
            records: list[ResolvedRecord] = []
            for position, row in enumerate(sheet.iter_rows(values_only=True)):
                if position == 0 or not row or not row[0]:
                    continue
                records.append(
                    ResolvedRecord(
                        task_name=str(row[0]),
                        course=str(row[1]) if row[1] else None,
                        deadline_iso=str(row[2]) if row[2] else None,
                        status=str(row[3] or "uncertain"),
                        source=str(row[4] or ""),
                        confidence=float(row[5] or 0.0),
                        evidence_ids=[item for item in str(row[6] or "").split("; ") if item],
                        updated_at=str(row[7]) if row[7] else None,
                    )
                )
            return records
        finally:
            workbook.close()

    def audit_sheet_entries(self) -> list[dict]:
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            if "Audit Log" not in workbook.sheetnames:
                return []
            sheet = workbook["Audit Log"]
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                return []
            header = [str(value or "") for value in rows[0]]
            return [dict(zip(header, row)) for row in rows[1:]]
        finally:
            workbook.close()
