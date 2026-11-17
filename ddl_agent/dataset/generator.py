"""合成数据集生成器。

三档难度，每档默认 24 个样本（需求要求 20–30），覆盖：
- easy：单来源直接抽取（绝对日期 / 中文日期 / 同学群消息）
- medium：同一任务跨来源重复（HW2 vs Assignment 2）、信息缺失（无日期）
- hard：冲突时间、旧公告被新公告取代、相对时间、无法判定（uncertain）
每个样本自带 ground truth，并按 tier 分层划分 train/val/test。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..settings import TZ

BASE_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
COURSES = ["COMP7608", "COMP7404", "STAT7001", "FITE7410", "COMP7506"]
TIERS = ("easy", "medium", "hard")


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _at(day: datetime, hour: int = 23, minute: int = 59) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _email(message_id: str, course: str, published_at: datetime,
           subject: str, body: str) -> dict:
    return {
        "message_id": message_id,
        "source_name": "outlook",
        "source_type": "official_email",
        "sender": "ta@cs.hku.hk",
        "published_at": _iso(published_at),
        "subject": subject,
        "body": body,
    }


def _chat(message_id: str, course: str, published_at: datetime, sender: str,
          role: str, text: str, source_type: str | None = None) -> dict:
    item = {
        "message_id": message_id,
        "source_name": f"{course} 课程群",
        "source_type": source_type or ("instructor_chat" if role in ("instructor", "ta") else "student_chat"),
        "sender": sender,
        "role": role,
        "published_at": _iso(published_at),
        "subject": "",
        "text": text,
    }
    return item


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
#  三档场景
# ---------------------------------------------------------------------- #
def _easy_absolute(sample_id: str, index: int) -> dict:
    course = COURSES[index % len(COURSES)]
    task = f"Assignment {1 + index % 6}"
    deadline = _at(BASE_NOW + timedelta(days=13 + index % 9))
    messages = [
        _email(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(days=3),
            f"[{course}] {task} deadline",
            f"Dear students,\n{task} for {course} is due on "
            f"{deadline:%Y-%m-%d %H:%M}. Please submit via Moodle.",
        )
    ]
    return _sample(sample_id, course, f"帮我查一下 {course} 的 {task} 截止时间", messages,
                   [_ground_truth(task, course, deadline, sources=("official_email",))])


def _easy_chinese(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 2) % len(COURSES)]
    task = f"Assignment {2 + index % 5}"
    deadline = _at(BASE_NOW + timedelta(days=17 + index % 7))
    text = (f"各位同学，{task} 的截止时间是 {deadline.month}月{deadline.day}日 23:59，"
            "请按时提交。")
    messages = [
        _chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=2),
              "王老师", "instructor", text),
    ]
    return _sample(sample_id, course, f"{course} {task} 什么时候截止？", messages,
                   [_ground_truth(task, course, deadline, sources=("instructor_chat",))])


def _easy_student(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 1) % len(COURSES)]
    task = f"Lab {1 + index % 4}"
    deadline = _at(BASE_NOW + timedelta(days=9 + index % 6))
    messages = [
        _chat(f"{sample_id}-m1", course, BASE_NOW - timedelta(days=1),
              "Alice", "student",
              f"DDL 提醒：{course} {task} due {deadline:%Y-%m-%d %H:%M}"),
    ]
    return _sample(sample_id, course, f"{course} 的 {task} DDL", messages,
                   [_ground_truth(task, course, deadline, sources=("student_chat",))])


def _medium_duplicate(sample_id: str, index: int) -> dict:
    course = COURSES[index % len(COURSES)]
    task = f"Assignment {3 + index % 4}"
    short = task.replace("Assignment", "HW")
    deadline = _at(BASE_NOW + timedelta(days=20 + index % 5))
    messages = [
        _email(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(days=5),
            f"[{course}] {task}",
            f"Dear students,\n{task} for {course} is due on {deadline:%Y-%m-%d %H:%M}.",
        ),
        _chat(
            f"{sample_id}-m2", course, BASE_NOW - timedelta(days=1),
            "TA Chen", "ta",
            f"Reminder: {short} deadline is {deadline:%Y-%m-%d %H:%M}, submit via Moodle.",
        ),
    ]
    return _sample(sample_id, course, f"{course} {task} 的截止时间", messages,
                   [_ground_truth(task, course, deadline,
                                  sources=("official_email", "instructor_chat"))])


def _medium_missing(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 3) % len(COURSES)]
    task = f"Assignment {4 + index % 3}"
    messages = [
        _chat(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(hours=6),
            "Bob", "student",
            f"{course} {task} 的截止时间还没定，TA 说下周会在邮件里通知。",
        )
    ]
    return _sample(sample_id, course, f"{course} {task} 什么时候截止", messages,
                   [_ground_truth(task, course, None, status="missing_deadline",
                                  sources=("student_chat",))])


def _hard_conflict(sample_id: str, index: int) -> dict:
    course = COURSES[index % len(COURSES)]
    task = f"Assignment {2 + index % 4}"
    official = _at(BASE_NOW + timedelta(days=21))
    rumored = _at(BASE_NOW + timedelta(days=23))
    messages = [
        _email(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(days=6),
            f"[{course}] {task} deadline",
            f"{task} for {course} is due on {official:%Y-%m-%d %H:%M}.",
        ),
        _chat(
            f"{sample_id}-m2", course, BASE_NOW - timedelta(days=4),
            "David", "student",
            f"I heard {task} deadline is {rumored:%Y-%m-%d %H:%M}?",
        ),
    ]
    return _sample(sample_id, course, f"{course} {task} 到底什么时候交", messages,
                   [_ground_truth(task, course, official, sources=("official_email",),
                                  conflict=True, reason="官方邮件优先于同学消息")])


def _hard_superseded(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 1) % len(COURSES)]
    task = f"Assignment {5 + index % 3}"
    old = _at(BASE_NOW + timedelta(days=15))
    new = _at(BASE_NOW + timedelta(days=22))
    messages = [
        _email(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(days=45),
            f"[{course}] {task}",
            f"{task} for {course} is due on {old:%Y-%m-%d %H:%M}.",
        ),
        _email(
            f"{sample_id}-m2", course, BASE_NOW - timedelta(days=10),
            f"[{course}] {task} deadline extended",
            f"Update: {task} for {course} is now due on {new:%Y-%m-%d %H:%M}.",
        ),
    ]
    return _sample(sample_id, course, f"{course} {task} 的最新截止时间", messages,
                   [_ground_truth(task, course, new, sources=("official_email",),
                                  conflict=True, reason="同源新公告取代旧公告")])


def _hard_relative(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 4) % len(COURSES)]
    task = f"Assignment {1 + index % 5}"
    delta = 7 - BASE_NOW.weekday() + 4  # 下周五
    deadline = _at(BASE_NOW + timedelta(days=delta))
    messages = [
        _email(
            f"{sample_id}-m1", course, BASE_NOW - timedelta(days=2),
            f"[{course}] {task}",
            f"{task} for {course} is due next Friday at 23:59.",
        )
    ]
    return _sample(sample_id, course, f"{course} {task} 下周五前要交吗", messages,
                   [_ground_truth(task, course, deadline, sources=("official_email",))])


def _hard_uncertain(sample_id: str, index: int) -> dict:
    course = COURSES[(index + 2) % len(COURSES)]
    task = f"Assignment {6 + index % 2}"
    first = _at(BASE_NOW + timedelta(days=18))
    second = _at(BASE_NOW + timedelta(days=20))
    published = BASE_NOW - timedelta(hours=8)
    messages = [
        _chat(f"{sample_id}-m1", course, published, "Alice", "student",
              f"{task} deadline is {first:%Y-%m-%d %H:%M}, pretty sure."),
        _chat(f"{sample_id}-m2", course, published, "Bob", "student",
              f"{task} deadline is {second:%Y-%m-%d %H:%M}, TA told me."),
    ]
    return _sample(sample_id, course, f"{course} {task} 的截止时间", messages,
                   [_ground_truth(task, course, None, status="uncertain",
                                  sources=("student_chat",), conflict=True,
                                  reason="两个同学消息互相矛盾且无更高可信度来源")])


_BUILDERS = {
    "easy": [_easy_absolute, _easy_chinese, _easy_student],
    "medium": [_medium_duplicate, _medium_missing],
    "hard": [_hard_conflict, _hard_superseded, _hard_relative, _hard_uncertain],
}


def _sample(sample_id: str, course: str, query: str, messages: list[dict],
            ground_truth: list[dict]) -> dict:
    return {
        "sample_id": sample_id,
        "course": course,
        "now": _iso(BASE_NOW),
        "query": query,
        "source_config": "跟踪课程邮件、课程群聊和同学消息",
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


def generate_samples(per_tier: int = 24) -> list[dict]:
    if not 20 <= per_tier <= 30:
        raise ValueError("每个难度档需要 20–30 条样本")
    samples: list[dict] = []
    for tier in TIERS:
        builders = _BUILDERS[tier]
        for index in range(per_tier):
            sample_id = f"{tier}-{index + 1:03d}"
            sample = builders[index % len(builders)](sample_id, index)
            sample["tier"] = tier
            sample["split"] = _split_for(index, per_tier)
            samples.append(sample)
    return samples


def write_dataset(out_dir: str | Path, per_tier: int = 24) -> dict:
    root = Path(out_dir)
    samples = generate_samples(per_tier)

    for sample in samples:
        target = root / sample["split"] / f"{sample['sample_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

    index = {
        "created_from": "ddl_agent.dataset.generator",
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
    args = parser.parse_args()
    result = write_dataset(args.out, args.per_tier)
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
