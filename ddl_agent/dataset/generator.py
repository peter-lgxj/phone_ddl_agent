"""合成数据集生成器（事实驱动 + 噪声渲染）。

设计原则：
- 场景先定义事实（任务、课程、真实截止时间、冲突胜者），ground truth 直接由事实导出；
- 文本只负责把事实渲染成带噪声的真实邮件/群聊，不参与 ground truth 推导；
- 文本刻意包含引用链、转发、签名、闲聊、旧 DDL、考试时间、相对时间歧义和中英变体，
  避免规则抽取靠固定句式“自我应验”。

三档难度，每档默认 24 个样本（需求要求 20–30），按 tier 分层划分 train/val/test。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from ..settings import TZ

BASE_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)  # 周一
COURSES = ["COMP7608", "COMP7404", "STAT7001", "FITE7410", "COMP7506"]
TIERS = ("easy", "medium", "hard")

_SIGNATURES = [
    "Best regards,\nTA Chen\nDepartment of Computer Science",
    "Cheers,\nWei\nCourse coordinator",
    "Thanks,\nAlice",
    "Regards,\nTeaching Team",
]
_DISCLAIMERS = [
    "This email is intended only for enrolled students of the course.",
    "Please do not reply-all to this announcement.",
    "If you received this message in error, please ignore it.",
]
_CHITCHAT = [
    "By the way, is anyone going to the library tonight?",
    "Coffee chat after class still on?",
    "My laptop fan is dying, any repair shop recommendations?",
    "Anyone has notes for last week's lecture?",
]


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _at(day: datetime, hour: int = 23, minute: int = 59) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _pick(rng: random.Random, items: list[str]) -> str:
    return items[rng.randrange(len(items))]


# ---------------------------------------------------------------------- #
#  日期渲染（文本只是事实的一种表达）
# ---------------------------------------------------------------------- #
def _month_first(value: datetime) -> str:
    return value.strftime("%b %d, %Y %H:%M")


def _day_first(value: datetime) -> str:
    return value.strftime("%d %b %Y %H:%M")


def _cn_date(value: datetime) -> str:
    return f"{value.month}月{value.day}日 {value:%H:%M}"


def _iso_style(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


_DATE_RENDERERS = [_month_first, _day_first, _cn_date, _iso_style]


def _render_date(rng: random.Random, value: datetime) -> str:
    # day-first 与中文格式规则抽取覆盖不到，用于降低“模板可被正则直接命中”的比例
    return _pick(rng, [render(value) for render in _DATE_RENDERERS])


def _noise(rng: random.Random, *, signature: bool = True, disclaimer: bool = False,
           chitchat: bool = False) -> str:
    parts: list[str] = []
    if chitchat and rng.random() < 0.6:
        parts.append(_pick(rng, _CHITCHAT))
    if disclaimer and rng.random() < 0.5:
        parts.append(_pick(rng, _DISCLAIMERS))
    if signature and rng.random() < 0.7:
        parts.append(_pick(rng, _SIGNATURES))
    return "\n".join(parts)


def _quote(header: str, body: str) -> str:
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    return f"{header}\n{quoted}"


def _forward(original_from: str, original_date: datetime, subject: str, body: str) -> str:
    return (
        "---------- Forwarded message ---------\n"
        f"From: {original_from}\n"
        f"Date: {original_date.strftime('%a, %d %b %Y %H:%M:%S %z')}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )


def _email(message_id: str, course: str, published_at: datetime, subject: str,
           body: str, sender: str = "ta@cs.hku.hk") -> dict:
    return {
        "message_id": message_id,
        "source_name": "outlook",
        "source_type": "official_email",
        "sender": sender,
        "published_at": _iso(published_at),
        "subject": subject,
        "body": body,
    }


def _chat(message_id: str, course: str, published_at: datetime, sender: str,
          role: str, text: str, source_name: str | None = None) -> dict:
    return {
        "message_id": message_id,
        "source_name": source_name or f"{course} 课程群",
        "source_type": "instructor_chat" if role in ("instructor", "ta") else "student_chat",
        "sender": sender,
        "role": role,
        "published_at": _iso(published_at),
        "subject": "",
        "text": text,
    }


def _distractors(sample_id: str, course: str) -> list[dict]:
    """与查询无关、但可能被“固定查全部来源”误读的干扰消息。

    这些消息不进入 ground truth，也不改写任何事实；它们只在固定源序列的消融中
    制造额外的无关行，用来检验动态选源是否真的排除了无关来源。
    """
    other_course = next(item for item in COURSES if item != course)
    return [
        _chat(
            f"{sample_id}-d1", course, BASE_NOW - timedelta(hours=6),
            "内推小助手", "student",
            "【内推】某互联网公司 2026 暑期实习网申截止 12月15日 23:59，专业不限，欢迎投递。",
            source_name="实习内推群",
        ),
        _chat(
            f"{sample_id}-d2", course, BASE_NOW - timedelta(hours=5),
            "妈妈", "student",
            "这周日晚上 7 点回家吃饭，别迟到；顺便把上次的快递带回来。",
            source_name="家人群",
        ),
        _chat(
            f"{sample_id}-d3", course, BASE_NOW - timedelta(hours=4),
            "社团干事", "student",
            "本周三 18:00 社团例会，地点 CPD-3.04；报名截止周二中午 12:00。",
            source_name="社团活动群",
        ),
        _chat(
            f"{sample_id}-d4", course, BASE_NOW - timedelta(hours=3),
            "助教", "ta",
            f"{other_course} Assignment 9 截止 12月3日 23:59，请按时提交。",
            source_name=f"{other_course} 课程群",
        ),
    ]


def _ground_truth(task: str, course: str, deadline: datetime | None, *,
                  status: str = "confirmed", sources: tuple[str, ...] = (),
                  conflict: bool = False, reason: str = "") -> dict:
    return {
        "task_name": task,
        "course": course,
        "deadline_iso": _iso(deadline) if deadline else None,
        "status": status,
        "expected_sources": list(sources),
        "conflict_expected": conflict,
        "winner_reason": reason,
    }


# ---------------------------------------------------------------------- #
#  easy：单来源、可直接推断
# ---------------------------------------------------------------------- #
def _easy_email(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[index % len(COURSES)]
    task = ["Assignment 1", "HW 2", "Homework 3", "Lab 4"][index % 4]
    deadline = _at(BASE_NOW + timedelta(days=12 + index % 9))
    body = (
        f"Dear students,\n\n{task} for {course} is due on {_render_date(rng, deadline)}.\n"
        "The final exam timetable will be announced separately.\n\n"
        f"{_noise(rng, disclaimer=True)}"
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=4),
                       f"[{course}] {task}", body)]
    return messages, [_ground_truth(task, course, deadline, sources=("official_email",))]


def _easy_chat(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 2) % len(COURSES)]
    task = ["作业一", "作业二", "实验三", "报告四"][index % 4]
    deadline = _at(BASE_NOW + timedelta(days=17 + index % 6))
    text = (
        f"各位同学，{task}安排在 {_cn_date(deadline)} 前完成，逾期系统会关闭。\n"
        f"{_noise(rng, signature=False, chitchat=True)}"
    )
    messages = [_chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=2),
                      "王老师", "instructor", text)]
    return messages, [_ground_truth(task, course, deadline, sources=("instructor_chat",))]


def _easy_forward(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 1) % len(COURSES)]
    task = ["Project Proposal", "Assignment 5", "Lab 2"][index % 3]
    deadline = _at(BASE_NOW + timedelta(days=20 + index % 5))
    original = (
        f"Hi all,\n{task} submission opens next week. "
        f"The hard cut-off is {_day_first(deadline)}.\n"
        "Late submissions lose 10% per day."
    )
    body = (
        "FYI — forwarding the original note from the course office.\n\n"
        + _forward("course-office@hku.hk", BASE_NOW - timedelta(days=9),
                   f"[{course}] {task} timeline", original)
        + "\n\n"
        + _noise(rng, disclaimer=True)
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=8),
                       f"Fwd: [{course}] {task} timeline", body,
                       sender="coordinator@hku.hk")]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("official_email",))]


# ---------------------------------------------------------------------- #
#  medium：跨来源重复、信息缺失、引用链
# ---------------------------------------------------------------------- #
def _medium_duplicate(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[index % len(COURSES)]
    task = ["Assignment 2", "Homework 4", "Lab 1"][index % 3]
    variant = {"Assignment 2": "HW2", "Homework 4": "homework 4", "Lab 1": "实验一"}[task]
    deadline = _at(BASE_NOW + timedelta(days=21 + index % 4))

    email_body = (
        f"Dear all,\n\n{task} for {course} is due {_render_date(rng, deadline)}.\n"
        "For reference, HW0 was due on 2026-10-10 23:59 and is already closed.\n\n"
        + _noise(rng, disclaimer=True)
    )
    chat_text = (
        f"提醒一下，{variant} 的截止时间是 {_cn_date(deadline)}，"
        f"别和上周那个弄混。\n{_noise(rng, signature=False, chitchat=True)}"
    )
    messages = [
        _email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=6),
               f"[{course}] {task}", email_body),
        _chat(f"{sample_id}-m2", course, BASE_NOW - timedelta(days=1),
              "TA Chen", "ta", chat_text),
    ]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("official_email", "instructor_chat"))]


def _medium_missing(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 3) % len(COURSES)]
    task = ["Assignment 6", "作业五", "Report 2"][index % 3]
    messages = [
        _chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(hours=5),
              "Bob", "student",
              f"{course} {task} 到底什么时候交？助教说排期还没定，可能在期末周之后。\n"
              f"{_noise(rng, signature=False, chitchat=True)}"),
        _chat(f"{sample_id}-m2", course, BASE_NOW - timedelta(hours=4),
              "TA Chen", "ta",
              "还没最终确定，定了会在 Outlook 发正式通知，先别按旧日期准备。"),
    ]
    return messages, [_ground_truth(task, course, None, status="missing_deadline",
                                    sources=("student_chat", "instructor_chat"))]


def _medium_quote_chain(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 1) % len(COURSES)]
    task = ["Assignment 3", "Homework 1", "Project 2"][index % 3]
    deadline = _at(BASE_NOW + timedelta(days=16 + index % 5))
    original = (
        f"Hi,\n{task} for {course} is due {_month_first(deadline)}.\n"
        "Please upload a single PDF."
    )
    body = (
        "Adding the course office's note below so everyone has the same date.\n\n"
        + _quote(f"On {BASE_NOW - timedelta(days=12):%b %d, %Y}, course office wrote:", original)
        + "\n\n"
        + _noise(rng, disclaimer=True)
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=2),
                       f"Re: [{course}] {task}", body)]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("official_email",))]


# ---------------------------------------------------------------------- #
#  hard：冲突、取代、歧义、时区、多任务干扰
# ---------------------------------------------------------------------- #
def _hard_conflict(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[index % len(COURSES)]
    task = ["Assignment 2", "Homework 5", "Lab 3"][index % 3]
    official = _at(BASE_NOW + timedelta(days=22))
    rumored = _at(BASE_NOW + timedelta(days=24))
    email_body = (
        f"{task} for {course} is due {_month_first(official)}.\n"
        "The submission portal will close automatically.\n\n"
        + _noise(rng, disclaimer=True)
    )
    chat_text = (
        f"I asked around and heard {task} is actually due {_iso_style(rumored)}? "
        "Can anyone confirm?\n" + _pick(rng, _CHITCHAT)
    )
    messages = [
        _email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=7),
               f"[{course}] {task} deadline", email_body),
        _chat(f"{sample_id}-m2", course, BASE_NOW - timedelta(days=3),
              "David", "student", chat_text),
    ]
    return messages, [_ground_truth(task, course, official, sources=("official_email",),
                                    conflict=True, reason="官方邮件优先于同学消息")]


def _hard_superseded(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 1) % len(COURSES)]
    task = ["Assignment 4", "Homework 2", "Project 1"][index % 3]
    old = _at(BASE_NOW + timedelta(days=14))
    new = _at(BASE_NOW + timedelta(days=23))
    old_note = f"{task} for {course} was originally due {_day_first(old)}."
    new_body = (
        "We are extending the deadline after the typhoon warning.\n\n"
        f"{task} for {course} is now due {_month_first(new)}.\n\n"
        + _quote(f"On {BASE_NOW - timedelta(days=45):%b %d, %Y}, course office wrote:", old_note)
        + "\n\n"
        + _noise(rng, signature=True)
    )
    messages = [
        _email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=46),
               f"[{course}] {task}", old_note + "\n\n" + _noise(rng, signature=False)),
        _email(f"{sample_id}-m2", course, BASE_NOW - timedelta(days=9),
               f"[{course}] {task} deadline extended", new_body),
    ]
    return messages, [_ground_truth(task, course, new, sources=("official_email",),
                                    conflict=True, reason="同源新公告取代旧公告")]


def _hard_timezone(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 2) % len(COURSES)]
    task = ["Assignment 7", "Lab 5", "Report 3"][index % 3]
    # 美西时间 2026-12-03 23:59 => 北京时间 2026-12-04 15:59
    deadline = datetime(2026, 12, 4, 15, 59, tzinfo=TZ)
    body = (
        "The external host set the cut-off in their local time zone.\n"
        f"{task} must be uploaded before 2026-12-03 23:59 PST.\n"
        "Convert to your local time before submitting.\n\n"
        + _noise(rng, disclaimer=True)
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=5),
                       f"[{course}] {task} (external)", body)]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("official_email",))]


def _hard_month_end(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 3) % len(COURSES)]
    task = ["Assignment 8", "Homework 6", "Essay 1"][index % 3]
    deadline = datetime(2026, 11, 30, 23, 59, tzinfo=TZ)
    chat_text = (
        f"通知：{task} 请在月底之前提交，系统按北京时间 11 月最后一刻关闭。\n"
        f"{_noise(rng, signature=False, chitchat=False)}"
    )
    messages = [_chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=1),
                      "TA Chen", "ta", chat_text)]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("instructor_chat",))]


def _hard_after_next(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 4) % len(COURSES)]
    task = ["Assignment 9", "Homework 7", "Lab 6"][index % 3]
    # BASE_NOW 是周一；下周五 = +11 天，再下一周周五 = +18 天
    deadline = _at(BASE_NOW + timedelta(days=18))
    body = (
        f"{task} is due the Friday after next, end of day.\n"
        "I know the phrasing is confusing — plan for the later Friday, not this week's.\n\n"
        + _noise(rng, signature=False)
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=3),
                       f"[{course}] {task}", body, sender="instructor@cs.hku.hk")]
    return messages, [_ground_truth(task, course, deadline,
                                    sources=("official_email",))]


def _hard_ambiguous(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 1) % len(COURSES)]
    task = ["作业三", "Assignment 10", "Report 4"][index % 3]
    messages = [
        _chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(hours=10),
              "Alice", "student",
              f"{task} 什么时候交？助教说考完试那周再说，具体日期还没定。\n"
              + _pick(rng, _CHITCHAT)),
        _chat(f"{sample_id}-m2", course, BASE_NOW - timedelta(hours=9),
              "Bob", "student",
              "我听到的版本也一样，反正不是这周，等正式邮件吧。"),
    ]
    return messages, [_ground_truth(task, course, None, status="uncertain",
                                    sources=("student_chat",), conflict=True,
                                    reason="相对时间有歧义且无可信来源")]


def _hard_multi(sample_id: str, index: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    course = COURSES[(index + 2) % len(COURSES)]
    first_task = ["Assignment 11", "Homework 8"][index % 2]
    second_task = ["Lab 7", "Essay 2"][index % 2]
    first = _at(BASE_NOW + timedelta(days=19))
    second = _at(BASE_NOW + timedelta(days=26))
    body = (
        f"Two reminders for {course}:\n"
        f"1) {first_task} is due {_month_first(first)}.\n"
        f"2) {second_task} is due {_cn_date(second)}.\n"
        "The final exam will take place on 2026-12-15 09:30 in LTC.\n"
        "The revision lecture is on 2026-12-10 14:00.\n\n"
        + _noise(rng, disclaimer=True)
    )
    messages = [_email(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=4),
                       f"[{course}] two reminders", body)]
    return messages, [
        _ground_truth(first_task, course, first, sources=("official_email",)),
        _ground_truth(second_task, course, second, sources=("official_email",)),
    ]


_BUILDERS = {
    "easy": [_easy_email, _easy_chat, _easy_forward],
    "medium": [_medium_duplicate, _medium_missing, _medium_quote_chain],
    "hard": [_hard_conflict, _hard_superseded, _hard_timezone, _hard_month_end,
             _hard_after_next, _hard_ambiguous, _hard_multi],
}


def _source_config(messages: list[dict]) -> str:
    names = list(dict.fromkeys(
        message["source_name"] for message in messages if message.get("source_name")
    ))
    return "跟踪 " + "、".join(names)


def _sample(sample_id: str, course: str, query: str, messages: list[dict],
            ground_truth: list[dict]) -> dict:
    return {
        "sample_id": sample_id,
        "course": course,
        "now": _iso(BASE_NOW),
        "query": query,
        "source_config": _source_config(messages),
        "messages": messages,
        "ground_truth": ground_truth,
    }


def _split_for(index: int, per_tier: int) -> str:
    train_end = round(per_tier * 0.6)
    val_end = train_end + round(per_tier * 0.2)
    if index < train_end:
        return "train"
    if index < val_end:
        return "val"
    return "test"


def generate_samples(per_tier: int = 24, seed: int = 42) -> list[dict]:
    if not 20 <= per_tier <= 30:
        raise ValueError("每个难度档需要 20–30 条样本")
    samples: list[dict] = []
    for tier in TIERS:
        builders = _BUILDERS[tier]
        for index in range(per_tier):
            rng = random.Random(seed * 1000 + index + {"easy": 0, "medium": 100, "hard": 200}[tier])
            sample_id = f"{tier}-{index + 1:03d}"
            messages, ground_truth = builders[index % len(builders)](sample_id, index, rng)
            course = ground_truth[0]["course"]
            messages = [*messages, *_distractors(sample_id, course)]
            tasks = "、".join(item["task_name"] for item in ground_truth)
            query = f"帮我查一下 {course} 的 {tasks} 截止时间"
            sample = _sample(sample_id, course, query, messages, ground_truth)
            sample["tier"] = tier
            sample["split"] = _split_for(index, per_tier)
            samples.append(sample)
    return samples


def write_dataset(out_dir: str | Path, per_tier: int = 24, seed: int = 42) -> dict:
    root = Path(out_dir)
    samples = generate_samples(per_tier, seed=seed)

    for sample in samples:
        target = root / sample["split"] / f"{sample['sample_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

    index = {
        "created_from": "ddl_agent.dataset.generator (fact-based v2)",
        "seed": seed,
        "per_tier": per_tier,
        "base_now": _iso(BASE_NOW),
        "counts": {
            split: {
                tier: sum(1 for s in samples if s["split"] == split and s["tier"] == tier)
                for tier in TIERS
            }
            for split in ("train", "val", "test")
        },
        "samples": [
            {"sample_id": s["sample_id"], "tier": s["tier"], "split": s["split"],
             "ground_truth": len(s["ground_truth"])}
            for s in samples
        ],
    }
    (root / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return index


def load_samples(dataset_dir: str | Path, split: str | None = None,
                 tier: str | None = None, limit: int | None = None) -> list[dict]:
    root = Path(dataset_dir)
    samples: list[dict] = []
    for path in sorted(root.glob("*/*.json")):
        if path.parent.name not in ("train", "val", "test"):
            continue
        sample = json.loads(path.read_text(encoding="utf-8"))
        if split and sample.get("split") != split:
            continue
        if tier and sample.get("tier") != tier:
            continue
        samples.append(sample)
    return samples[:limit] if limit else samples


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成 DDL 合成数据集")
    parser.add_argument("--out", default="experiments/ddl_agent/dataset")
    parser.add_argument("--per-tier", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    result = write_dataset(args.out, args.per_tier, args.seed)
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
