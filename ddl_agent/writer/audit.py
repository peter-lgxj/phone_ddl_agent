"""审计日志：独立 CSV 边车文件，任何写入器都共用。"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ..settings import AUDIT_COLUMNS, TZ


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, events: list[dict], timestamp: datetime | None = None) -> None:
        # 即使本次没有任何变更，也落一个只有表头的审计文件，保证每次写入都可追溯
        if not events and self.path.exists():
            return
        stamp = (timestamp or datetime.now(TZ)).isoformat(timespec="seconds")
        new_file = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(AUDIT_COLUMNS)
            for event in events:
                writer.writerow([
                    event.get("timestamp") or stamp,
                    event.get("action", ""),
                    event.get("task_name", ""),
                    event.get("old_value", ""),
                    event.get("new_value", ""),
                    event.get("reason", ""),
                ])

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
