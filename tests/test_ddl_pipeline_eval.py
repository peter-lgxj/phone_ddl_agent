"""DDL Agent：连接器、流水线、数据集、指标与离线评测。"""

from datetime import datetime

import pytest

from ddl_agent.connectors.base import SourceNotConfigured
from ddl_agent.connectors.graph_mail import GraphMailConnector
from ddl_agent.connectors.local_chat import LocalChatConnector
from ddl_agent.dataset.generator import generate_samples, load_samples, write_dataset
from ddl_agent.evaluation.baseline import SinglePassBaseline
from ddl_agent.evaluation.metrics import evaluate_dataset, evaluate_sample, validate_document
from ddl_agent.evaluation.run_eval import materialize_sources, run_one, summarize
from ddl_agent.models import ResolvedRecord, RetrievalPlan
from ddl_agent.pipeline import DdlAgent
from ddl_agent.planner import QueryPlanner
from ddl_agent.writer.xlsx_writer import XlsxWriter

NOW = datetime.fromisoformat("2026-11-16T09:00:00+08:00")


def _sample(sample_id: str) -> dict:
    samples = generate_samples(24)
    return next(sample for sample in samples if sample["sample_id"] == sample_id)


def _record(task: str, deadline: str | None, status: str = "confirmed") -> ResolvedRecord:
    return ResolvedRecord(
        task_name=task,
        course="COMP7608",
        deadline_iso=deadline,
        status=status,
        source="outlook (official_email)",
        evidence_ids=[f"{task}-e"],
        confidence=0.9,
    )


# ---------------------------------------------------------------- 连接器
def test_materialize_sources_are_readable(tmp_path):
    sample = _sample("easy-001")
    sources = materialize_sources(sample, tmp_path / "src")
    plan = QueryPlanner().plan(sample["query"], sources, NOW)
    messages = sources[0].fetch(plan)
    assert len(messages) == 1
    assert "Assignment" in messages[0].body
    assert messages[0].source_type == "official_email"


def test_local_chat_connector_maps_roles(tmp_path):
    chat_path = tmp_path / "chat.json"
    chat_path.write_text(
        """{"messages": [
            {"sender": "TA", "role": "ta", "timestamp": "2026-11-15T10:00:00+08:00",
             "text": "Assignment 1 deadline is 2026-12-01 23:59"},
            {"sender": "Bob", "role": "student", "timestamp": "2026-11-15T11:00:00+08:00",
             "text": "thanks"}
        ]}""",
        encoding="utf-8",
    )
    messages = LocalChatConnector("chat", chat_path).fetch(RetrievalPlan(query=""))
    assert [message.source_type for message in messages] == ["instructor_chat", "student_chat"]


def test_graph_connector_without_token_is_explicit():
    with pytest.raises(SourceNotConfigured):
        GraphMailConnector().fetch(RetrievalPlan(query=""))


# ---------------------------------------------------------------- 流水线
def test_pipeline_dedupes_and_writes_document(tmp_path):
    sample = _sample("medium-001")  # 邮件 + 群聊引用同一任务，另有 HW0 干扰项
    sources = materialize_sources(sample, tmp_path / "src")
    document = tmp_path / "deadlines.xlsx"

    agent = DdlAgent(sources=sources, writer=XlsxWriter(document), now=NOW)
    result = agent.run(sample["query"], sample["source_config"])

    target = next(record for record in result.records
                  if "assignment" in record.task_name.lower())
    assert target.deadline_iso == sample["ground_truth"][0]["deadline_iso"]
    assert target.status == "confirmed"
    # HW1/作业一 变体应合并成一行，而不是留下两份
    assert sum(1 for record in result.records
               if "assignment" in record.task_name.lower()) == 1
    assert result.write_result.ok
    assert validate_document(document)["ok"]
    assert result.trace["llm_calls"] == 0
    assert result.trace["message_count"] == 2


def test_rule_pipeline_does_not_invent_missing_deadline(tmp_path):
    sample = _sample("medium-002")  # 只有一句“还没定”，没有可解析日期
    sources = materialize_sources(sample, tmp_path / "src")
    agent = DdlAgent(sources=sources, now=NOW)
    result = agent.run(sample["query"], sample["source_config"])
    # 规则版宁可漏掉，也不能编一个截止时间
    assert all(record.status != "confirmed" for record in result.records)


def test_planner_extracts_course_and_time_window():
    plan = QueryPlanner().plan("COMP7608 下周的 DDL 有哪些", [], NOW)
    assert plan.course_filters == ["COMP7608"]
    assert plan.date_from is not None
    assert plan.date_from.isoformat() == "2026-11-23T00:00:00+08:00"


# ---------------------------------------------------------------- 数据集
def test_generator_covers_tiers_and_splits():
    samples = generate_samples(24)
    assert len(samples) == 72
    for tier in ("easy", "medium", "hard"):
        assert sum(1 for sample in samples if sample["tier"] == tier) == 24
    assert {sample["split"] for sample in samples} == {"train", "val", "test"}
    assert all(sample["ground_truth"] for sample in samples)
    assert any(sample["ground_truth"][0]["conflict_expected"] for sample in samples)


def test_generator_is_deterministic():
    assert generate_samples(20) == generate_samples(20)


def test_dataset_write_and_load(tmp_path):
    index = write_dataset(tmp_path, per_tier=20)
    assert index["counts"]["test"] == {"easy": 4, "medium": 4, "hard": 4}
    assert len(load_samples(tmp_path, split="test")) == 12
    assert len(load_samples(tmp_path, tier="hard")) == 20


# ---------------------------------------------------------------- 指标
def test_metrics_exact_fuzzy_and_duplicates():
    records = [
        _record("Assignment 1", "2026-12-05T23:59:00+08:00"),
        _record("Assignment 2", "2026-12-06T00:30:00+08:00"),
        _record("Assignment 2", "2026-12-06T00:30:00+08:00"),
    ]
    ground_truth = [
        {"task_name": "Assignment 1", "course": "COMP7608",
         "deadline_iso": "2026-12-05T23:59:00+08:00", "status": "confirmed"},
        {"task_name": "Assignment 2", "course": "COMP7608",
         "deadline_iso": "2026-12-05T23:59:00+08:00", "status": "confirmed"},
        {"task_name": "Assignment 3", "course": "COMP7608",
         "deadline_iso": None, "status": "missing_deadline"},
    ]
    metrics = evaluate_sample(records, ground_truth)
    assert metrics["exact"] == 1
    assert metrics["fuzzy"] == 2
    assert metrics["matched"] == 2
    assert metrics["duplicate_rows"] == 1
    assert metrics["duplicate_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["missed_tasks"] == ["Assignment 3"]


def test_metrics_recall_counts_matched_without_deadline():
    records = [_record("Assignment 1", None, status="uncertain")]
    ground_truth = [{"task_name": "Assignment 1", "course": "COMP7608",
                     "deadline_iso": None, "status": "uncertain"}]
    sample = evaluate_sample(records, ground_truth)
    dataset = evaluate_dataset([sample])
    assert dataset["exact_match"] == 0.0
    assert dataset["recall"] == 1.0


def test_validate_document_detects_missing_audit(tmp_path):
    sample = _sample("easy-001")
    sources = materialize_sources(sample, tmp_path / "src")
    document = tmp_path / "deadlines.xlsx"
    DdlAgent(sources=sources, writer=XlsxWriter(document), now=NOW).run(
        sample["query"], sample["source_config"]
    )
    assert validate_document(document)["ok"]

    (tmp_path / "deadlines.xlsx.audit.csv").unlink()
    result = validate_document(document)
    assert result["ok"] is False
    assert "缺少审计日志文件" in result["issues"]


# ---------------------------------------------------------------- baseline / 端到端评测
def test_baseline_uses_one_pass(tmp_path):
    sample = _sample("easy-001")
    sources = materialize_sources(sample, tmp_path / "src")
    plan = QueryPlanner().plan(sample["query"], sources, NOW)
    messages = sources[0].fetch(plan)

    records, trace = SinglePassBaseline().run(messages, now=NOW)
    assert records
    assert trace["llm_calls"] == 1
    assert trace["tokens_estimate"] > 0


def test_run_one_produces_valid_document(tmp_path):
    sample = _sample("easy-001")
    entry = run_one(sample, tmp_path)  # 不传 client，等价于离线规则路径
    assert entry["agent_rules"]["matched"] == 1
    assert entry["agent_rules"]["document"]["ok"] is True
    assert entry["baseline"]["predicted"] >= 1

    summary = summarize([entry])
    assert summary["samples"] == 1
    assert summary["agent_rules"]["recall"] == 1.0
    assert summary["agent_rules"]["document_valid_rate"] == 1.0
