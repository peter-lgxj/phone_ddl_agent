"""评测指标：精确/模糊匹配、召回、冲突解决、重复率、文档结构校验。"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from ..dedupe import normalize_task_name, task_similarity
from ..models import ResolvedRecord
from ..settings import AUDIT_COLUMNS, DEADLINE_COLUMNS

_FUZZY_TOLERANCE = timedelta(days=1)
_NAME_SIMILARITY = 0.8
_VALID_STATUS = {"confirmed", "uncertain", "missing_deadline"}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _names_match(left: str, right: str) -> bool:
    a, b = normalize_task_name(left), normalize_task_name(right)
    return a == b or task_similarity(a, b) >= _NAME_SIMILARITY


def _courses_match(left: str | None, right: str | None) -> bool:
    if left and right:
        return left.upper() == right.upper()
    return True


def evaluate_sample(records: list[ResolvedRecord], ground_truth: list[dict]) -> dict:
    used: set[int] = set()
    exact = fuzzy = 0
    conflict_total = conflict_correct = 0
    matched_gt: list[str] = []
    missed: list[str] = []

    for truth in ground_truth:
        truth_deadline = _parse_iso(truth.get("deadline_iso"))
        match_index = _best_match(records, used, truth, truth_deadline)
        if match_index is None:
            missed.append(truth["task_name"])
            if truth.get("conflict_expected"):
                conflict_total += 1
            continue

        used.add(match_index)
        record = records[match_index]
        matched_gt.append(truth["task_name"])
        record_deadline = _parse_iso(record.deadline_iso)
        if truth_deadline and record_deadline and truth_deadline == record_deadline:
            exact += 1
            fuzzy += 1
        elif truth_deadline and record_deadline and abs(truth_deadline - record_deadline) <= _FUZZY_TOLERANCE:
            fuzzy += 1

        if truth.get("conflict_expected"):
            conflict_total += 1
            if _conflict_is_correct(truth, truth_deadline, record, record_deadline):
                conflict_correct += 1

    duplicate_rows = 0
    unknown_rows = 0
    for index, record in enumerate(records):
        if index in used:
            continue
        if any(_names_match(record.task_name, truth["task_name"]) and
               _courses_match(record.course, truth.get("course")) for truth in ground_truth):
            duplicate_rows += 1
        else:
            unknown_rows += 1

    total_gt = len(ground_truth)
    return {
        "gt": total_gt,
        "predicted": len(records),
        "exact": exact,
        "fuzzy": fuzzy,
        "matched": len(matched_gt),
        "recall": round(len(matched_gt) / total_gt, 4) if total_gt else 0.0,
        "duplicate_rows": duplicate_rows,
        "unknown_rows": unknown_rows,
        "duplicate_rate": round(duplicate_rows / len(records), 4) if records else 0.0,
        "conflict_total": conflict_total,
        "conflict_correct": conflict_correct,
        "matched_tasks": matched_gt,
        "missed_tasks": missed,
    }


def _best_match(records: list[ResolvedRecord], used: set[int], truth: dict,
                truth_deadline: datetime | None) -> int | None:
    candidates = [
        index for index, record in enumerate(records)
        if index not in used
        and _names_match(record.task_name, truth["task_name"])
        and _courses_match(record.course, truth.get("course"))
    ]
    if not candidates:
        return None

    def score(index: int) -> tuple[int, float]:
        record = records[index]
        record_deadline = _parse_iso(record.deadline_iso)
        if truth_deadline and record_deadline and truth_deadline == record_deadline:
            return (3, 0.0)
        if truth_deadline and record_deadline and abs(truth_deadline - record_deadline) <= _FUZZY_TOLERANCE:
            return (2, abs((truth_deadline - record_deadline).total_seconds()))
        return (1, 0.0)

    return max(candidates, key=score)


def _conflict_is_correct(truth: dict, truth_deadline: datetime | None,
                         record: ResolvedRecord, record_deadline: datetime | None) -> bool:
    if truth.get("status") == "uncertain" or truth_deadline is None:
        return record.status in ("uncertain", "missing_deadline")
    return record_deadline == truth_deadline


def evaluate_dataset(results: list[dict]) -> dict:
    """把逐样本指标聚合成数据集级指标。"""
    gt = sum(item["gt"] for item in results)
    predicted = sum(item["predicted"] for item in results)
    exact = sum(item["exact"] for item in results)
    fuzzy = sum(item["fuzzy"] for item in results)
    matched = sum(item["matched"] for item in results)
    duplicate_rows = sum(item["duplicate_rows"] for item in results)
    conflict_total = sum(item["conflict_total"] for item in results)
    conflict_correct = sum(item["conflict_correct"] for item in results)

    return {
        "samples": len(results),
        "gt": gt,
        "predicted": predicted,
        "exact_match": round(exact / gt, 4) if gt else 0.0,
        "fuzzy_match": round(fuzzy / gt, 4) if gt else 0.0,
        # 召回：ground truth 任务中最终出现在文档里的比例（不看时间是否正确）
        "recall": round(matched / gt, 4) if gt else 0.0,
        "duplicate_rate": round(duplicate_rows / predicted, 4) if predicted else 0.0,
        "conflict_accuracy": (
            round(conflict_correct / conflict_total, 4) if conflict_total else None
        ),
        "conflict_samples": conflict_total,
        "unknown_rows": sum(item["unknown_rows"] for item in results),
    }


def validate_document(path: str | Path) -> dict:
    """结构化校验输出文档：列齐全、必填非空、ISO 合法、审计存在。"""
    document = Path(path)
    issues: list[str] = []
    if not document.exists():
        return {"ok": False, "issues": ["文档不存在"], "rows": 0}

    suffix = document.suffix.lower()
    try:
        if suffix == ".xlsx":
            header, rows = _read_xlsx(document)
        elif suffix == ".csv":
            header, rows = _read_csv(document)
        elif suffix in (".md", ".markdown"):
            header, rows = _read_markdown(document)
        else:
            return {"ok": False, "issues": [f"不支持的文档类型：{suffix}"], "rows": 0}
    except Exception as exc:  # noqa: BLE001 - 校验器需要把任何解析异常当成结构问题
        return {"ok": False, "issues": [f"读取文档失败：{exc}"], "rows": 0}

    if header != DEADLINE_COLUMNS:
        issues.append(f"表头不符：{header}")

    for position, row in enumerate(rows, start=1):
        if len(row) != len(DEADLINE_COLUMNS):
            issues.append(f"第 {position} 行列数错误：{len(row)}")
            continue
        task_name, _course, deadline, status, source, all_sources, confidence, _evidence, _updated = row
        if not str(task_name).strip():
            issues.append(f"第 {position} 行任务名为空")
        if status not in _VALID_STATUS:
            issues.append(f"第 {position} 行状态非法：{status}")
        if status == "confirmed" and _parse_iso(deadline) is None:
            issues.append(f"第 {position} 行截止时间不是合法 ISO-8601：{deadline}")
        if not str(source).strip():
            issues.append(f"第 {position} 行来源为空")
        if not str(all_sources).strip():
            issues.append(f"第 {position} 行原始来源列表为空")
        try:
            value = float(confidence)
            if not 0 <= value <= 1:
                issues.append(f"第 {position} 行置信度越界：{value}")
        except (TypeError, ValueError):
            issues.append(f"第 {position} 行置信度不是数字：{confidence}")

    audit_path = Path(f"{document}.audit.csv")
    if not audit_path.exists():
        issues.append("缺少审计日志文件")
    else:
        with audit_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            audit_header = next(reader, [])
            audit_rows = sum(1 for _ in reader)
        if audit_header != AUDIT_COLUMNS:
            issues.append(f"审计表头不符：{audit_header}")
        if audit_rows == 0 and rows:
            issues.append("审计日志为空")

    return {"ok": not issues, "issues": issues, "rows": len(rows)}


def _read_xlsx(path: Path) -> tuple[list[str], list[list[str]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Deadlines"] if "Deadlines" in workbook.sheetnames else workbook.active
        values = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    if not values:
        return [], []
    header = [str(value or "") for value in values[0]]
    rows = [[str(cell) if cell is not None else "" for cell in row] for row in values[1:]]
    return header, rows


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        return header, [row for row in reader]


def _read_markdown(path: Path) -> tuple[list[str], list[list[str]]]:
    rows: list[list[str]] = []
    header: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not header:
            header = cells
            continue
        if all(set(cell) <= {"-"} for cell in cells if cell):
            continue
        rows.append(cells)
    return header, rows
