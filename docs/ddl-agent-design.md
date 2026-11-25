# DDL Agent Architecture Design

> Status: Phase 5 (phone-first + periodic retrieval). Corresponding requirements document `main(1).md`: query in natural language, collect DDLs from multiple sources,
> plan/select tools, field extraction and time normalization, cross-source deduplication, resolve conflicts by credibility and publication time, write into a shared structured document,
> and provide a synthetic dataset, a single-pass baseline and evaluation.
>
> This document records both the design and the current implementation boundaries; paths involving real devices and real credentials are marked "not verified with real data".

## 0. Operating Model (read this section first)

This is where misunderstandings are most likely, so it is stated first:

- **The operation target is the phone.** The interfaces of WeChat (group/private chat), QQ (group chat) and the Outlook App inbox are all read and tapped **on the phone**.
  The agent does not open WeChat/QQ/Outlook desktop clients on the computer, nor does it depend on local email files on the computer
  (local `.eml` / chat archives are only offline test stand-ins).
- **The runtime environment is the computer.** This project runs on a computer and drives the phone through **ADB (USB cable or WiFi debugging)**.
  **No component of this project is installed** on the phone, and **root is not required**; on first use uiautomator2 may install an automation helper component by itself,
  which is uiautomator2's existing mechanism, not a deployment of this project.
- In one sentence: **the agent on the computer operates the phone GUI through ADB**, aggregating group-chat and email DDLs from the phone into a calendar.

Windows users can double-click `run_ddl_monitor.bat` in the repository root: the script first checks adb, device online and authorized, and the device lock,
then lets the user choose "scan once / periodic"; after the run it refreshes and opens `output/calendar.html`.
When no phone is plugged in, USB debugging is not authorized, adb is not found, or the phone is occupied by another window, it gives a corresponding plain-language hint and next action.

## 1. Goals and Boundaries

### 1.1 Goals

- The user describes the data sources to track in natural language (`source_config`), then asks questions in natural language (`query`).
- The Agent autonomously completes: parse query → select data sources/tools → retrieve → extract → normalize → deduplicate → conflict check (at most two supplementary retrieval rounds)
  → write to the shared document (insert/modify rows + audit log).
- The whole flow can run offline and be evaluated on **local simulated data**, without depending on a real device or real Microsoft Graph permissions.

### 1.2 Explicit Boundaries

| Item | Handling in this phase |
|---|---|
| Three real data sources | WeChat (group/private chat), QQ (group chat), Outlook App inbox, **all read from the phone GUI screen content**. The real-device path is **not verified with real data**. |
| Outlook access method | **Only the phone Outlook App GUI screen reading**, sharing the same phone layer as WeChat and QQ. Microsoft Graph is not on the current implementation path; the code skeleton is kept only as a "future optional implementation" and is currently not enabled. |
| Local `.eml` / chat archives | Downgraded to **offline test stand-ins**, kept for running the full pipeline and tests when no phone or account is available. |
| LLM dependency | Planning/extraction/adjudication use DeepSeek; when offline or without a key, it automatically degrades to rule-based implementations. |
| Existing phone layer | **No public behavior is changed**; it is called only as a reading tool. |
| main branch / existing PRs | Do not touch, do not force push, do not merge. |

## 2. Overall Architecture

```
                      ┌──────────────────────────────┐
  自然语言 source_config│  SourceConfigParser           │
  自然语言 query        │  QueryPlanner                 │
                      └───────────────┬──────────────┘
                                      │ Plan(sources, filters, time_range, keywords)
                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │  RetrievalOrchestrator                               │
        │  逐个 MessageSource.fetch(query) → RawMessage[]       │
        └───────┬───────────────────┬──────────────────┬───────┘
                │                   │                  │
      LocalEmlConnector     LocalChatConnector   MobileChatConnector
      (默认，离线)           (默认，离线)          (复用手机层，未验证)
                │                   │                  │
                └───────────────────┴──────────────────┘
                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │  ExtractionPipeline                                  │
        │  RuleExtractor (默认) / LLMExtractor (可注入)         │
        │  → DeadlineCandidate[]（含 evidence、源、发布时间）    │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  TimeNormalizer → ISO-8601；不可解析标 unresolved      │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  Deduplicator（关键词 + 语义相似度，任务名归一化）      │
        │  → TaskCluster[]（同一任务的证据集合）                 │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  ConflictResolver（可信度优先 → 发布时间新者覆盖）     │
        │  最多两轮追加检索（回调 RetrievalOrchestrator）        │
        │  仍不一致 → status=uncertain（交人工）                 │
        └───────────────────────┬──────────────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────┐
        │  DeadlineWriter（可替换）                             │
        │  XlsxWriter(默认) / CsvWriter / MarkdownWriter        │
        │  插行/改行 + 审计日志 + 失败重试一次 + 保留上一版      │
        └──────────────────────────────────────────────────────┘
```

`ddl_agent.pipeline.DdlAgent.run(query, source_config)` is the only external entry point; evaluation and baselines call it or
its internal components.

## 3. Data Flow of One Query

1. **Parse configuration**: `SourceConfigParser.parse(source_config)` → `[SourceSpec(kind=email|chat, provider, locator, credibility)]`.
2. **Plan**: `QueryPlanner.plan(query, specs)` produces a `RetrievalPlan`: selected data sources, course/project filter words, time window, retrieval keywords.
   The rule version parses by keyword hits and time words; the LLM version can be substituted (interface `Planner`), with the offline default being rules.
3. **Retrieve**: `RetrievalOrchestrator.fetch(plan)` calls each `MessageSource.fetch()` in parallel and converts everything uniformly into `RawMessage`.
   A single source failure only records a warning and does not block other sources. Messages are soft-filtered by course (those that do not mention a course at all are kept).
   When the user says "next week/this month", the time window applies to the **final deadline** and filters records after conflict resolution; it does not filter by publication time, to avoid dropping earlier original announcements.
4. **Extract**: `RuleExtractor.extract(messages)` outputs `DeadlineCandidate` (task name, course, original deadline text, status, evidence).
5. **Normalize**: `TimeNormalizer` converts absolute/relative times into ISO-8601 (with injected `now` to guarantee reproducibility); unparseable ones are set to `unresolved`.
6. **Deduplicate**: `Deduplicator.cluster(candidates)` normalizes by task name + similarity clustering, merging the same task across sources and keeping all evidence.
7. **Conflict resolution**: `ConflictResolver.resolve(clusters, retrieval)` compares candidate times; if consistent, finalize directly;
   if inconsistent, decide by "source credibility → publication time"; if the highest-credibility group is still internally conflicting, initiate supplementary retrieval (at most two rounds, `supplementary_query`);
   if still inconsistent or undecidable → `status=uncertain`.
8. **Write**: `DeadlineWriter.upsert(records)` matches existing rows by `(course, normalized_task_name)`: hit means modify the row, otherwise insert a row;
   each change writes an audit entry; a write failure retries once, and on failure rolls back to the previous version and returns the failure reason.
9. **Report**: `AgentRunResult` contains `records`, `uncertain`, `warnings`, `trace` (per-stage time, retrieval rounds, tool-call count).

## 4. Module List (input / output / failure handling)

| Module | Input | Output | Failure handling |
|---|---|---|---|
| `SourceConfigParser` | Natural-language configuration text | `[SourceSpec]` | Falls back to the default configuration with a warning when the source cannot be recognized; does not guess the provider |
| `QueryPlanner` | query + `[SourceSpec]` | `RetrievalPlan` | With no keywords, degrades to "all configured sources + full time window" |
| `MessageSource.fetch` | `RetrievalPlan` | `[RawMessage]` | Single-source exceptions are captured as `SourceError` and the remaining sources continue; same for timeouts |
| `RuleExtractor` | `[RawMessage]` | `[DeadlineCandidate]` | Returns empty when nothing matches; missing fields are set to `None` and marked `missing_fields` |
| `TimeNormalizer` | Original time text + `now` + time zone | ISO-8601 string / `None` | Unparseable → `unresolved=True`, does not guess the date |
| `Deduplicator` | `[DeadlineCandidate]` | `[TaskCluster]` | When similarities tie, split by source priority to avoid false merging |
| `ConflictResolver` | `[TaskCluster]` + retrieval callback | `[ResolvedRecord]` | Still conflicting after two rounds → `uncertain`, keep all candidates for human review |
| `DeadlineWriter` | `[ResolvedRecord]` | `WriteResult` (inserted/updated/skipped counts) | Retry once on the first write failure; if it still fails, roll back to the previous version and report an error |
| `AuditLog` | Change event | Audit row (time, action, old value, new value, evidence ID) | An audit write failure is treated as an overall write failure and triggers rollback |

## 5. Relationship with the Existing Phone Automation Layer

The existing repository is an implementation of "phone GUI automation + screenshots/OCR/view tree + device lock + evidence"; this project downgrades it to **a data source connector**:

| Existing module | How it is reused | Boundary |
|---|---|---|
| `screen_agent.ScreenAgent` | `MobileChatConnector` drives it with a natural-language task to "open WeChat → enter the specified group/conversation → scroll up to read messages" | Reuses the public interface; scroll observation adds per-message hints and OCR overlay |
| `ocr_locator.OCRLocator` / `detect_tree` / `snap_action_coords` | Used internally by ScreenAgent; during scroll observation the text from `detect_ocr` is merged into the observation | OCR interface unchanged |
| `phone_controller.PhoneController` | Used internally by ScreenAgent (screenshot, tap, type_text) | Interface unchanged |
| `device_lock.DeviceLock` | The connector locks around the whole reading cycle to prevent concurrent phone operation with other experiments | Read-only reuse |
| `runs/` evidence directory | The connector uses each step's screenshot and `step_NNN.json` as `RawMessage.evidence_path` | Read-only reuse |

Real-device read-only verification (2026-11-18, WeChat 「文件传输助手」): ScreenAgent can enter the target conversation and scroll with evidence;
after overlaying OCR on scroll observation, 10 steps produce 10 non-empty message texts (0 before the fix). When the step limit is reached,
`MobileChatConnector` returns the collected observations with `partial=true` / `last_status=timeout`, no longer discarding evidence.

The rest (planning, extraction, deduplication, conflict, writing, evaluation, dataset) are **all newly created** in the `ddl_agent/` package.

## 6. Two Interfaces That Must Be Replaceable

### 6.1 Data Source Connector `MessageSource`

```python
class MessageSource(Protocol):
    name: str                 # e.g. "outlook" / "wechat:群名"
    source_type: SourceType   # OFFICIAL_EMAIL / COURSE_ANNOUNCEMENT / INSTRUCTOR_CHAT / STUDENT_CHAT
    def fetch(self, plan: RetrievalPlan) -> list[RawMessage]: ...
```

Implementations:

| Implementation | Status | Description |
|---|---|---|
| `MobileAppSource` (WeChat / QQ / Outlook) | **Main path; only the WeChat self-chat conversation has had one real-device read-only verification** | Uses ScreenAgent to search/scroll inside the App to locate the target conversation and read screen messages. WeChat verified once; QQ and Outlook currently have only package-name mapping and code paths, with **no real-device screen-reading verification**; all three lack coverage of real course groups/third-party conversations |
| `LocalEmlConnector` | Offline test stand-in | Reads a `*.eml` directory; runs the full pipeline without a phone |
| `LocalChatConnector` | Offline test stand-in | Reads chat archive JSON; runs the full pipeline without a phone |
| `GraphMailConnector` | Not on the main path | Code skeleton for a future optional implementation; currently not enabled, not tested, not a delivery capability |

Same `fetch(plan)` contract; evaluation can substitute arbitrarily without changing the upper layer.

### 6.2 Output Document Writer `DeadlineWriter`

```python
class DeadlineWriter(Protocol):
    def upsert(self, records: list[ResolvedRecord]) -> WriteResult: ...
    def read(self) -> list[ResolvedRecord]: ...
```

Implementations: `XlsxWriter` (default, openpyxl), `CsvWriter`, `MarkdownWriter`. All three share `TableSchema` (column definitions) and
`AuditLog`, ensuring that substituting the writer in evaluation does not change record semantics.

## 7. Data Model

```python
SourceType = Literal["official_email", "course_announcement", "instructor_chat", "student_chat"]

@dataclass
class RawMessage:
    message_id: str
    source_name: str
    source_type: SourceType
    sender: str
    published_at: datetime
    subject: str
    body: str
    evidence_path: str | None      # screenshot/step json under the real-device path; None offline
    thread_id: str | None

@dataclass
class DeadlineCandidate:
    candidate_id: str
    task_name_raw: str
    course: str | None
    deadline_raw: str | None
    deadline_iso: str | None       # ISO-8601
    missing_fields: list[str]
    unresolved_time: bool
    source_type: SourceType
    source_name: str
    published_at: datetime
    message_id: str

@dataclass
class TaskCluster:
    cluster_id: str
    task_name: str                 # normalized name
    course: str | None
    candidates: list[DeadlineCandidate]

@dataclass
class ResolvedRecord:
    task_name: str
    course: str | None
    deadline_iso: str | None
    status: Literal["confirmed", "uncertain", "missing_deadline"]
    source: str                    # source description of the winning evidence
    sources: list[str]             # all original sources (after deduplication)
    evidence_ids: list[str]
    confidence: float
    resolution_note: str           # conflict resolution note (why this one was chosen)
```

Document columns: `Task Name | Course | Deadline (ISO-8601) | Status | Source | Confidence | Evidence | Last Updated`.
Audit table columns: `Timestamp | Action(insert/update/rollback) | Task Name | Old Value | New Value | Reason`.

## 8. Key Algorithms

### 8.1 Time Normalization

- Supported: `YYYY-MM-DD`, `MM/DD`, `Nov 20`, `11月20日`, `2026-11-20 23:59`;
  relative: `今天/明天/后天/本周五/下周五/3天后/两天后`, `today/tomorrow/next Friday/in 3 days/EOD`.
- All relative times are based on the injected `now` + `Asia/Shanghai`, guaranteeing reproducible offline results.
- A date without a time → default `23:59`, and note "defaults to end of day" in `resolution_note`.
- Parse failure → `deadline_iso=None`, `unresolved_time=True`, final status `uncertain`, never guessing.

### 8.2 Deduplication

1. Task-name normalization: lowercase, strip punctuation, `HW1/Homework 1/Assignment 1/作业一` → uniformly `assignment 1`; strip course prefixes.
2. Similarity: `score = 0.6 * token_jaccard + 0.4 * difflib_ratio`, computed on normalized names.
3. Threshold `>= 0.78` (configurable) and the same course → same cluster; otherwise separate clusters. At a tie near the threshold, take the one with more evidence to avoid false merging.
4. Semantic matching is an interface: the default is lexical + character-level similarity (offline, deterministic); an embedding implementation can be injected.

### 8.3 Conflict Resolution

Credibility order (high → low): `official_email(4) > course_announcement(3) > instructor_chat(2) > student_chat(1)`.

When an LLM arbiter is injected (default Agent-LLM configuration): conflicts are first given to the model, which selects one of the existing candidate times and gives a reason and confidence;
confidence < 0.6, refusal, or fabricating a time outside the candidates → not adopted, supplementary retrieval continues, and finally marked `uncertain`.
Without an injected arbiter (rule version/no key), the following deterministic rules apply:

```
candidates → deduplicate by deadline_iso
  all equal → confirmed
  differ:
    consistent within the highest-credibility group → confirmed (note: a high-credibility source overrides a lower one)
    inconsistent within the highest-credibility group → compare published_at, newest wins (note: a newer announcement from the same source supersedes the old one)
    still tied / undecidable → supplementary retrieval (rounds 1 and 2, add keywords to sources related to the same task)
        results consistent → confirmed
        still inconsistent after two rounds → uncertain (keep all candidates, hand to manual review)
```

`confidence`: consistent=0.95; credibility override=0.85; newer time overrides=0.75; manual review=0.4; LLM adjudication takes the model's self-reported confidence.

### 8.4 Document Writing

- Match key: `(course or "", normalize(task_name))`; if hit, modify the row, otherwise append.
- Transactional write: copy the current file to `*.bak` before writing; delete the backup after a successful write.
- Write failure → retry once; if it fails again → restore from `.bak`, `WriteResult.ok=False` with the exception information.
- Every insert/update/rollback appends an audit row; an audit write failure is treated as an overall failure.

## 9. Directory Structure and Persistence

```
ddl_agent/
  __init__.py
  models.py
  settings.py                 # thresholds, credibility, time zone, dataset paths
  planner.py
  pipeline.py
  connectors/{base,local_eml,local_chat,graph_mail,mobile_chat}.py
  semantic.py                 # lexical + local embedding similarity (offline-degradable)
  extract/{rules,normalizer,llm_extractor}.py
  llm/{client,planner,arbiter}.py
  factory.py                  # LLM_ENABLED switch and rule fallback
  dedupe.py
  conflict.py
  writer/{base,xlsx_writer,csv_writer,markdown_writer,audit}.py
  output.py                   # data.json / .ics / calendar.html
  monitor/{config,cursor,sources,runner,cli}.py
  dataset/generator.py        # three difficulty tiers × 20–30 entries, train/val/test
  evaluation/{metrics,baseline,run_eval}.py
tests/test_ddl_*.py
experiments/ddl_agent/dataset/   # synthetic dataset and ground truth (committed for reproducibility)
experiments/ddl_agent/results/   # evaluation output (ignored by .gitignore)
```

## 10. Synthetic Dataset and Evaluation Protocol

### 10.1 Dataset

- Three difficulty tiers, 20–30 samples each (`easy`: direct extraction; `medium`: duplicate entries + missing fields;
  `hard`: conflicting times + old DDL superseded by new values + relative time/time zone/ambiguity + multi-task interference).
- Fact-driven: scenarios first define tasks, courses, real deadlines and conflict winners; ground truth is derived directly from the facts,
  and text is only responsible for rendering, not for deriving ground truth.
- Noise: quotation chains (`> `), `---------- Forwarded message ---------`, signatures, disclaimers, irrelevant chit-chat,
  expired old DDLs, other people's course tasks, exam/class times, formats not covered by rules such as `27 Nov 2026` and Chinese dates,
  `the Friday after next` / `月底前` / `考完试那周` / `PST` time zones, etc.
- Each sample: `sample_id`, `tier`, `query`, `source_config`, `messages[]` (email/chat mixed), `ground_truth[]`.
- Distractor sources: each sample additionally attaches 4 query-irrelevant sources (internship-referral group, family group, club-activity group, other course group),
  containing decoys that look like DDLs such as "application deadline / meeting sign-up deadline / other course assignment deadline". They **do not enter ground truth**,
  which is still derived only from facts; their purpose is to test whether dynamic source selection excludes irrelevant sources, and whether fixing the sources costs more extraction.
- `ground_truth` fields: `task_name`, `course`, `deadline_iso`, `status`, `expected_sources[]`, `conflict_expected` (bool), `winner_reason`.
- Split: stratified by tier `train/val/test = 60%/20%/20%`, fixed random seed, with the `split` field written at generation time to avoid prompt-tuning leakage.
- Generator deterministic (fixed seed) and rebuildable; no hand-written fictional "measured metrics".

### 10.2 Metrics

| Metric | Definition |
|---|---|
| Exact match | Proportion where the task name (normalized) and the deadline both match |
| Fuzzy match | Task name matches and the time difference is ≤ 1 day |
| Recall | Number of ground-truth entries hit / total number of ground-truth entries |
| Conflict accuracy | Among samples with `conflict_expected=true`, the proportion where the final time equals the expected winner |
| Duplicate rate | Extra rows for the same ground-truth task in the output / total output rows |
| Doc validity | All columns present, required fields non-empty, valid ISO format, audit table exists with matching row counts |
| Efficiency | Wall-clock time, LLM call count, token estimate, cost converted at unit prices |

### 10.3 Baseline

`LLMSinglePassBaseline`: concatenates the same batch of `RawMessage` into **one** prompt and produces records directly in one call,
without planning, deduplication, conflict checking or audit writing. `SinglePassBaseline` is the rule-based fallback under the same protocol,
used when there is no key. Token/cost are counted with the same price table.

### 10.4 Semantic Similarity: lexical / embedding / combined

- Interface: `SemanticMatcher(strategy, threshold)` returns a similarity in 0–1; deduplication and metric matching share the same implementation.
- Three strategies:
  - `lexical`: after task-name normalization, token Jaccard 0.6 + character-sequence similarity 0.4; threshold 0.78.
  - `embedding`: local `sentence-transformers` (default `paraphrase-multilingual-MiniLM-L12-v2`) cosine; threshold 0.6.
  - `combined`: takes `max(lexical, embedding)`; passing the threshold on either channel is enough to merge; it is not a weighted average.
- Degradation: if `sentence_transformers` is not installed, the model is not downloaded, loading fails, or the environment is offline, `similarity()` returns the lexical score,
  the report sets `similarity.available=false` and writes `fallback_reason`; evaluation and offline tests continue to completion without interruption.
- Dependencies and cache: `sentence-transformers` is in `requirements-embedding.txt` (optional), not in the main dependencies or CI;
  the model and framework cache default to `~/.cache/ddl-agent/sentence-transformers` (overridable with `DDL_EMBEDDING_CACHE`); the code sets `HF_HOME`,
  `SENTENCE_TRANSFORMERS_HOME` and `TORCH_HOME` before loading, without hardcoding any machine's drive letter.
- Offline environment: copy the whole cache directory to the target machine, or set `DDL_EMBEDDING_CACHE` to point to an existing directory while keeping the model name consistent;
  the evaluation command line can override with `--embedding-model` / `--embedding-cache` / `--dedupe-threshold`.
- Real-model test cases are skipped by default; set `RUN_EMBEDDING_TESTS=1` to execute them.

## 11. Verification Status Annotations

| Capability | Status |
|---|---|
| Local `.eml` / chat archive connectors, extraction, normalization, deduplication, conflict, writing, evaluation | Implemented; offline tests actually run |
| DeepSeek planning / extraction / conflict adjudication | Actually called (synthetic data); usage and cost written into the report |
| Graph mail connector | Not on the main path; only a "future optional" skeleton retained, unverified |
| Semantic matching (local embedding) | Implemented and runs offline; falls back to lexical when the model is missing |
| WeChat/QQ real-device reading | The WeChat single-device self-chat conversation has had read-only verification (`docs/real-device-test-report.md`); QQ and real group chats unverified |
| Overall performance on real email/chat | **unverified** |

### 11.1 Phase 4 Three-Way Measured Test (2026-11-18, synthetic data)

Command: `python -m ddl_agent.evaluation.run_eval --split test`
(test = 15 samples / 16 ground-truth entries, not used in any tuning).
All three systems use exactly the same raw messages.

| Metric | Agent-LLM | Agent-Rules | baseline single-pass |
| --- | --- | --- | --- |
| Exact match | **0.8125** | 0.5625 | 0.5000 |
| ±1-day fuzzy match | **0.8125** | 0.6250 | 0.5000 |
| Recall | **1.0000** | 0.8125 | 0.5625 |
| Duplicate rate | **0.0000** | 0.0000 | 0.1923 |
| Conflict resolution accuracy | **1.0000** (3 conflicts) | 0.3333 (3) | 0.6667 (3) |
| Document structure validation pass rate | **1.0000** | 1.0000 | not applicable |
| Output rows | 16 | 16 | 26 |
| LLM calls | 38 | 0 | 15 |
| token estimate | 15956 | 831 (character estimate) | 5346 |
| Cost estimate (USD) | 0.007385 | 0 | 0.002623 |
| Average time (s/sample) | 2.67 | 0.015 | 0.86 |

Full 72 samples (train+val+test, reference for stability only, not used for tuning):
Agent-LLM exact 0.7733 / recall 1.0000 / duplicate rate 0 / conflict accuracy 1.0000;
Agent-Rules 0.5200 / 0.8533 / 0 / 0.3636;
baseline 0.5200 / 0.6533 / 0.2051 / 0.7273.
Agent-LLM made 179 calls, 75108 tokens, about $0.0349.

Notes:
- This table is the three-way comparison **before adding distractor sources**; after adding distractor sources, Agent-LLM's exact/recall/conflict accuracy are unchanged,
  only the source configuration gets longer and tokens slightly increase (full from 15968 to 16647). Current RQ4 and similarity comparison numbers are in
  Section 8 of `docs/proposal-compliance.md`.
- Exact/fuzzy match does not reach 1.0 because some ground truths themselves have no concrete time
  (missing / uncertain), which are not counted in the time hit but are counted in recall.
- All numbers above come from **synthetic data**; real emails and real WeChat/QQ records are not yet verified,
  so the conclusions hold only on this generator's scenario distribution.
- Cost is approximately converted from DeepSeek's public prices, not the billed amount.

## 12. LLM Integration and Degradation

- Client: `ddl_agent/llm/client.py` reads only `config.PROVIDERS["deepseek"]` (key from `.env`),
  retries once on failure, and accumulates token, time and cost for each call.
- Planning: `LLMQueryPlanner` outputs the sources to query, course filter words, time window, and whether supplementary retrieval is allowed;
  on invalid output it falls back to `QueryPlanner`.
- Extraction: `LLMDeadlineExtractor` outputs task name/course/time phrase/ISO/confidence/whether review is needed;
  when ISO is invalid it falls back to local normalization; when JSON is invalid or the network is down, the whole message falls back to `RuleExtractor`.
- Adjudication: `LLMConflictArbiter` can only choose one of the existing candidates; low confidence/refusal/fabricated time → uncertain.
- Switch: `LLM_ENABLED` (on by default; set to `0` to force offline); without a key, `factory.build_agent` automatically returns the rule version;
  all unit tests and `--no-llm` evaluation do not access the network.

## 13. Milestones

1. Design review (this document).
2. Package skeleton + data model + normalization + extraction + deduplication + conflict + writer, with unit tests.
3. Dataset generator + split + ground truth.
4. Baseline + evaluation script, producing an offline evaluation report.
5. LLM planning/extraction/adjudication integration, dataset de-templating, three-way comparison (done, see §11.1).
6. (Follow-up) Real-device chat reading, re-testing on real data and ablation experiments; Graph integration is only an **optional non-main-path implementation** and is not scheduled in the current milestones.

## 14. Phone-First Architecture (Phase 5, current direction)

### 14.1 Data Sources and Localization

- The main path has only three kinds of phone sources: `wechat` (group/private chat), `qq` (group chat), `outlook` (App inbox).
  All are implemented uniformly by `MobileAppSource`, reusing the existing `ScreenAgent`: screenshots/OCR/view tree/tap/device lock/evidence are all handled by the phone layer.
- Locate by target name, not by hardcoded coordinates: the task explicitly requires "search for the target name inside the App → open the conversation → scroll up → read message by message",
  and the agent decides search, pinning, list scrolling and other operations by itself.
- When the target conversation is not found, throw `TargetNotFound` and record `source_errors` in the run result; **do not silently skip**, and do not substitute another conversation.
- Offline test stand-ins: `LocalEmlConnector` / `LocalChatConnector` are retained, used only when there is no phone;
  Graph is not on the main path, and only an unenabled code skeleton is kept as a "future optional implementation".

### 14.2 Natural-Language Retrieval Configuration

- New `monitor/config.py`; the configuration is persisted as `sources.yaml`, and each source stores:
  `id / app / kind / target / source_type / enabled / lookback_days / nl_text / parse_note`.
- `nl_text` stores the original natural language, and `parse_note` records the parsed key fields, satisfying the traceability requirement of "which sentence this configuration came from".
- Supports `add / list / edit / remove / enable / disable`; when `add` parsing fails, it explicitly rejects with the reason
  (for example, no App specified, no target name given, or multiple conflicting targets appearing at once), **without guessing**.
- The schedule period is also parsed from natural language (e.g. "every 6 hours", "once a day"); a parse failure reports an error directly and does not apply a default.

### 14.3 Increment, Deduplication and Scheduling

- State directory (default `state/`):
  - `cursors.json`: per-source `last_timestamp`, `last_message_id`, `baseline_done`;
  - `messages.jsonl`: raw messages already fetched (kept across cycles for reuse in conflict resolution);
  - `candidates.jsonl`: extracted candidates (the LLM processes only new messages, and old candidates are reused directly).
- Each cycle only **fetches** new messages after the cursor; the first run performs a baseline scan (bounded by `lookback_days`).
  The cursor advances after a source is fetched successfully; on failure it does not advance and is retried next cycle. The cursor can be reset manually (`monitor reset-cursor`).
- Cross-cycle deduplication is handled uniformly by the writer: the same `(course, normalized task name)` hit modifies the row; if neither content nor status changed, it is skipped.
  Each cycle's audit records `inserted / updated / skipped`.
- Scheduling: `monitor/runner.py` provides `run_cycle()`; `monitor/cli.py` provides the resident loop
  `python -m ddl_agent.monitor.cli run --config sources.yaml --out output --state state`,
  and a `--once` single-cycle mode. It does not depend on any external cloud service.

### 14.4 Output

| Artifact | Description |
|---|---|
| `deadlines.xlsx` | Default structured document, containing two sheets: Deadlines and Audit Log |
| `data.json` | Flat structure for the web page, fully rewritten each cycle |
| `deadlines.ics` | Importable into the system calendar; one VEVENT per confirmed record + VALARM 1 day in advance |
| `calendar.html` | Self-contained calendar page (data embedded, double-click to open offline), rewritten each cycle, auto-reloads every 2 minutes after opening |
| Phone system calendar | `PhoneCalendarWriter` interface reserved, **off by default**, to be enabled after real-device verification |

### 14.5 Module Differences

| Module | Change |
|---|---|
| `connectors/local_eml.py`, `local_chat.py` | Downgraded to offline test stand-ins, interface unchanged |
| `connectors/mobile_chat.py` → `monitor/sources.py` | Upgraded to the main path: WeChat/QQ/Outlook App, locate by name, explicit error when not found |
| `connectors/graph_mail.py` | Not on the main path; only a "future optional" skeleton retained |
| `monitor/config.py` | New: natural-language configuration, persistence, CRUD, explicit rejection |
| `monitor/cursor.py` | New: cursor, message store, candidate store |
| `monitor/runner.py`, `monitor/cli.py` | New: single-cycle/resident scheduling and result summary |
| `monitor/launcher.py`, `run_ddl_monitor.bat` | New: double-click launch, environment checks, plain-language errors, single-cycle/resident, open the calendar page |
| `output.py` | New: `data.json`, `.ics`, `calendar.html` (phone calendar writing disabled by the read-only constraint) |
| `pipeline.py` / `writer/` | Reused; the writer already supports row modification/skipping and auditing |

### 14.6 Verification Boundaries

- Phone GUI reading: WeChat single-device, single-account, self-chat conversation has had read-only verification (`docs/real-device-test-report.md`); QQ, real group chats and the Outlook App inbox are still unverified. Phone system calendar writing has been disabled by the read-only constraint and no write path is provided.
- Increment, cross-cycle updates, scheduling, configuration parsing, `data.json` / `.ics` export: offline tests can actually run.
- Graph is not on the main path; only an unenabled code skeleton is retained; local connectors serve only as offline test stand-ins, and none of them represents real source behavior.

### 14.7 Per-Step Model Requests and Timing Basis

| Mode | Per-step vision requests | Description |
|---|---|---|
| Old flow (4 calls) | decision + semantics + observation + completion check | semantics off by default; completion check merged into decision by default |
| Two-stage (`MERGED_OBSERVE=0`) | 2 | decision + observation; completion is judged from the decision response |
| Merged observation (`MERGED_OBSERVE=1`, default) | **1** | one response returns both `observation` and `action` |
| Invalid merged response | 3 | 1 failed merge + observe + decide; the action is still executed only once |

Timing basis:
- **Measured**: RapidOCR on a 1080×2374 screenshot takes 1.2–1.5s/image; DeepSeek decision with an image takes about 3.5–5s; fixed settle defaults to 1.0s (from the real-device report and phase measurements).
- **Estimated**: after merging observation, a whole step takes about 5–7s (the old two-stage flow about 8–10s); no real-device A/B was done; OCR preheating saves 6–7s on the first step, and adaptive settle saves at most about 1s per step.
- When `COMPLETION_CHECK=every_step`, merged observation is not enabled, avoiding an extra task_done request and degradation.

### 14.8 How to Read Token and Timing Data

- Each step archives `step_NNN.json`: `usage` is the summary of that step's vision calls (`calls[].kind` = decision/observe/completion), and is `null` when there is no usage; `timings` holds seconds per stage.
- At task end/max steps, `usage_summary.json` is written: total tokens, per-step mean, grouped by provider; `calls_detail` keeps the kind, model and usage of every call.
- Stage statistics script: `experiments/summarize_timings.py runs/<run>`; with multiple directories it outputs a mean/P90 comparison table for `MERGED_OBSERVE` A/B.
- Evidence export: `experiments/export_evidence.py runs/<run>` keeps only publishable numbers and hashes, deletes screenshots and raw text, and puts the artifacts in `docs/evidence/`.

## 15. Explicit Deviations from the Proposal

See [`docs/proposal-compliance.md`](proposal-compliance.md) for the complete item-by-item comparison. Only three differences that are easy to blur and must be stated explicitly are recorded here:

1. **Outlook access method**: the proposal says Graph API; the current **only main path is phone Outlook App GUI screen reading**,
   and Graph is not on the configuration, planning, retrieval or evaluation path; only an unenabled code skeleton is kept as a "future optional implementation".
   This is a deliberate design decision, **not "both are supported and can be switched freely"**.
   Reason: unify all sources onto the phone layer, reuse localization/evidence/device lock, and avoid Graph tenant credentials that the course environment cannot provide stably.
   Cost: reading speed, structure and attachment capability are weaker than Graph.
2. **ground truth source**: the proposal says "two team members annotate independently and discuss disagreements"; currently the generator first constructs facts and then derives
   ground truth from the facts, equivalent to "controllable, reproducible annotation", but without independent manual annotation. Limitation: errors in the generator's fact definitions cannot be found,
   and text and rules/prompts may share patterns, with a risk of self-fulfilment.
3. **RQ5 user study**: a manual step, currently **not done**, and the team needs to arrange questionnaires/interviews and a comparative demonstration; do not write it as completed in the report.
