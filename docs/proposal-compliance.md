# Proposal Compliance Matrix

> Compared against: `main(1).md` (proposal)
> Comparison date: 2026-11-18
> Status definitions: **✅ implemented and verifiable** / **◐ partially implemented or different criteria** / **✗ not done**
> Verification boundary: verifiability comes from offline tests and synthetic data; real-device screen reading has had one read-only verification on a single device, single account, WeChat self-chat conversation
> (see `docs/real-device-test-report.md`); real course groups, QQ / Outlook App, image-type DDL and real credentials are still unverified.

## 1. System Goals

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| The user queries the DDL of a course/project in natural language | ✅ | `ddl_agent/planner.py: QueryPlanner`, `ddl_agent/pipeline.py: DdlAgent.run` | Parses course code, keywords and time window; every sample in the evaluation has a query. |
| Configure the data sources to retrieve in natural language before running | ✅ | `ddl_agent/monitor/config.py`, `ddl_agent/monitor/cli.py` | Parsed into `sources.yaml`, supports CRUD, enable/disable and retention of the original source sentence; explicitly rejects when unparseable. |
| The Agent autonomously completes task planning, tool selection, retrieval, extraction, deduplication and conflict verification | ◐ | `pipeline.py`, `llm/planner.py`, `extract/*`, `dedupe.py`, `conflict.py` | Extraction/deduplication/conflict verification are implemented; **tool selection only goes as far as "selecting a subset among configured sources"**; there is no general tool registry, nor a loop that "plans an arbitrary tool sequence". Retrieval is basically a single pull, with two supplementary rounds only on conflict. |
| Write structured DDL into the specified output document | ✅ | `writer/xlsx_writer.py`, `writer/base.py`, `output.py` | Writes xlsx; also exports `data.json`, `deadlines.ics`, `calendar.html`. |
| Reduce manual searching across Apps and improve correctness and completeness | ◐ | `evaluation/run_eval.py` | Improvement over single-pass on synthetic data (see Section 7); real data not yet verified. |

## 2. Four Challenges

| Proposal challenge | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Extract structured fields from heterogeneous, scattered, unstructured email/chat | ✅ | `extract/rules.py`, `extract/llm_extractor.py`, `extract/normalizer.py` | Dual rule and LLM implementations; relative times are normalized against the injected `now`, unparseable ones marked `unresolved`. |
| Handle duplicate records and cross-source conflicts | ✅ | `dedupe.py`, `conflict.py` | Task-name normalization + similarity clustering; adjudication by credibility and publication time; at most two supplementary retrieval rounds; still conflicting → `uncertain`. |
| Dynamic planning and tool selection; users do not always say clearly which App to query | ◐ | `llm/planner.py`, `planner.py: SourceConfigParser` | The LLM planner selects a subset among the configured sources; but the "configured sources" are still provided by the user in advance, and there is no general tool-calling loop. After adding distractor sources, dynamic source selection still does not change task quality but significantly reduces extraction calls for irrelevant messages (see Section 8). |
| Reliably modify the structured document while preserving existing valid content | ✅ | `writer/base.py`, `writer/xlsx_writer.py`, `writer/audit.py` | Inserts/modifies rows by `(course, normalized task name)`; backs up before writing, retries once on failure then rolls back; audit CSV + xlsx sheet. |

## 3. Solution by Section

### 3.1 System Overview

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Work around a single shared DDL document | ✅ | `writer/xlsx_writer.py` | Default `deadlines.xlsx`, containing Deadlines and Audit Log. |
| Start from a user-defined source configuration | ✅ | `monitor/config.py` | The phone path uses `sources.yaml`; the evaluation path uses the sample's `source_config`. |
| Natural language → planning → fetching → parsing → dedup/conflict → write-back | ✅ | `pipeline.py: DdlAgent.run` | Stages are clear; trace records per-stage time, rounds, token/cost. |
| Outlook goes through the Microsoft Graph API | ◐ | `monitor/sources.py: MobileAppSource` | **Design decision**: Outlook only uses phone App GUI screen reading, which is the only main path. Microsoft Graph **is not on the current implementation path**; the code skeleton is kept only as a "future optional implementation" and is not promised support, nor is it on par with phone screen reading. See 3.1.1 for the reason. |
| WeChat/QQ have no public API, so use phone GUI automation | ✅ (code) / unverified (real device) | `monitor/sources.py: MobileAppSource`, `screen_agent` | Searches/scrolls to locate by name and explicitly reports "not found"; the real-device path is not verified with real data. |
| Develop and unit-test early with manually constructed message sets | ✅ | `dataset/generator.py`, `connectors/local_*.py` | Evaluation still runs on local `.eml`/chat archives and does not depend on a phone. |
| Test limited real chat records once the core is stable | ✗ | — | Not done; requires a real device, real accounts and consent from participating users, and needs team scheduling. |

#### 3.1.1 Outlook Design Decision (explicit deviation)

- The proposal says "connect to Outlook through the official Microsoft Graph API".
- **The current only main path is phone Outlook App GUI screen reading**, sharing the same phone layer as WeChat and QQ.
- Microsoft Graph **has been removed from the main path**: configuration, planning, retrieval and evaluation do not use Graph, and there is no "Graph / phone screen reading either-or" switch.
  `connectors/graph_mail.py` is only a retained code skeleton, labelled "future optional implementation"; it is currently not enabled, not tested, and not a delivery capability.
- Reasons:
  1. The project supervisor explicitly requires all sources to go uniformly through the phone GUI and reuse the phone agent;
  2. Graph needs tenant/app registration and user authorization, and real credentials cannot be provided stably in the course environment;
  3. WeChat and QQ can only use the GUI; putting Outlook on the same layer allows reusing localization, screenshots, evidence and the device lock, reducing the maintenance cost of two reading paths.
- Cost: Outlook's reading speed, structure and stability are inferior to the Graph API; email body/attachment parsing is limited to what is visible on screen. This point is marked "not verified with real data" in both docs and code.
- Wording: the paper/report should say "Outlook uses phone App screen reading", not "supports both Graph and phone access".

### 3.2 Task Planning and Tool Selection

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Understand the course/project and time range in the query | ✅ | `planner.py: QueryPlanner`, `_parse_date_range` | Course code, keywords, this week/next week/this month/today, etc. |
| Decide which configured sources should be queried | ✅ | `llm/planner.py: LLMQueryPlanner`, `planner.py: SourceConfigParser` | The LLM returns a source subset; the rule version matches by name/type and falls back to all sources when nothing matches. |
| Tools are split into "fetching" and "LLM reasoning", decided by the LLM at runtime | ◐ | `llm/planner.py`, `conflict.py` | The planner selects sources and decides whether to continue checking during conflicts; but there is no general tool registry, nor runtime orchestration of arbitrary tool sequences. |
| Do not execute a fixed hardcoded search sequence | ◐ | `pipeline.py` | The main flow is still a fixed stage order; dynamism appears only in the source subset and supplementary retrieval on conflict. |

### 3.3 DDL Extraction and Integration

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Extract task name, course/project, deadline and source | ✅ | `extract/rules.py`, `extract/llm_extractor.py`, `conflict.py` | Both candidates and final records keep the source; the output table adds `All Sources` to retain all original sources. |
| Convert times uniformly to ISO-8601 | ✅ | `extract/normalizer.py` | Fixed `+08:00`; relative times are based on the injected `now` to guarantee reproducibility. |
| Convert relative times (next Friday etc.) into absolute times | ✅ | `extract/normalizer.py` | Chinese and English relative dates, weekdays, `N 天后`, `EOD`, etc. |
| Mark for manual checking when parsing is unreliable | ✅ | `models.py`, `conflict.py` | `unresolved_time` / `needs_review` → `uncertain`, never guessing the date. |
| Keyword + semantic matching of `HW1` vs `Assignment 1` | ✅ | `dedupe.py`, `semantic.py` | Lexical normalization (Jaccard + character sequence) and local embedding cosine can be combined; `combined` takes the higher of the two, threshold configurable. Missing model/offline automatically falls back to lexical. Three-strategy comparison on the same data in 8.1. |
| Merge the same task and retain every original source | ✅ | `dedupe.py`, `conflict.py`, `writer/xlsx_writer.py` | This round adds `ResolvedRecord.sources` and the xlsx `All Sources` column. |

### 3.4 Verification and Conflict Resolution

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Four-level credibility: official email > course announcement > instructor chat > student message | ➖ not implemented as fixed four levels | `conflict.py: _resolve_cluster` | Adjudication does not use a source-level prior such as `SOURCE_CREDIBILITY`; all candidates in the same conflict cluster are equally weighted. The actual order is: correction/void markers carried by the candidate → newest `published_at` → majority agreement; only if still inconsistent is it handed to LLM arbitration or marked `uncertain`. |
| More credible sources take precedence | ⚠️ only markers carried by candidates | `conflict.py`, `llm/arbiter.py` | No fixed weighting merely because it is "from email/instructor group"; priority is decided by markers only when the candidate text explicitly says "take this as authoritative/void/corrected", and the arbitration prompt also requires treating sources equally. |
| A newer announcement overrides an older announcement among equal-weight candidates | ✅ | `conflict.py` | Equal-weight candidates compare `published_at`; the newest wins. |
| Stop condition 1: all relevant sources checked and consistent | ✅ | `pipeline.py` + `conflict.py` | The retriever first pulls all selected sources; if candidate times agree, directly confirmed. |
| Stop condition 2: two supplementary retrieval rounds still cannot resolve | ✅ | `conflict.py: _resolve_cluster`, `settings.py: MAX_SUPPLEMENTARY_ROUNDS=2` | Both the rule path and the LLM path have `while ... rounds_used < max_rounds`; a test asserts that exactly two rounds are followed by `uncertain`. |
| Stop condition 3: model confidence high enough | ✅ | `llm/arbiter.py`, `settings.py: LLM_MIN_CONFIDENCE=0.6` | Below the threshold, refusal, or fabricating a time outside the candidates is not adopted, and supplementary retrieval continues. |
| Still conflicting → uncertain, hand to human | ✅ | `conflict.py` | Keeps all candidates and notes; does not guess. |

**Verification conclusion on "two rounds"**: it is not just one round, nor is supplementary retrieval missing. `ConflictResolver` defaults to `max_rounds=2`, and both the rule path and the LLM arbitration path call `_supplement` in rounds 1 and 2; existing tests cover "resolved in round 1" and "still uncertain after two rounds".

**Difference that still must be stated honestly**: the "supplementary retrieval" in offline evaluation pulls the same static local messages again, so no new evidence usually appears; the phone path re-reads the screen, but this has not been verified on a real device. Therefore "the mechanism exists and is testable" is ✅, while "supplementary retrieval can change the conclusion in a real environment" is not yet verified.

### 3.5 Document Updating

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| The structured table contains task name, course/project, deadline, task status and original source | ✅ | `settings.py: DEADLINE_COLUMNS`, `writer/xlsx_writer.py` | This round completes `All Sources`. Current columns: Task Name / Course / Deadline / Status / Source / All Sources / Confidence / Evidence / Last Updated. |
| Do not rewrite the whole table; insert or modify rows | ✅ | `writer/base.py: merge_records` | Match by `(course, normalize(task_name))`; update if the time changed, skip if the content is the same. |
| Retry once on write failure, keep the previous version | ✅ | `writer/base.py` | `.bak` backup → write → retry once on failure → roll back if it fails again. |
| Write an audit log for every modification | ✅ | `writer/audit.py`, `xlsx_writer.py` | `deadlines.xlsx.audit.csv` + Audit Log sheet; each cycle's `cycles/<id>.json` records insert/update/skip. |
| Support downstream calendar reminders | ✅ | `output.py` | `.ics` with `VALARM`; `calendar.html` can be opened by double-click; the phone system calendar interface is reserved and disabled by default. |

## 4. Dataset

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Synthetic simulated email and chat, with realistic-style DDL | ✅ | `dataset/generator.py` | Fact-driven generation; text only renders the noise. |
| Each sample has manually prepared ground truth | ◐ | `dataset/generator.py` | **Deviation**: ground truth is derived directly from the generator's facts, not independently annotated by two team members. Equivalence is discussed in 4.1. |
| 20–30 per tier | ✅ | `dataset/generator.py: generate_samples(per_tier=24)` | 3 tiers × 24 = 72. |
| Cover direct extraction, duplicates, missing fields, conflicts, and old values superseded by new values | ✅ | `dataset/generator.py` | Also forwarding chains, signature/chit-chat noise, cross-year/time zones, name variants, old-DDL distractors, etc. |
| Query-irrelevant distractor sources (to test dynamic source selection) | ✅ | `dataset/generator.py: _distractors` | Each sample additionally carries 4 irrelevant sources: internship-referral group, family group, club-activity group, and another course group. Messages contain decoys that "look like DDL" (application deadlines, meeting sign-up deadlines, other course assignment deadlines), but they **do not enter ground truth and do not rewrite any fact**; truth is still derived only from facts. |
| Two team members independently annotate and discuss disagreements | ✗ | — | Not done; replaced by generator facts. |
| Limited tests on real chat records (consent required) | ✗ | — | Not done; needs team scheduling and consent from participating users. |
| train / val / test split to prevent leakage | ✅ | `dataset/generator.py`, `write_dataset` | Deterministic 60/20/20 split; 15 test samples. |

### 4.1 ground truth Criteria Difference and Equivalence

- Proposal: two team members annotate independently and resolve disagreements through discussion.
- Current: the generator first constructs facts (task, course, real deadline, source, whether there is a conflict), then derives ground truth from the facts; the text layer only renders the facts as noisy messages.
- Equivalence: in the chain "facts → text → ground truth", annotation is no longer affected by manual reading error and is reproducible; coverage scenarios are explicitly controlled by the generator.
- Limitations:
  1. There is no independent annotation, so errors in the definition of the generator's facts themselves cannot be found;
  2. Evaluation text and rules/prompts may share some patterns, with a risk of self-fulfilment;
  3. Real email/chat long-tail formats, images, attachments and quotation chains far exceed the generator's coverage.
- Therefore all metrics should be described only as "results on synthetic data" and must not be extrapolated to real-world performance.

## 5. Baseline

| Proposal requirement | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Same LLM | ✅ | `evaluation/baseline.py: LLMSinglePassBaseline` | Shares `LLMClient` configuration and model with the Agent. |
| All raw content + the original query in one input | ✅ | `baseline.py` | Single prompt, single call. |
| No task planning, repeated tool calls, or verification | ✅ | `baseline.py` | No deduplication, no conflict checking, no supplementary retrieval. |
| Gets exactly the same raw material as the Agent | ✅ | `evaluation/run_eval.py: run_one` | Same `materialize_sources` output. |
| Compare performance, LLM calls, tokens and cost | ✅ | `run_eval.py: _efficiency` | Report includes calls / tokens / USD / average time. |

## 6. Six Metrics

| Proposal metric | Status | Implementation location | Notes / differences |
| --- | --- | --- | --- |
| Exact match (task name + deadline exactly equal) | ✅ | `evaluation/metrics.py: evaluate_sample` | Task names are normalized before comparison. |
| Fuzzy match (±1 day) | ✅ | `metrics.py` | Tolerates time zones/informal dates. |
| Recall / completeness | ✅ | `metrics.py: evaluate_dataset` | matched / total ground-truth entries. |
| Conflict resolution accuracy | ✅ | `metrics.py` | Computed only on samples whose ground truth contains a conflict. |
| Duplicate rate | ✅ | `metrics.py` | Extra duplicate rows / all predicted rows. |
| Document update quality | ◐ | `metrics.py: validate_document` | Currently only automated structural checks (columns, empty values, audit table, status); **no manual spot-checking**. |
| Runtime / LLM calls / token / cost | ✅ | `run_eval.py: _efficiency` | The report lists each item. |

## 7. Five Research Questions

| RQ | Status | Evidence | Notes / differences |
| --- | --- | --- | --- |
| RQ1: Is the Agent better than single-pass | ✅ | `experiments/ddl_agent/results/report.md` (test set) and `ablation_test/ablation.md` | On the test set, Agent-LLM exact 0.8125 / recall 1.0; baseline exact 0.500 / recall 0.562. All synthetic data. |
| RQ2: Does iterative verification improve duplicates/missing fields/conflicts | ◐ | `conflict.py`, `tests/test_ddl_llm.py`, `ablation_test` | The two-round mechanism and tests exist; offline supplementary retrieval produces no new evidence, so the real benefit is unverified. The ablation shows conflict accuracy 1.0 → 0.667 after disabling verification. |
| RQ3: Trade-off between performance gain and multi-step overhead | ✅ | `run_eval.py` report | Reports quality, time, call count, token and cost together. |
| RQ4: Contribution of each module (ablation) | ✅ | This round adds `--ablation` / `--no-dedup` / `--no-verify` / `--fixed-sources`; results in Section 8 | Deduplication mainly lowers the duplicate rate; verification mainly improves exact match and conflict accuracy; dynamic source selection still does not change task quality after adding distractors, but disabling it raises LLM calls from 37 to 67 — i.e. what dynamic source selection saves is the extraction cost of irrelevant messages. |
| RQ5: Small-scale user study | ✗ | — | A manual step; not done; needs team scheduling of questionnaires/interviews and a comparative demonstration. |

## 8. RQ4 Ablation Results

Command:

```bash
python -m ddl_agent.evaluation.run_eval --ablation --split test \
  --out experiments/ddl_agent/results/ablation_distractors2
```

The same test samples (15, 16 ground-truth entries), the same raw messages, with the actual engine set to `llm`.
This round's dataset has added 4 irrelevant sources per sample (internship-referral group, family group, club-activity group, another course group);
the distractor messages contain decoys such as "application deadline / meeting sign-up deadline / other course assignment deadline", but **do not enter ground truth**.
Complete results are in `experiments/ddl_agent/results/ablation_distractors2/ablation.md` and `ablation.json`.

| Variant | Exact | ±1-day fuzzy | Recall | Precision | Irrelevant rows | Duplicate rate | Conflict accuracy | Doc pass rate | LLM calls | token | Cost USD | Avg time s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full (all on) | 0.812 | 0.812 | 1.000 | 1.000 | 0 | 0.000 | 1.000 | 1.000 | 37 | 16647 | 0.007454 | 2.604 |
| no_dedup (dedup off) | 0.812 | 0.812 | 1.000 | **0.889** | 0 | **0.111** | 1.000 | 1.000 | 36 | 16291 | 0.007324 | 2.500 |
| no_verify (conflict verification off) | **0.750** | **0.750** | 1.000 | 1.000 | 0 | 0.000 | **0.667** | 1.000 | 36 | 16313 | 0.007349 | 2.417 |
| fixed_sources (fixed sources) | 0.812 | 0.812 | 1.000 | 1.000 | 0 | 0.000 | 1.000 | 1.000 | **67** | **27843** | **0.009177** | **3.635** |

**Before and after adding distractor sources**

| Variant | LLM calls before adding | LLM calls after adding | token before adding | token after adding | Quality change |
| --- | --- | --- | --- | --- | --- |
| full | 38 | 37 | 15968 | 16647 | none |
| fixed_sources | 23 | 67 | 11220 | 27843 | none |

**Honest interpretation**

- The largest contribution to task quality still comes from **conflict verification/adjudication**: disabling it gives exact match −0.062 and conflict accuracy −0.333.
- The measurable contribution of **deduplication** is concentrated in the duplicate rate: 0 → 0.111 after disabling, precision 1.000 → 0.889; exact match is unchanged, because the exact metric is counted by ground-truth matching, while extra duplicate rows are penalized separately by duplicate rate/precision.
- **Dynamic source selection**: after adding distractor sources, all-on uses 37 calls while `fixed_sources` rises to 67 (+30, +81%), token 16647 → 27843 (+67%), cost about +23.1%, average time 2.60s → 3.64s. Quality metrics are completely unchanged.
  - Reason 1: `fixed_sources` skips LLM planning (saving about 15 planning calls), but sends messages from all 4 distractor sources into the extractor; the extra extraction/arbitration calls outweigh the saved planning calls, for a net increase of 30 calls.
  - Reason 2: the LLM extractor can filter internship-referral, family-group and club-group messages by course and "whether it looks like a course DDL", and other course assignments are also dropped by the course filter, so the extra messages do not become output rows.
  - Conclusion: **on this dataset, the benefit of dynamic source selection is cost and latency, not task quality**. This cannot prove "it is useless for quality", but it also **must not** be written as "dynamic source selection improves accuracy". The dataset has no ground truth for "source-selection accuracy", which is an explicit limitation of the current evaluation.
- All numbers come from synthetic data; `full`'s exact/recall/conflict accuracy agree with the historical three-way comparison (0.8125 / 1.0 / 1.0).

### 8.1 Semantic Matching: lexical / embedding / combined

Command:

```bash
python -m ddl_agent.evaluation.run_eval --similarity-ablation --split test \
  --out experiments/ddl_agent/results/similarity_test4
```

Same test samples, same raw messages, all other modules on; only the deduplication similarity is replaced.
The model is the local `sentence-transformers` `paraphrase-multilingual-MiniLM-L12-v2`;
the cache directory defaults to `~/.cache/ddl-agent/sentence-transformers` (overridable with `DDL_EMBEDDING_CACHE`).

| Strategy | Exact | Recall | Duplicate rate | Precision | Irrelevant rows | Doc pass rate | embedding available |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lexical | 0.812 | 1.000 | 0.000 | 1.000 | 0 | 1.000 | — |
| pure embedding | 0.812 | 1.000 | **0.059** | **0.941** | 0 | 1.000 | True |
| combined | 0.812 | 1.000 | 0.000 | 1.000 | 0 | 1.000 | True |

**Conclusion: on the current test set, embedding brings no quality improvement.**

- Exact match, recall and conflict accuracy are identical across the three strategies (0.812 / 1.000 / 1.000); lexical has already reached the upper bound of this dataset.
- Pure embedding instead produces 1 extra duplicate row: the cosine of `Assignment 2` and `HW2` is only 0.342 (< 0.6),
  while after lexical normalization the two are identical. So pure embedding's duplicate rate goes 0 → 0.059 and precision 1.000 → 0.941.
- The combined strategy takes `max(lexical, embedding)`; lexical's 1.0 covers this error, so the result is identical to pure lexical.
  This shows that **the value of combining is complementarity and a safety net, not improving the metrics in this round**.
- Number basis: all three strategies make 37 LLM calls; embedding only affects deduplication and does not affect extraction and adjudication calls.

**Paraphrase capability: yes, but it must be honestly bounded**

- Unit-level verification: `Midterm Report` / `期中报告` cosine 0.652 (passes 0.6), `term paper` / `期中报告` cosine 0.476 (does not pass the threshold).
- We once tried adding a sample to the test set with "the same task reworded in Chinese and English"; embedding could raise exact match from 0.750 to 0.875;
  but that amounts to **adjusting the test set for the method**, so it was reverted and does not enter this round's data or conclusions.
- Therefore the current position is: **embedding has the capability for genuine cross-language paraphrases, but on the existing
  test set, which is dominated by short codes (HW1/Assignment 1), no gain can be measured, and using it alone can even degrade performance because of abbreviations**. Do not write embedding as "semantic matching solved".

**Offline and degradation**

- `sentence-transformers` is an optional dependency, placed in `requirements-embedding.txt`, not in the main `requirements.txt`/CI,
  to avoid installing torch in CI just for evaluation.
- The model is downloaded by default to `~/.cache/ddl-agent/sentence-transformers` (overridable with `DDL_EMBEDDING_CACHE`); the code sets `HF_HOME`, `SENTENCE_TRANSFORMERS_HOME` and
  `TORCH_HOME` before loading, without hardcoding machine drive letters.
- Offline environment: first copy the whole model directory to the target machine, or set `DDL_EMBEDDING_CACHE` to point to an existing cache while keeping the model name consistent;
  load-failure exceptions are written to the report's `fallback_reason`, and evaluation continues to completion with lexical, without interruption.
- Real-model test cases are skipped by default; set `RUN_EMBEDDING_TESTS=1` to execute them. CI and offline tests do not depend on model files.

## 9. Still Incomplete / Needs Team Scheduling

1. **RQ5 user study**: questionnaire or small-scale comparative experience comparing Agent output with single-pass; needs team scheduling.
2. **Real-device verification**: phone screen reading, locating by name, Outlook App inbox; all currently marked "not verified with real data". Phone system calendar writing is disabled by the read-only constraint.
3. **Real chat/email testing**: requires real accounts, consent from participating users and data anonymization.
4. **Manual document spot-checking**: currently only automated structural checks; the manual checking step required by the proposal is missing.
5. **Independent two-person annotation**: ground truth is currently derived from generator facts; one independent annotation or sampling review is needed to verify the generator facts.
6. **Remaining gap in semantic matching**: embedding is integrated but brings no improvement on the current test set; pure embedding actually degrades on abbreviations such as `Assignment 2` / `HW2`. Cross-language paraphrasing has only been verified at the unit level (`Midterm Report` / `期中报告` 0.652; `term paper` / `期中报告` 0.476 still does not pass the threshold).
7. **General tool orchestration**: the current "tool selection" is limited to the source subset and supplementary retrieval on conflict; a general tool registry/arbitrary tool sequence has not been implemented.
8. **Missing source-selection metric**: the dataset has no ground truth for "which sources should be selected", so dynamic source selection can only report cost/latency benefits and cannot quantify source-selection accuracy.
