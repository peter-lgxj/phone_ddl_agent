"""写入器公共逻辑：合并行、失败重试一次、保留上一版、写审计。"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..dedupe import normalize_task_name
from ..extract.rules import normalize_course
from ..models import ResolvedRecord, WriteResult
from ..settings import TZ
from .audit import AuditLog


def record_key(record: ResolvedRecord) -> tuple[str, str]:
    return (normalize_course(record.course) or "", normalize_task_name(record.task_name))


@dataclass
class MergeOutcome:
    rows: list[ResolvedRecord]
    inserted: int
    updated: int
    skipped: int
    events: list[dict]


def merge_records(existing: list[ResolvedRecord], incoming: list[ResolvedRecord],
                  timestamp: datetime) -> MergeOutcome:
    """按 ``(course, 归一化任务名)`` 合并：命中改行，否则插行。"""
    rows = list(existing)
    index = {record_key(row): position for position, row in enumerate(rows)}
    inserted = updated = skipped = 0
    events: list[dict] = []
    stamp = timestamp.isoformat(timespec="seconds")

    for record in incoming:
        record = replace(record, updated_at=stamp)
        key = record_key(record)
        if key not in index:
            rows.append(record)
            index[key] = len(rows) - 1
            inserted += 1
            events.append({
                "action": "insert",
                "task_name": record.task_name,
                "old_value": "",
                "new_value": _cell(record),
                "reason": record.resolution_note,
                "timestamp": stamp,
            })
            continue

        old = rows[index[key]]
        if not _should_update(old, record):
            skipped += 1
            continue

        rows[index[key]] = record
        updated += 1
        events.append({
            "action": "update",
            "task_name": record.task_name,
            "old_value": _cell(old),
            "new_value": _cell(record),
            "reason": record.resolution_note,
            "timestamp": stamp,
        })

    return MergeOutcome(rows, inserted, updated, skipped, events)


def _should_update(old: ResolvedRecord, new: ResolvedRecord) -> bool:
    # 新结果缺截止时间时，不能把已有有效日期覆盖掉
    if new.status == "missing_deadline" and old.deadline_iso:
        return False
    # 低置信度的 uncertain 不能覆盖已确认记录
    if new.status == "uncertain" and old.status == "confirmed":
        return False
    return (
        old.task_name,
        old.course,
        old.deadline_iso,
        old.status,
        old.source,
        round(old.confidence, 4),
    ) != (
        new.task_name,
        new.course,
        new.deadline_iso,
        new.status,
        new.source,
        round(new.confidence, 4),
    )


def _row_still_supported(row: ResolvedRecord, keep_evidence_ids: set[str]) -> bool:
    """旧行至少要有一条仍有效的证据候选；没有证据字段的行保守保留。"""
    if not row.evidence_ids:
        return True
    return any(item in keep_evidence_ids for item in row.evidence_ids)


def _cell(record: ResolvedRecord) -> str:
    if record.deadline_iso:
        return f"{record.task_name} | {record.course or '-'} | {record.deadline_iso} | {record.status}"
    return f"{record.task_name} | {record.course or '-'} | - | {record.status}"


class DeadlineWriter(ABC):
    """所有写入器的统一基类：事务式写入 + 审计。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.audit = AuditLog(self.audit_path)

    @property
    def audit_path(self) -> Path:
        return Path(f"{self.path}.audit.csv")

    # ------------------------------------------------------------------ #
    def read(self) -> list[ResolvedRecord]:
        if not self.path.exists():
            return []
        return self._read()

    def upsert(self, records: list[ResolvedRecord],
               timestamp: datetime | None = None,
               keep_evidence_ids: set[str] | None = None) -> WriteResult:
        stamp = timestamp or datetime.now(TZ)
        existed = self.path.exists()
        backup = Path(f"{self.path}.bak")
        if existed:
            shutil.copy2(self.path, backup)

        try:
            existing = self.read()
        except Exception as exc:
            return WriteResult(ok=False, path=str(self.path), error=f"读取现有文档失败：{exc}")

        if keep_evidence_ids is not None:
            # 已删除来源 / 已改目标来源的旧行不再有有效证据，必须一并移除
            existing = [row for row in existing
                        if _row_still_supported(row, keep_evidence_ids)]

        outcome = merge_records(existing, records, stamp)

        try:
            self._write_with_retry(outcome.rows, outcome.events, stamp)
            self.audit.append(outcome.events, timestamp=stamp)
        except Exception as exc:
            self._restore(backup, existed)
            try:
                self.audit.append([{
                    "action": "rollback",
                    "task_name": "",
                    "old_value": "",
                    "new_value": "",
                    "reason": f"写入失败已回滚：{exc}",
                    "timestamp": stamp.isoformat(timespec="seconds"),
                }], timestamp=stamp)
            except OSError:
                pass
            return WriteResult(
                ok=False,
                rolled_back=True,
                path=str(self.path),
                error=str(exc),
            )

        if existed and backup.exists():
            backup.unlink()
        return WriteResult(
            ok=True,
            inserted=outcome.inserted,
            updated=outcome.updated,
            skipped=outcome.skipped,
            path=str(self.path),
        )

    # ------------------------------------------------------------------ #
    def _write_with_retry(self, rows: list[ResolvedRecord], events: list[dict],
                          timestamp: datetime) -> None:
        last_error: Exception | None = None
        for _attempt in range(2):  # 失败后重试一次
            try:
                self._write(rows, events, timestamp)
                return
            except Exception as exc:  # noqa: BLE001 - 需要捕获任意写入异常后回滚
                last_error = exc
        assert last_error is not None
        raise last_error

    def _restore(self, backup: Path, existed: bool) -> None:
        try:
            if existed and backup.exists():
                os.replace(backup, self.path)
            elif self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    @abstractmethod
    def _write(self, rows: list[ResolvedRecord], events: list[dict],
               timestamp: datetime) -> None:
        raise NotImplementedError

    @abstractmethod
    def _read(self) -> list[ResolvedRecord]:
        raise NotImplementedError
