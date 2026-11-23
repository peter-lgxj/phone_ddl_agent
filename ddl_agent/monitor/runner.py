"""单轮监控：增量抓取 → 抽取新消息 → 复用旧候选 → 冲突解决 → 写文档与导出。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..connectors.base import SourceError
from ..dedupe import Deduplicator
from ..factory import build_components
from ..llm.client import UsageTracker
from ..models import ResolvedRecord
from ..settings import TZ
from ..writer.xlsx_writer import XlsxWriter
from ..output import PhoneCalendarWriter, export_outputs
from .config import MonitorConfig, MonitorSource
from .cursor import CandidateStore, CursorStore, MessageStore
from .sources import MobileAppSource


@dataclass
class CycleResult:
    cycle_id: str
    started_at: str
    finished_at: str
    engine: str
    sources: list[dict] = field(default_factory=list)
    records: list[ResolvedRecord] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    write_ok: bool = False
    outputs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    status: str = "ok"
    sources_ok: int = 0

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "engine": self.engine,
            "status": self.status,
            "sources_ok": self.sources_ok,
            "sources": self.sources,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "write_ok": self.write_ok,
            "outputs": self.outputs,
            "errors": self.errors,
            "llm": self.llm,
            "records": [
                {
                    "task_name": record.task_name,
                    "course": record.course,
                    "deadline": record.deadline_iso,
                    "status": record.status,
                    "superseded_by": record.superseded_by,
                    "conflicts": record.conflicts,
                }
                for record in self.records
            ],
        }


def run_cycle(config: MonitorConfig, config_path: str | Path | None = None,
              connector_factory=None, engine: str = "auto",
              now: datetime | None = None,
              out_dir: str | Path | None = None,
              state_dir: str | Path | None = None,
              tracker: UsageTracker | None = None) -> CycleResult:
    started = now or datetime.now(TZ)
    cycle_id = started.strftime("%Y%m%dT%H%M%S")
    out = Path(out_dir or config.output_dir)
    state = Path(state_dir or config.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    tracker = tracker or UsageTracker()
    usage_before = tracker.snapshot()
    components = build_components(engine, tracker)

    cursors = CursorStore(state / "cursors.json")
    message_store = MessageStore(state / "messages.jsonl")
    candidate_store = CandidateStore(state / "candidates.jsonl")

    result = CycleResult(
        cycle_id=cycle_id,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=started.isoformat(timespec="seconds"),
        engine=components["engine"],
    )

    for source in config.sources:
        if not source.enabled:
            continue
        try:
            entry = _sync_source(
                source, connector_factory, components["extractor"], now=started,
                cursors=cursors, messages=message_store, candidates=candidate_store,
                errors=result.errors,
            )
        except Exception as exc:  # noqa: BLE001 - 单来源异常不能拖垮整轮
            message = f"{source.id}: {type(exc).__name__}: {exc}"
            result.errors.append(message)
            entry = _failed_entry(source, message)
        result.sources.append(entry)

    enabled = [source for source in config.sources if source.enabled]
    result.sources_ok = sum(1 for entry in result.sources if entry.get("error") is None)
    if not enabled:
        result.errors.append("没有启用的检索目标")
    if result.sources_ok == 0:
        # 全部来源失败：不写 xlsx / data.json / deadlines.ics / calendar.html，
        # 避免把“空结果”伪装成成功；周期 JSON 仍写出，明确标注 failed。
        result.status = "failed"
        result.write_ok = False
        result.finished_at = datetime.now(TZ).isoformat(timespec="seconds")
        result.llm = tracker.since(usage_before)
        _write_cycle(out, cycle_id, result)
        return result

    clusters = Deduplicator().cluster(candidate_store.all())
    outcome = components["resolver"].resolve(clusters, plan=None, retrieval=None)
    result.records = outcome.records

    try:
        writer = XlsxWriter(out / "deadlines.xlsx")
        write_result = writer.upsert(result.records, timestamp=started)
        result.write_ok = write_result.ok
        result.inserted = write_result.inserted
        result.updated = write_result.updated
        result.skipped = write_result.skipped
        if not write_result.ok:
            result.errors.append(f"写入失败：{write_result.error}")
    except Exception as exc:  # noqa: BLE001 - 写 xlsx 失败仍尽量导出其它产物
        result.write_ok = False
        result.errors.append(f"写入失败：{type(exc).__name__}: {exc}")

    try:
        result.outputs = [str(path) for path in export_outputs(result.records, out, started)]
    except Exception as exc:  # noqa: BLE001
        result.outputs = []
        result.write_ok = False
        result.errors.append(f"导出失败：{type(exc).__name__}: {exc}")

    phone = PhoneCalendarWriter(enabled=config.write_phone_calendar)
    try:
        phone_result = phone.write(result.records)
        if phone_result.get("skipped"):
            result.sources.append({"phone_calendar": phone_result})
    except SourceError as exc:
        result.errors.append(f"手机日历写入未执行：{exc}")

    result.finished_at = datetime.now(TZ).isoformat(timespec="seconds")
    result.llm = tracker.since(usage_before)
    result.status = "partial" if result.errors else "ok"
    _write_cycle(out, cycle_id, result)
    return result


def _failed_entry(source: MonitorSource, message: str) -> dict:
    """来源级异常也保留一条可审计的来源记录。"""
    return {
        "source_id": source.id,
        "app": source.app,
        "target": source.target,
        "baseline": False,
        "fetched": 0,
        "new_messages": 0,
        "new_candidates": 0,
        "error": message,
        "partial": False,
        "last_status": "error",
    }


def _write_cycle(out: Path, cycle_id: str, result: CycleResult) -> None:
    cycles_dir = out / "cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    (cycles_dir / f"{cycle_id}.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _sync_source(source: MonitorSource, connector_factory, extractor,
                 now: datetime, cursors: CursorStore, messages: MessageStore,
                 candidates: CandidateStore, errors: list[str]) -> dict:
    cursor_before = cursors.get(source.id)
    entry = {
        "source_id": source.id,
        "app": source.app,
        "target": source.target,
        "baseline": not cursor_before.baseline_done,
        "fetched": 0,
        "new_messages": 0,
        "new_candidates": 0,
        "error": None,
        "partial": False,
        "last_status": None,
    }

    connector = connector_factory(source) if connector_factory is not None else MobileAppSource(
        source, now=now
    )
    try:
        fetched = connector.fetch()
    except SourceError as exc:
        entry["error"] = str(exc)
        errors.append(f"{source.id}: {exc}")
        return entry

    # 用户配置的时间范围：只保留最近 N 天内的消息；
    # 读屏未解析出发布时间的消息不能按时间丢弃，保留并交给抽取器标记复核
    cutoff = now - timedelta(days=max(1, source.lookback_days))
    fetched = [
        message for message in fetched
        if message.published_at is None or message.published_at >= cutoff
    ]

    entry["partial"] = bool(getattr(connector, "partial", False))
    entry["last_status"] = getattr(connector, "last_status", None)
    entry["fetched"] = len(fetched)
    fresh = messages.add(fetched)
    entry["new_messages"] = len(fresh)

    new_candidates = extractor.extract(fresh, now=now) if fresh else []
    candidates.add(new_candidates)
    entry["new_candidates"] = len(new_candidates)

    if entry["partial"]:
        # 部分结果不能把游标推到本轮：未读到的旧消息下一轮还要能再取
        cursor = cursors.get(source.id)
    else:
        cursor = cursors.advance(source.id, fetched)
    entry["last_timestamp"] = cursor.last_timestamp
    entry["last_message_id"] = cursor.last_message_id
    return entry
