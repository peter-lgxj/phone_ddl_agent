"""离线评测：同一批输入对比 Agent-LLM、Agent-规则、单次直出 baseline。

用法：
    python -m ddl_agent.evaluation.run_eval --split test
    LLM_ENABLED=0 python -m ddl_agent.evaluation.run_eval --split test   # 纯离线
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
from ..factory import build_agent, llm_enabled
from ..llm.client import LLMClient
from ..planner import QueryPlanner
from ..retrieval import RetrievalOrchestrator
from ..writer.xlsx_writer import XlsxWriter
from .baseline import LLMSinglePassBaseline, SinglePassBaseline
from .metrics import evaluate_dataset, evaluate_sample, validate_document

SYSTEMS = ("agent_llm", "agent_rules", "baseline")


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


def run_one(sample: dict, work_root: Path, client: LLMClient | None = None) -> dict:
    now = datetime.fromisoformat(sample["now"])
    sample_dir = work_root / sample["sample_id"]
    sources = materialize_sources(sample, sample_dir / "sources")

    entry: dict = {
        "sample_id": sample["sample_id"],
        "tier": sample["tier"],
        "split": sample["split"],
        "query": sample["query"],
    }

    for key, engine in (("agent_llm", "llm"), ("agent_rules", "rules")):
        # 没有可用 LLM 客户端时不发网络请求，两个 Agent 都走规则实现
        effective_engine = engine if (engine == "rules" or client is not None) else "rules"
        document = sample_dir / f"{key}.xlsx"
        agent, engine_name = build_agent(sources, writer=XlsxWriter(document), now=now,
                                         engine=effective_engine)
        result = agent.run(sample["query"], sample.get("source_config", ""))
        entry[key] = {
            **evaluate_sample(result.records, sample["ground_truth"]),
            "engine": engine_name,
            "trace": result.trace,
            "write_ok": bool(result.write_result and result.write_result.ok),
            "document": validate_document(document),
            "warnings": result.warnings,
            "records": [
                {"task_name": r.task_name, "course": r.course, "deadline_iso": r.deadline_iso,
                 "status": r.status, "confidence": r.confidence, "note": r.resolution_note}
                for r in result.records
            ],
        }

    plan = QueryPlanner().plan(sample["query"], sources, now)
    orchestrator = RetrievalOrchestrator(sources, now=now)
    raw_messages = orchestrator.fetch(plan)
    baseline = LLMSinglePassBaseline(client) if client is not None else SinglePassBaseline()
    base_records, base_trace = baseline.run(raw_messages, now=now)
    entry["baseline"] = {
        **evaluate_sample(base_records, sample["ground_truth"]),
        "engine": "llm_single_pass" if client is not None else "rule_single_pass",
        "trace": base_trace,
        "records": [
            {"task_name": r.task_name, "course": r.course, "deadline_iso": r.deadline_iso,
             "status": r.status, "note": r.resolution_note}
            for r in base_records
        ],
    }
    return entry


def _efficiency(entries: list[dict], key: str) -> dict:
    traces = [entry[key]["trace"] for entry in entries]
    count = len(traces) or 1
    return {
        "avg_seconds": round(sum(t["elapsed_seconds"] for t in traces) / count, 4),
        "avg_llm_seconds": round(sum(t.get("llm_seconds", 0.0) for t in traces) / count, 4),
        "llm_calls": sum(t["llm_calls"] for t in traces),
        "tokens_estimate": sum(t["tokens_estimate"] for t in traces),
        "cost_usd": round(sum(t["cost_usd"] for t in traces), 6),
    }


def summarize(entries: list[dict]) -> dict:
    summary: dict = {"samples": len(entries)}
    for key in SYSTEMS:
        block = evaluate_dataset([entry[key] for entry in entries])
        block["efficiency"] = _efficiency(entries, key)
        if key in ("agent_llm", "agent_rules"):
            valid_docs = sum(1 for entry in entries if entry[key]["document"]["ok"])
            block["document_valid_rate"] = round(valid_docs / len(entries), 4) if entries else 0.0
            block["document_issues"] = [
                f"{entry['sample_id']}: {issue}"
                for entry in entries for issue in entry[key]["document"]["issues"]
            ]
            block["write_failures"] = sum(1 for entry in entries if not entry[key]["write_ok"])
        summary[key] = block
    summary["engines"] = {
        key: sorted({entry[key]["engine"] for entry in entries}) for key in SYSTEMS
    }
    return summary


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _render_report(summary: dict, dataset: str, split: str) -> str:
    lines = [
        "# DDL Agent 离线评测报告（三方对比）",
        "",
        f"- 数据集：`{dataset}`",
        f"- 划分：`{split}`，样本数：{summary['samples']}",
        f"- 实际引擎：Agent-LLM={summary['engines']['agent_llm']}，"
        f"Agent-规则={summary['engines']['agent_rules']}，"
        f"baseline={summary['engines']['baseline']}",
        "- 三者使用完全相同的原始消息；baseline 为单次直出（一次模型调用，不去重、不核查冲突）。",
        "",
        "## 总体指标",
        "",
        "| 指标 | Agent-LLM | Agent-规则 | baseline 单次直出 |",
        "| --- | --- | --- | --- |",
    ]

    def row(label: str, key: str, fmt=lambda value: f"{value:.3f}"):
        values = []
        for system in SYSTEMS:
            value = summary[system][key]
            values.append(_fmt(value) if value is None else fmt(value))
        lines.append(f"| {label} | " + " | ".join(values) + " |")

    row("精确匹配", "exact_match")
    row("±1 天模糊匹配", "fuzzy_match")
    row("召回", "recall")
    row("重复率", "duplicate_rate")
    row("冲突解决正确率", "conflict_accuracy")
    row("输出行数", "predicted", lambda value: str(value))
    row("LLM 调用次数", "efficiency", lambda value: str(value["llm_calls"]))
    row("token 估算", "efficiency", lambda value: str(value["tokens_estimate"]))
    row("成本估算（USD）", "efficiency", lambda value: f"{value['cost_usd']:.6f}")
    row("平均耗时（秒/样本）", "efficiency", lambda value: f"{value['avg_seconds']:.3f}")
    row("平均 LLM 耗时（秒/样本）", "efficiency", lambda value: f"{value['avg_llm_seconds']:.3f}")

    lines += [
        "",
        f"文档结构校验通过率：Agent-LLM **{summary['agent_llm'].get('document_valid_rate', 0):.3f}**，"
        f"Agent-规则 **{summary['agent_rules'].get('document_valid_rate', 0):.3f}**；"
        f"写失败：{summary['agent_llm'].get('write_failures', 0)} / "
        f"{summary['agent_rules'].get('write_failures', 0)}。",
        "",
        "## 分难度档",
        "",
        "| 难度 | 系统 | 精确 | 召回 | 重复率 | 冲突正确率 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for tier, block in summary["by_tier"].items():
        for system in SYSTEMS:
            item = block[system]
            lines.append(
                f"| {tier} | {system} | {item['exact_match']:.3f} | {item['recall']:.3f} | "
                f"{item['duplicate_rate']:.3f} | {_fmt(item['conflict_accuracy'])} |"
            )

    lines += [
        "",
        "## 结论适用范围",
        "",
        "- 以上数字全部来自**合成数据**，只说明在本生成器构造的场景分布上的相对表现。",
        "- 真实 Outlook 邮件与真实微信/QQ 聊天记录**尚未验证**；Graph 与手机连接器目前是骨架。",
        "- baseline 的 token/成本是估算值（按 DeepSeek 公开单价换算），不是账单金额。",
        "- 若某项 Agent-LLM 不如规则版，报告保留原值，不做针对性调参。",
    ]
    if summary.get("agent_llm", {}).get("document_issues"):
        lines += ["", "## Agent-LLM 文档校验问题", ""]
        lines += [f"- {issue}" for issue in summary["agent_llm"]["document_issues"][:20]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="DDL Agent 三方离线评测")
    parser.add_argument("--dataset", default="experiments/ddl_agent/dataset")
    parser.add_argument("--out", default="experiments/ddl_agent/results")
    parser.add_argument("--split", default="test", choices=["train", "val", "test", "all"])
    parser.add_argument("--tier", default=None, choices=["easy", "medium", "hard"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-tier", type=int, default=24)
    parser.add_argument("--no-llm", action="store_true", help="强制纯规则/离线模式")
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

    client = None
    if llm_enabled() and not args.no_llm:
        client = LLMClient.from_config()
    if client is None:
        print("警告：未启用 LLM（无 key 或 --no-llm），Agent-LLM 与 baseline 将退回规则实现")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_root = out_dir / "work"

    entries = [run_one(sample, work_root, client=client) for sample in samples]

    overall = summarize(entries)
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        by_tier[entry["tier"]].append(entry)
    overall["by_tier"] = {tier: summarize(group) for tier, group in by_tier.items()}
    overall["per_sample"] = [
        {
            "sample_id": entry["sample_id"],
            "tier": entry["tier"],
            **{
                system: {
                    "engine": entry[system]["engine"],
                    "exact": entry[system]["exact"],
                    "fuzzy": entry[system]["fuzzy"],
                    "matched": entry[system]["matched"],
                    "predicted": entry[system]["predicted"],
                    "duplicate_rows": entry[system]["duplicate_rows"],
                    "conflict_total": entry[system]["conflict_total"],
                    "conflict_correct": entry[system]["conflict_correct"],
                    "missed_tasks": entry[system]["missed_tasks"],
                    "llm_calls": entry[system]["trace"]["llm_calls"],
                    "records": entry[system]["records"],
                }
                for system in SYSTEMS
            },
        }
        for entry in entries
    ]

    (out_dir / "summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _render_report(overall, args.dataset, args.split)
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps({key: overall[key] for key in ("samples", "engines", *SYSTEMS)},
                     ensure_ascii=False, indent=2))
    print(f"\n报告已写入：{out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
