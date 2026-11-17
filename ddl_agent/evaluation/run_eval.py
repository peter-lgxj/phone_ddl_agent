"""离线评测：同一数据集上跑多步 agent 与单次直出 baseline，输出报告。

用法：
    python -m ddl_agent.evaluation.run_eval --split test --limit 10
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from ..connectors.local_chat import LocalChatConnector
from ..connectors.local_eml import LocalEmlConnector
from ..dataset.generator import load_samples, write_dataset
from ..pipeline import DdlAgent
from ..planner import QueryPlanner
from ..retrieval import RetrievalOrchestrator
from ..writer.xlsx_writer import XlsxWriter
from .baseline import SinglePassBaseline
from .metrics import evaluate_dataset, evaluate_sample, validate_document


def materialize_sources(sample: dict, root: Path) -> list:
    """把样本里的消息写成真实连接器能读的 .eml 目录 + 聊天归档。"""
    root.mkdir(parents=True, exist_ok=True)
    mail_dir = root / "mail"
    mail_dir.mkdir(exist_ok=True)
    chat_items: list[dict] = []

    for message in sample["messages"]:
        if message["source_type"] == "official_email":
            _write_eml(message, mail_dir)
        else:
            chat_items.append({
                "message_id": message["message_id"],
                "sender": message.get("sender", "unknown"),
                "role": message.get("role", "student"),
                "timestamp": message["published_at"],
                "text": message.get("body") or message.get("text") or "",
                "subject": message.get("subject", ""),
                "source_type": message["source_type"],
            })

    sources: list = []
    if mail_dir.glob("*.eml"):
        sources.append(LocalEmlConnector("outlook", mail_dir, "official_email"))
    if chat_items:
        chat_path = root / "chat.json"
        chat_path.write_text(json.dumps({"messages": chat_items}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        sources.append(LocalChatConnector("course-chat", chat_path, "student_chat"))
    return sources


def _write_eml(message: dict, directory: Path) -> None:
    email = EmailMessage()
    email["Subject"] = message.get("subject", "")
    email["From"] = message.get("sender", "unknown")
    email["To"] = "student@connect.hku.hk"
    email["Date"] = format_datetime(datetime.fromisoformat(message["published_at"]))
    email["Message-ID"] = f"<{message['message_id']}@synthetic.local>"
    email.set_content(message.get("body") or message.get("text") or "")
    (directory / f"{message['message_id']}.eml").write_bytes(email.as_bytes())


def run_one(sample: dict, work_root: Path) -> dict:
    now = datetime.fromisoformat(sample["now"])
    sample_dir = work_root / sample["sample_id"]
    sources = materialize_sources(sample, sample_dir / "sources")

    document = sample_dir / "deadlines.xlsx"
    agent = DdlAgent(sources=sources, writer=XlsxWriter(document), now=now)
    agent_result = agent.run(sample["query"], sample.get("source_config", ""))
    agent_metrics = evaluate_sample(agent_result.records, sample["ground_truth"])
    doc_check = validate_document(document)

    plan = QueryPlanner().plan(sample["query"], sources, now)
    orchestrator = RetrievalOrchestrator(sources, now=now)
    raw_messages = orchestrator.fetch(plan)
    baseline_records, baseline_trace = SinglePassBaseline().run(raw_messages, now=now)
    baseline_metrics = evaluate_sample(baseline_records, sample["ground_truth"])

    return {
        "sample_id": sample["sample_id"],
        "tier": sample["tier"],
        "split": sample["split"],
        "query": sample["query"],
        "agent": {
            **agent_metrics,
            "trace": agent_result.trace,
            "write_ok": bool(agent_result.write_result and agent_result.write_result.ok),
            "document": doc_check,
            "warnings": agent_result.warnings,
        },
        "baseline": {
            **baseline_metrics,
            "trace": baseline_trace,
        },
    }


def _efficiency(entries: list[dict], key: str) -> dict:
    traces = [entry[key]["trace"] for entry in entries]
    count = len(traces) or 1
    return {
        "avg_seconds": round(sum(t["elapsed_seconds"] for t in traces) / count, 4),
        "llm_calls": sum(t["llm_calls"] for t in traces),
        "tokens_estimate": sum(t["tokens_estimate"] for t in traces),
        "cost_usd": round(sum(t["cost_usd"] for t in traces), 6),
    }


def summarize(entries: list[dict]) -> dict:
    agent_metrics = evaluate_dataset([entry["agent"] for entry in entries])
    baseline_metrics = evaluate_dataset([entry["baseline"] for entry in entries])
    valid_docs = sum(1 for entry in entries if entry["agent"]["document"]["ok"])
    issues = [
        f"{entry['sample_id']}: {issue}"
        for entry in entries
        for issue in entry["agent"]["document"]["issues"]
    ]
    return {
        "samples": len(entries),
        "agent": {
            **agent_metrics,
            "efficiency": _efficiency(entries, "agent"),
            "document_valid_rate": round(valid_docs / len(entries), 4) if entries else 0.0,
            "document_issues": issues,
            "write_failures": sum(1 for entry in entries if not entry["agent"]["write_ok"]),
        },
        "baseline": {
            **baseline_metrics,
            "efficiency": _efficiency(entries, "baseline"),
        },
    }


def _render_report(summary: dict, dataset: str, split: str) -> str:
    lines = [
        "# DDL Agent 离线评测报告",
        "",
        f"- 数据集：`{dataset}`",
        f"- 划分：`{split}`，样本数：{summary['samples']}",
        "- Agent 与 baseline 使用完全相同的原始消息；Agent 默认规则实现，不调用 LLM。",
        "",
        "## 总体指标",
        "",
        "| 指标 | Agent | 单次直出 baseline |",
        "| --- | --- | --- |",
        f"| 精确匹配 | {summary['agent']['exact_match']:.3f} | {summary['baseline']['exact_match']:.3f} |",
        f"| ±1 天模糊匹配 | {summary['agent']['fuzzy_match']:.3f} | {summary['baseline']['fuzzy_match']:.3f} |",
        f"| 召回 | {summary['agent']['recall']:.3f} | {summary['baseline']['recall']:.3f} |",
        f"| 重复率 | {summary['agent']['duplicate_rate']:.3f} | {summary['baseline']['duplicate_rate']:.3f} |",
        f"| 冲突解决正确率 | {_fmt(summary['agent']['conflict_accuracy'])} | "
        f"{_fmt(summary['baseline']['conflict_accuracy'])} |",
        f"| 输出行数 | {summary['agent']['predicted']} | {summary['baseline']['predicted']} |",
        f"| LLM 调用次数 | {summary['agent']['efficiency']['llm_calls']} | "
        f"{summary['baseline']['efficiency']['llm_calls']} |",
        f"| token 估算 | {summary['agent']['efficiency']['tokens_estimate']} | "
        f"{summary['baseline']['efficiency']['tokens_estimate']} |",
        f"| 成本估算（USD） | {summary['agent']['efficiency']['cost_usd']:.6f} | "
        f"{summary['baseline']['efficiency']['cost_usd']:.6f} |",
        f"| 平均耗时（秒/样本） | {summary['agent']['efficiency']['avg_seconds']:.4f} | "
        f"{summary['baseline']['efficiency']['avg_seconds']:.4f} |",
        "",
        f"文档结构校验通过率：**{summary['agent']['document_valid_rate']:.3f}**，"
        f"写入失败：{summary['agent']['write_failures']}。",
        "",
        "## 分难度档",
        "",
        "| 难度 | Agent 精确 | Agent 召回 | Agent 重复率 | Baseline 精确 | Baseline 召回 | Baseline 重复率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for tier, block in summary["by_tier"].items():
        agent, baseline = block["agent"], block["baseline"]
        lines.append(
            f"| {tier} | {agent['exact_match']:.3f} | {agent['recall']:.3f} | "
            f"{agent['duplicate_rate']:.3f} | {baseline['exact_match']:.3f} | "
            f"{baseline['recall']:.3f} | {baseline['duplicate_rate']:.3f} |"
        )

    if summary["agent"]["document_issues"]:
        lines += ["", "## 文档校验问题", ""]
        lines += [f"- {issue}" for issue in summary["agent"]["document_issues"][:20]]
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="DDL Agent 离线评测")
    parser.add_argument("--dataset", default="experiments/ddl_agent/dataset")
    parser.add_argument("--out", default="experiments/ddl_agent/results")
    parser.add_argument("--split", default="test", choices=["train", "val", "test", "all"])
    parser.add_argument("--tier", default=None, choices=["easy", "medium", "hard"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-tier", type=int, default=24)
    args = parser.parse_args()

    dataset_dir = Path(args.dataset)
    split = None if args.split == "all" else args.split
    samples = load_samples(dataset_dir, split=split, tier=args.tier)
    if not samples:
        print(f"数据集为空，先生成到 {dataset_dir}")
        write_dataset(dataset_dir, args.per_tier)
        samples = load_samples(dataset_dir, split=split, tier=args.tier)
    if args.limit:
        samples = samples[: args.limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_root = out_dir / "work"

    entries = [run_one(sample, work_root) for sample in samples]

    overall = summarize(entries)
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        by_tier[entry["tier"]].append(entry)
    overall["by_tier"] = {tier: summarize(group) for tier, group in by_tier.items()}
    overall["per_sample"] = [
        {
            "sample_id": entry["sample_id"],
            "tier": entry["tier"],
            "agent": {key: entry["agent"][key] for key in (
                "exact", "fuzzy", "matched", "predicted", "duplicate_rows",
                "conflict_total", "conflict_correct", "missed_tasks")},
            "baseline": {key: entry["baseline"][key] for key in (
                "exact", "fuzzy", "matched", "predicted", "duplicate_rows",
                "conflict_total", "conflict_correct", "missed_tasks")},
            "document_ok": entry["agent"]["document"]["ok"],
        }
        for entry in entries
    ]

    (out_dir / "summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _render_report(overall, args.dataset, args.split)
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps({key: overall[key] for key in ("samples", "agent", "baseline")},
                     ensure_ascii=False, indent=2))
    print(f"\n报告已写入：{out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
