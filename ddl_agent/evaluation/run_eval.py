"""离线评测：同一批输入对比 Agent-LLM、Agent-规则、单次直出 baseline。

用法：
    python -m ddl_agent.evaluation.run_eval --split test
    LLM_ENABLED=0 python -m ddl_agent.evaluation.run_eval --split test   # 纯离线
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from ..connectors.local_chat import LocalChatConnector
from ..connectors.local_eml import LocalEmlConnector
from ..dataset.generator import load_samples, write_dataset
from ..dedupe import Deduplicator
from ..factory import build_agent, llm_enabled
from ..llm.client import LLMClient
from ..planner import QueryPlanner
from ..retrieval import RetrievalOrchestrator
from ..settings import EMBEDDING_CACHE_DIR, EMBEDDING_MODEL
from ..writer.xlsx_writer import XlsxWriter
from .baseline import LLMSinglePassBaseline, SinglePassBaseline
from .metrics import evaluate_dataset, evaluate_sample, validate_document

SYSTEMS = ("agent_llm", "agent_rules", "baseline")


@dataclass(frozen=True)
class Ablation:
    """单模块消融开关；默认全开。"""

    dedupe: bool = True
    verify: bool = True
    fixed_sources: bool = False

    @property
    def label(self) -> str:
        parts = []
        if not self.dedupe:
            parts.append("no_dedup")
        if not self.verify:
            parts.append("no_verify")
        if self.fixed_sources:
            parts.append("fixed_sources")
        return "_".join(parts) or "full"


ABLATIONS = [
    Ablation(),
    Ablation(dedupe=False),
    Ablation(verify=False),
    Ablation(fixed_sources=True),
]


@dataclass(frozen=True)
class SimilarityConfig:
    """去重相似度策略；embedding 不可用时自动退回词法。"""

    strategy: str = "lexical"
    model: str = EMBEDDING_MODEL
    cache_dir: str = EMBEDDING_CACHE_DIR

    def build(self):
        from ..semantic import SemanticMatcher

        return SemanticMatcher(
            strategy=self.strategy, model_name=self.model,
            cache_dir=self.cache_dir,
        )


SIMILARITIES = ("lexical", "embedding", "combined")


def materialize_sources(sample: dict, root: Path) -> list:
    """把样本里的消息写成真实连接器能读的 .eml 目录 + 每个群独立的聊天归档。"""
    root.mkdir(parents=True, exist_ok=True)
    mail_dir = root / "mail"
    mail_dir.mkdir(exist_ok=True)
    chats: dict[str, list[dict]] = {}

    for message in sample["messages"]:
        if message["source_type"] == "official_email":
            _write_eml(message, mail_dir)
        else:
            chats.setdefault(message["source_name"], []).append({
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
    for index, (name, items) in enumerate(chats.items(), start=1):
        chat_path = root / f"chat-{index}.json"
        chat_path.write_text(json.dumps({"messages": items}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        source_type = items[0].get("source_type") or "student_chat"
        sources.append(LocalChatConnector(name, chat_path, source_type))
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


def run_one(sample: dict, work_root: Path, client: LLMClient | None = None,
            ablation: Ablation | None = None,
            include_baseline: bool = True,
            systems: tuple[str, ...] = ("agent_llm", "agent_rules"),
            matcher=None, threshold: float | None = None) -> dict:
    now = datetime.fromisoformat(sample["now"])
    ablation = ablation or Ablation()
    sample_dir = work_root / sample["sample_id"]
    sources = materialize_sources(sample, sample_dir / "sources")

    entry: dict = {
        "sample_id": sample["sample_id"],
        "tier": sample["tier"],
        "split": sample["split"],
        "query": sample["query"],
        "ablation": asdict(ablation),
        "ablation_label": ablation.label,
        "similarity": matcher.info() if matcher is not None else {"strategy": "lexical"},
    }

    for key, engine in (("agent_llm", "llm"), ("agent_rules", "rules")):
        if key not in systems:
            continue
        # 没有可用 LLM 客户端时不发网络请求，两个 Agent 都走规则实现
        effective_engine = engine if (engine == "rules" or client is not None) else "rules"
        document = sample_dir / f"{key}_{ablation.label}.xlsx"
        dedupe_kwargs = {}
        if matcher is not None:
            dedupe_kwargs["similarity"] = matcher.similarity
            dedupe_kwargs["raw_inputs"] = True
            dedupe_kwargs["threshold"] = (
                threshold if threshold is not None else matcher.threshold
            )
        elif threshold is not None:
            dedupe_kwargs["threshold"] = threshold
        agent, engine_name = build_agent(
            sources, writer=XlsxWriter(document), now=now, engine=effective_engine,
            dedupe_enabled=ablation.dedupe, verify_enabled=ablation.verify,
            fixed_sources=ablation.fixed_sources,
            deduplicator=Deduplicator(**dedupe_kwargs) if dedupe_kwargs else None,
        )
        result = agent.run(sample["query"], sample.get("source_config", ""))
        entry[key] = {
            **evaluate_sample(result.records, sample["ground_truth"], matcher),
            "engine": engine_name,
            "trace": result.trace,
            "write_ok": bool(result.write_result and result.write_result.ok),
            "document": validate_document(document),
            "warnings": result.warnings,
            "plan": result.plan,
            "records": [
                {"task_name": r.task_name, "course": r.course, "deadline_iso": r.deadline_iso,
                 "status": r.status, "confidence": r.confidence, "source": r.source,
                 "sources": r.sources, "note": r.resolution_note}
                for r in result.records
            ],
        }

    if not include_baseline:
        return entry

    # baseline 使用与 Agent-LLM 相同的选源计划，保证“同一套原始材料”公平可比
    plan = entry.get("agent_llm", {}).get("plan")
    if plan is None:
        plan = QueryPlanner().plan(sample["query"], sources, now)
    orchestrator = RetrievalOrchestrator(plan.sources or sources, now=now)
    raw_messages = orchestrator.fetch(plan)
    baseline = LLMSinglePassBaseline(client) if client is not None else SinglePassBaseline()
    base_records, base_trace = baseline.run(raw_messages, now=now)
    entry["baseline"] = {
        **evaluate_sample(base_records, sample["ground_truth"], matcher),
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
        f"- 消融设置：去重={'开' if summary.get('ablation', {}).get('dedupe', True) else '关'}，"
        f"冲突验证={'开' if summary.get('ablation', {}).get('verify', True) else '关'}，"
        f"动态工具选择={'关（固定源）' if summary.get('ablation', {}).get('fixed_sources') else '开'}。",
        f"- 相似度策略：{summary.get('similarity', {}).get('strategy', 'lexical')}"
        f"（embedding 可用={summary.get('similarity', {}).get('available', 'N/A')}）。",
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


def summarize_ablation(entries: list[dict], ablation: Ablation) -> dict:
    block = evaluate_dataset([entry["agent_llm"] for entry in entries])
    valid_docs = sum(1 for entry in entries if entry["agent_llm"]["document"]["ok"])
    block.update({
        "variant": ablation.label,
        "ablation": asdict(ablation),
        "engine": sorted({entry["agent_llm"]["engine"] for entry in entries}),
        "document_valid_rate": round(valid_docs / len(entries), 4) if entries else 0.0,
        "efficiency": _efficiency(entries, "agent_llm"),
    })
    return block


def run_ablation(samples: list[dict], work_root: Path, client: LLMClient | None,
                 matcher=None) -> list[dict]:
    rows: list[dict] = []
    for ablation in ABLATIONS:
        entries = [
            run_one(
                sample, work_root / "ablation" / ablation.label, client=client,
                ablation=ablation, include_baseline=False, systems=("agent_llm",),
                matcher=matcher,
            )
            for sample in samples
        ]
        rows.append(summarize_ablation(entries, ablation))
        print(f"消融 {ablation.label}: "
              f"exact={rows[-1]['exact_match']:.3f} recall={rows[-1]['recall']:.3f} "
              f"dup={rows[-1]['duplicate_rate']:.3f}")
    return rows


def summarize_similarity(entries: list[dict], strategy: str, matcher) -> dict:
    block = evaluate_dataset([entry["agent_llm"] for entry in entries])
    valid_docs = sum(1 for entry in entries if entry["agent_llm"]["document"]["ok"])
    block.update({
        "strategy": strategy,
        "similarity": matcher.info(),
        "document_valid_rate": round(valid_docs / len(entries), 4) if entries else 0.0,
        "efficiency": _efficiency(entries, "agent_llm"),
    })
    return block


def run_similarity_ablation(samples: list[dict], work_root: Path,
                            client: LLMClient | None,
                            config: SimilarityConfig) -> list[dict]:
    rows: list[dict] = []
    for strategy in SIMILARITIES:
        matcher = SimilarityConfig(
            strategy=strategy, model=config.model,
            cache_dir=config.cache_dir,
        ).build()
        entries = [
            run_one(
                sample, work_root / "similarity" / strategy, client=client,
                include_baseline=False, systems=("agent_llm",), matcher=matcher,
            )
            for sample in samples
        ]
        rows.append(summarize_similarity(entries, strategy, matcher))
        print(f"相似度 {strategy}: exact={rows[-1]['exact_match']:.3f} "
              f"dup={rows[-1]['duplicate_rate']:.3f} "
              f"available={rows[-1]['similarity']['available']}")
    return rows


def _render_similarity(rows: list[dict], dataset: str, split: str) -> str:
    lines = [
        "# 相似度策略对比（词法 / embedding / 组合）",
        "",
        f"- 数据集：`{dataset}`；划分：`{split}`；样本数：{rows[0]['samples'] if rows else 0}",
        "- 同一批样本、同一套原始消息、其余模块全开；只替换去重相似度。",
        "- 精确匹配、召回、重复率、精度、无关行口径与消融实验一致。",
        "",
        "| 策略 | 精确 | 召回 | 重复率 | 精度 | 无关行 | 文档通过率 | embedding 可用 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        info = row["similarity"]
        note = "正常" if info["available"] else f"退回词法：{info['fallback_reason']}"
        lines.append(
            f"| {row['strategy']} | {row['exact_match']:.3f} | {row['recall']:.3f} | "
            f"{row['duplicate_rate']:.3f} | {row['precision']:.3f} | {row['unknown_rows']} | "
            f"{row['document_valid_rate']:.3f} | {info['available']} | {note} |"
        )

    by_strategy = {row["strategy"]: row for row in rows}
    lexical = by_strategy.get("lexical")
    combined = by_strategy.get("combined")
    embedding = by_strategy.get("embedding")
    lines += ["", "## 解读", ""]
    if lexical and combined:
        lines.append(
            f"- 组合策略相对纯词法：精确 {combined['exact_match'] - lexical['exact_match']:+.3f}，"
            f"重复率 {combined['duplicate_rate'] - lexical['duplicate_rate']:+.3f}。"
        )
    if lexical and embedding:
        lines.append(
            f"- 纯 embedding 相对纯词法：精确 "
            f"{embedding['exact_match'] - lexical['exact_match']:+.3f}，"
            f"重复率 {embedding['duplicate_rate'] - lexical['duplicate_rate']:+.3f}。"
        )
    unavailable = [row["strategy"] for row in rows if not row["similarity"]["available"]]
    if unavailable:
        lines.append(
            f"- 以下策略实际退回了词法路径：{', '.join(unavailable)}；"
            "上面的差异不能归因于 embedding。"
        )
    lines += [
        "",
        "## 口径与限制",
        "",
        "- 数字来自合成数据；大部分任务名是 `HW1` / `Assignment 1` 这类短文本，"
        "embedding 的优势可能只在真正的跨语言改写上体现，本轮不保证出现提升。",
        "- 组合策略取词法与 embedding 的较高分（`max`），不是加权平均："
        "两条路径各自过阈值即可合并，避免任一路径分数偏低时把另一路径的证据拉没。",
        "- 阈值：词法 0.78、embedding 余弦 0.6，可用 `--dedupe-threshold` 调整；"
        "embedding 模型默认 `paraphrase-multilingual-MiniLM-L12-v2`，缓存目录默认 "
        "`F:/models/sentence-transformers`（可用 `--embedding-model` / `--embedding-cache` 覆盖）。",
        "- 模型缺失、下载失败或离线时不会中断流程，会退回词法并在表中标注"
        "（`embedding 可用=False`）；此时各策略差异不能归因于 embedding。",
    ]
    return "\n".join(lines) + "\n"


def _render_ablation(rows: list[dict], dataset: str, split: str) -> str:
    engines = sorted({engine for row in rows for engine in row["engine"]})
    lines = [
        "# 单模块消融实验（RQ4）",
        "",
        f"- 数据集：`{dataset}`；划分：`{split}`；样本数：{rows[0]['samples'] if rows else 0}",
        f"- 实际引擎：{engines}",
        "- 同一批样本、同一套原始消息；每行只改变一个开关，其余保持全开。",
        "- 指标口径与三方对比一致；冲突正确率只在含冲突 ground truth 的样本上统计。",
        "",
        "| 变体 | 精确 | ±1天模糊 | 召回 | 精度 | 无关行 | 重复率 | 冲突正确率 | 文档通过率 | "
        "LLM 调用 | token | 成本 USD | 平均耗时 s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        efficiency = row["efficiency"]
        lines.append(
            f"| {row['variant']} | {row['exact_match']:.3f} | {row['fuzzy_match']:.3f} | "
            f"{row['recall']:.3f} | {row['precision']:.3f} | {row['unknown_rows']} | "
            f"{row['duplicate_rate']:.3f} | "
            f"{_fmt(row['conflict_accuracy'])} | {row['document_valid_rate']:.3f} | "
            f"{efficiency['llm_calls']} | {efficiency['tokens_estimate']} | "
            f"{efficiency['cost_usd']:.6f} | {efficiency['avg_seconds']:.3f} |"
        )

    lines += ["", "## 关闭单项后的变化（相对全开）", ""]
    full = next((row for row in rows if row["variant"] == "full"), rows[0] if rows else None)
    if full is not None:
        for row in rows:
            if row["variant"] == "full":
                continue
            delta_exact = row["exact_match"] - full["exact_match"]
            delta_recall = row["recall"] - full["recall"]
            delta_precision = row["precision"] - full["precision"]
            delta_unknown = row["unknown_rows"] - full["unknown_rows"]
            delta_dup = row["duplicate_rate"] - full["duplicate_rate"]
            delta_conflict = (
                None if row["conflict_accuracy"] is None or full["conflict_accuracy"] is None
                else row["conflict_accuracy"] - full["conflict_accuracy"]
            )
            lines.append(
                f"- **{row['variant']}**：精确 {delta_exact:+.3f}，召回 {delta_recall:+.3f}，"
                f"精度 {delta_precision:+.3f}，无关行 {delta_unknown:+d}，"
                f"重复率 {delta_dup:+.3f}，冲突正确率 "
                f"{'N/A' if delta_conflict is None else f'{delta_conflict:+.3f}'}"
            )
        drops = [
            (full["exact_match"] - row["exact_match"], row["variant"])
            for row in rows if row["variant"] != "full"
        ]
        if drops:
            worst = max(drops)
            lines += [
                "",
                f"以精确匹配为主要口径，关闭 **{worst[1]}** 的下降幅度最大（{worst[0]:.3f}）。",
            ]
    lines += [
        "",
        "## 口径与限制",
        "",
        "- 以上数字来自合成数据，只反映本生成器覆盖的场景。",
        "- `fixed_sources` 关闭的是 LLM 规划器的来源子集选择，改为固定使用全部已配置来源；"
        "它会跳过 LLM 规划，但把干扰来源的消息也送进抽取器，因此调用与 token 反而上升。"
        "质量指标是否变化取决于抽取器能否过滤无关消息，本数据集上质量不变、成本上升。",
        "- `no_verify` 仍保留去重，只跳过冲突比对、追加检索与裁决模型调用。",
        "- `no_dedup` 让每条候选单独成行，会直接抬高重复率、降低文档质量。",
    ]
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
    parser.add_argument("--ablation", action="store_true",
                        help="跑 RQ4 单模块消融：全开 / 关去重 / 关冲突验证 / 固定来源")
    parser.add_argument("--no-dedup", action="store_true", help="消融：关闭跨来源去重")
    parser.add_argument("--no-verify", action="store_true", help="消融：关闭冲突验证与裁决")
    parser.add_argument("--fixed-sources", action="store_true",
                        help="消融：关闭动态工具选择，固定使用全部已配置来源")
    parser.add_argument("--similarity", choices=list(SIMILARITIES), default="lexical",
                        help="去重相似度：lexical / embedding / combined")
    parser.add_argument("--similarity-ablation", action="store_true",
                        help="对比词法 / embedding / 组合三种相似度")
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument("--embedding-cache", default=EMBEDDING_CACHE_DIR)
    parser.add_argument("--dedupe-threshold", type=float, default=None,
                        help="去重相似度阈值（默认 0.78）")
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

    similarity_config = SimilarityConfig(
        strategy=args.similarity, model=args.embedding_model,
        cache_dir=args.embedding_cache,
    )

    if args.similarity_ablation:
        rows = run_similarity_ablation(samples, work_root, client, similarity_config)
        payload = {
            "dataset": args.dataset,
            "split": args.split,
            "samples": len(samples),
            "config": asdict(similarity_config),
            "strategies": rows,
        }
        (out_dir / "similarity.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "similarity.md").write_text(
            _render_similarity(rows, args.dataset, args.split), encoding="utf-8"
        )
        print(f"\n相似度对比已写入：{out_dir / 'similarity.md'}")
        return

    matcher = similarity_config.build()
    if similarity_config.strategy != "lexical" and not matcher.available:
        print(
            f"警告：{similarity_config.strategy} 相似度不可用，已退回词法路径："
            f"{matcher.info()['fallback_reason']}",
            file=sys.stderr,
        )

    if args.ablation:
        rows = run_ablation(samples, work_root, client, matcher=matcher)
        payload = {
            "dataset": args.dataset,
            "split": args.split,
            "samples": len(samples),
            "variants": rows,
        }
        (out_dir / "ablation.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "ablation.md").write_text(
            _render_ablation(rows, args.dataset, args.split), encoding="utf-8"
        )
        print(json.dumps(
            [{key: row[key] for key in (
                "variant", "exact_match", "fuzzy_match", "recall", "duplicate_rate",
                "conflict_accuracy", "document_valid_rate",
            )} for row in rows],
            ensure_ascii=False, indent=2,
        ))
        print(f"\n消融报告已写入：{out_dir / 'ablation.md'}")
        return

    ablation = Ablation(
        dedupe=not args.no_dedup,
        verify=not args.no_verify,
        fixed_sources=args.fixed_sources,
    )
    entries = [run_one(sample, work_root, client=client, ablation=ablation,
                       matcher=matcher, threshold=args.dedupe_threshold)
               for sample in samples]

    overall = summarize(entries)
    overall["ablation"] = asdict(ablation)
    overall["similarity"] = matcher.info()
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
