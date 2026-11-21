"""查询聚焦：从查询里抽出目标任务/课程，用于选源与最终记录过滤。

原则是「有明确目标才过滤，没有明确目标保守放行」：
- 只把带序号的任务（作业三/实验报告二/Assignment 3/Final Report）当成明确目标；
- 引号里像任务的短语（「数据库作业」）也算明确目标，无匹配时返回空结果并标
  ``no_match``，不再用无关记录凑数；
- 查询问「最近一条作业/课程 DDL」这类泛称时，按最近一次通知取一条，不扩大过滤；
- 查询里没有明确任务时不裁剪记录，避免 D15 这类「只报课程 DDL」的题被误清空；
- 有明确任务但一条都不匹配时输出空结果并标 ``no_match``，不再用无关记录凑数。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from .dedupe import normalize_task_name
from .extract.courses import find_courses, normalize_course
from .settings import TZ

_TASK_PATTERNS = [
    re.compile(r"[\u4e00-\u9fff]{0,10}作业\s*[一二三四五六七八九十\d]+"),
    re.compile(r"实验报告\s*[一二三四五六七八九十\d]+"),
    re.compile(r"课程设计\s*[一二三四五六七八九十\d]*"),
    re.compile(r"小组课程报告"),
    re.compile(r"期末报告"),
    re.compile(r"Final\s*Report", re.IGNORECASE),
    re.compile(r"Assignment\s*\d+", re.IGNORECASE),
]
_TASK_ALIASES = {"期末报告": "final report"}
_COURSE_RESTRICT_RE = re.compile(r"(只报告|只查|只看|只要|仅|不要报|别报|不报)")
_CLAUSE_SPLIT_RE = re.compile(r"[，。；;！？（）()]")
_DISTRACTOR_RE = re.compile(r"(旧|不要|别报|不是|诱饵|干扰|还有|误|错误|注意不要)")
# 引号里带这些词才当成任务名；「2026 计算机班级群」「LLM小组」不会被误当任务
_QUOTED_RE = re.compile(r"[「“\"]([^」”\"]{2,40})[」”\"]|‘([^’]{2,40})’")
_QUOTED_TASK_RE = re.compile(
    r"(作业|报告|assignment|homework|project|lab|quiz)", re.IGNORECASE
)
# 泛称查询：没有点名第几份作业，但要求“最近一条/课程 DDL”
_GENERIC_DDL_RE = re.compile(r"(作业|报告|ddl|截止)", re.IGNORECASE)
_LATEST_RE = re.compile(r"(最近|最新|上周|本周|近期)")
_NUMBERED_HOMEWORK_RE = re.compile(
    r"(作业|实验报告|assignment|homework|hw|lab|quiz)\s*[一二三四五六七八九十0-9]+",
    re.IGNORECASE,
)


def task_hints(query: str) -> list[str]:
    """抽出查询里明确点名的任务；「课程作业」「同一作业」这类泛称不算。

    含「旧时间/不要报/诱饵」等干扰线索的分句不作为目标任务，避免把
    D08 里同群作业二这种反向例子也当成要输出的任务。
    """
    hints: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(query or ""):
        if _DISTRACTOR_RE.search(clause):
            continue
        for pattern in _TASK_PATTERNS:
            for match in pattern.finditer(clause):
                text = match.group(0).strip()
                if text and _canonical_task(text) not in {
                    _canonical_task(item) for item in hints
                }:
                    hints.append(text)
        for match in _QUOTED_RE.finditer(clause):
            text = (match.group(1) or match.group(2) or "").strip()
            if not text or not _QUOTED_TASK_RE.search(text):
                continue
            if _canonical_task(text) not in {_canonical_task(item) for item in hints}:
                hints.append(text)
    return hints


def wants_latest_homework(query: str) -> bool:
    """查询是否在问“最近一条作业/课程 DDL”这类泛称，而不是点名某份作业。"""
    if task_hints(query) or not _GENERIC_DDL_RE.search(query or ""):
        return False
    if _LATEST_RE.search(query or ""):
        return True
    if wants_course_restriction(query):
        return True
    return bool(re.search(r"(作业通知|作业截止|课程作业)", query or ""))


def wants_course_restriction(query: str) -> bool:
    return bool(_COURSE_RESTRICT_RE.search(query or ""))


def _restricted_course_codes(query: str) -> list[str]:
    """「只报告 COMP7503，不要报 COMP7415」只取肯定侧的课程代码。"""
    positive = re.split(r"(不要报|别报|不报)", query or "")[0]
    return find_courses(positive) or find_courses(query or "")


def _canonical_task(text: str) -> str:
    aliased = _TASK_ALIASES.get((text or "").strip(), text or "")
    return normalize_task_name(aliased)


def task_matches(hint: str, task_name: str) -> bool:
    left = _canonical_task(hint)
    right = _canonical_task(task_name)
    if not left or not right:
        return False
    if left == right:
        return True
    return len(left) >= 3 and (left in right or right in left)


def _record_time_key(record) -> tuple[datetime, int]:
    """记录最近一次证据的时间；缺失时排在最旧，避免“最近一条”选到无时间记录。"""
    published = getattr(record, "latest_published_at", None)
    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published)
        except ValueError:
            published = None
    if not isinstance(published, datetime):
        published = None
    if published is not None and published.tzinfo is None:
        published = published.replace(tzinfo=TZ)
    return (
        published or datetime.min.replace(tzinfo=TZ),
        int(getattr(record, "latest_source_order", None) or 0),
    )


def focus_records(query: str, records: list) -> tuple[list, dict]:
    """按查询里的明确任务/课程裁剪最终记录；无明确目标时原样返回。"""
    hints = task_hints(query)
    scoped_course = bool(re.search(r"COMP\d{4}\s*的", query or "", re.IGNORECASE))
    if wants_course_restriction(query):
        codes = _restricted_course_codes(query)
    elif scoped_course:
        # 「COMP7415 的期末报告」这种所有格直接把范围限定到该课程
        codes = find_courses(query)
    else:
        codes = []
    restrict_course = bool(codes)

    decision = {
        "task_hints": hints,
        "course_codes": codes,
        "course_restriction": restrict_course,
        "task_filter": "disabled",
        "course_filter": "disabled",
        "no_match": False,
        "before": len(records),
        "after": len(records),
        "dropped": [],
    }
    if not hints and not restrict_course and not wants_latest_homework(query):
        return list(records), decision

    filtered = list(records)
    if hints:
        matched = [
            record for record in filtered
            if any(task_matches(hint, getattr(record, "task_name", "")) for hint in hints)
        ]
        if matched:
            filtered = matched
            decision["task_filter"] = "applied"
        else:
            # 明确点名了任务却一条都没命中：输出空结果并标 no_match，不用无关记录凑数
            decision["task_filter"] = "no_match"
            decision["no_match"] = True
            decision["after"] = 0
            decision["dropped"] = [
                getattr(record, "task_name", "") for record in records
            ]
            return [], decision

    if restrict_course:
        wanted = {normalize_course(code) for code in codes}
        course_matched = [
            record for record in filtered
            if not getattr(record, "course", None)
            or normalize_course(getattr(record, "course")) in wanted
        ]
        if course_matched:
            filtered = course_matched
            decision["course_filter"] = "applied"
        else:
            decision["course_filter"] = "no_match_keep_all"

    if not hints and wants_latest_homework(query):
        # 「最近一条作业/课程 DDL」：只保留最近一次编号作业通知，避免多报同群旧任务
        numbered = [
            record for record in filtered
            if _NUMBERED_HOMEWORK_RE.search(getattr(record, "task_name", "") or "")
        ]
        if numbered:
            latest = max(numbered, key=_record_time_key)
            filtered = [latest]
            decision["task_filter"] = "latest_homework"
            decision["latest"] = getattr(latest, "task_name", "")
        else:
            decision["task_filter"] = "latest_homework_no_match"

    kept = {id(record) for record in filtered}
    decision["after"] = len(filtered)
    decision["dropped"] = [
        getattr(record, "task_name", "") for record in records if id(record) not in kept
    ]
    return filtered, decision


# ---------------------------------------------------------------- 选源
def _compact(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _recent(messages: list, limit: int) -> list:
    def key(message):
        return (
            getattr(message, "published_at", None) or datetime.min.replace(tzinfo=TZ),
            getattr(message, "source_order", None) or 0,
        )

    return sorted(messages, key=key, reverse=True)[:limit]


def _hint_variants(hint: str) -> set[str]:
    """把任务名扩展成中英/缩写变体，供粗扫时匹配同义写法（Assignment 2 ↔ HW2）。"""
    variants = {hint}
    normalized = normalize_task_name(hint)
    match = re.fullmatch(
        r"(assignment|homework|hw|lab|report|project|essay|exam)\s*(\d+)", normalized
    )
    if not match:
        return variants
    word, number = match.group(1), int(match.group(2))
    chinese = _int_to_chinese(number)
    variants.update({
        f"assignment {number}", f"homework {number}", f"hw{number}", f"hw {number}",
        f"{word} {number}", f"作业{number}", f"作业{chinese}",
    })
    return variants


def _int_to_chinese(number: int) -> str:
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number == 10:
        return "十"
    if number < 20:
        return "十" + digits[number - 10]
    return str(number)


def _hint_in_text(hint: str, text: str) -> bool:
    lowered = (text or "").lower()
    return any(variant.lower() in lowered for variant in _hint_variants(hint))


def select_sources_by_content(query: str, source_messages: list,
                              fallback_sources: list | None = None,
                              recent: int = 8) -> tuple[list, dict]:
    """粗扫每个候选来源最近消息，按「群名命中 + 任务命中 + 课程命中」选源。

    只做规则打分，不新增 LLM 调用；返回 (选中的来源, 决策记录)。
    """
    hints = task_hints(query)
    codes = find_courses(query)
    compact_query = _compact(query)
    scored: list[dict] = []

    for source, messages in source_messages:
        name = str(getattr(source, "name", source))
        recent_messages = _recent(list(messages or []), recent)
        text = "\n".join(
            f"{getattr(message, 'subject', '')}\n{getattr(message, 'body', '')}"
            for message in recent_messages
        )
        task_hits = [hint for hint in hints if _hint_in_text(hint, text)]
        course_hits = sorted(set(find_courses(text)) & set(codes))
        name_hit = bool(_compact(name)) and _compact(name) in compact_query
        scored.append({
            "name": name,
            "name_hit": name_hit,
            "task_hits": task_hits,
            "course_hits": course_hits,
            "score": 5 * int(name_hit) + 3 * len(task_hits) + len(course_hits),
            "message_count": len(messages or []),
            "probe_message_count": len(recent_messages),
        })

    if not scored:
        return [], {"enabled": True, "hints": hints, "courses": codes, "sources": []}

    name_matched = [item for item in scored if item["name_hit"]]
    task_matched = [item for item in scored if item["task_hits"]]
    if name_matched:
        # 查询点名了具体群/邮箱：以名字为准，避免同课跨群内容把别的群带进来
        chosen_names = {item["name"] for item in name_matched}
        reason = "查询点名了来源名称，直接按名称选源"
    elif task_matched:
        chosen_names = {item["name"] for item in task_matched}
        reason = "粗扫最近消息命中查询任务"
    else:
        best = max(item["score"] for item in scored)
        if best <= 0:
            fallback = {
                str(getattr(source, "name", source)) for source in (fallback_sources or [])
            }
            chosen_names = fallback or {item["name"] for item in scored}
            reason = "粗扫内容没有命中明确任务/课程，回退到规划器来源"
        else:
            chosen_names = {item["name"] for item in scored if item["score"] == best}
            reason = "按粗扫内容的课程命中选源"

    chosen = [
        source for source, _ in source_messages
        if str(getattr(source, "name", source)) in chosen_names
    ]
    decision = {
        "enabled": True,
        "hints": hints,
        "courses": codes,
        "sources": scored,
        "kept": sorted(chosen_names),
        "dropped": sorted(
            item["name"] for item in scored if item["name"] not in chosen_names
        ),
        "reason": reason,
        "llm_calls": 0,
        "recent_per_source": recent,
    }
    return chosen, decision

