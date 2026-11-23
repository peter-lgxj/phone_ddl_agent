"""自然语言检索配置：解析 → 结构化 → 持久化到 ``sources.yaml``。

解析不出来时显式拒绝并说明原因，不猜测；每条来源保留原始自然语言用于溯源。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

APP_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("wechat", ("微信", "wechat", "weixin")),
    ("qq", ("qq", "扣扣")),
    ("outlook", ("outlook", "邮箱", "邮件")),
]
APP_LABELS = {"wechat": "微信", "qq": "QQ", "outlook": "Outlook"}

_QUOTE_RE = re.compile(r"[「“\"『《']([^」”\"'』》]{1,40})[」”\"'』》]")
_LOOKBACK_RE = re.compile(r"(?:最近|过去|近)\s*(\d+)\s*天")
_LOOKBACK_WORD = {"一周": 7, "两周": 14, "一个月": 30, "半年": 180}
_SCHEDULE_HOUR_RE = re.compile(r"每\s*(?:隔)?\s*(\d+)\s*(?:个)?\s*(?:小时|h|hours?|hour)")
_SCHEDULE_MIN_RE = re.compile(r"每\s*(?:隔)?\s*(\d+)\s*分钟")
_SCHEDULE_EN_RE = re.compile(r"every\s*(\d+)\s*(?:h|hours?|hour)")

_LEADING_NOISE_RE = re.compile(
    r"^(?:帮我|请|麻烦|我想要?|我要|想|然后|去|在)?\s*(?:监控|关注|盯住|盯着|跟踪|监测|读取)?\s*",
)
_TRAILING_NOISE_RE = re.compile(
    r"\s*(?:的)?\s*(?:群聊|群消息|聊天记录|消息|私聊|收件箱|邮箱|邮件)?\s*(?:里|中|内)?\s*"
    r"(?:的消息|的聊天记录|的邮件)?\s*$",
)


class ConfigError(ValueError):
    """自然语言描述无法可靠解析。"""


@dataclass
class MonitorSource:
    id: str
    app: str
    kind: str
    target: str
    source_type: str
    enabled: bool = True
    lookback_days: int = 14
    nl_text: str = ""
    parse_note: str = ""


@dataclass
class MonitorConfig:
    schedule: str = "every 6h"
    interval_minutes: int = 360
    output_dir: str = "output"
    state_dir: str = "state"
    write_phone_calendar: bool = False
    sources: list[MonitorSource] = field(default_factory=list)

    def to_yaml(self) -> str:
        payload = asdict(self)
        payload["version"] = 1
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict) -> "MonitorConfig":
        sources = [
            MonitorSource(**{key: item[key] for key in item if key in MonitorSource.__annotations__})
            for item in (data.get("sources") or [])
        ]
        return cls(
            schedule=str(data.get("schedule") or "every 6h"),
            interval_minutes=int(data.get("interval_minutes") or 360),
            output_dir=str(data.get("output_dir") or "output"),
            state_dir=str(data.get("state_dir") or "state"),
            write_phone_calendar=bool(data.get("write_phone_calendar", False)),
            sources=sources,
        )


def load_config(path: str | Path) -> MonitorConfig:
    file = Path(path)
    if not file.exists():
        return MonitorConfig()
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件无法解析（YAML 语法错误）：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式不正确（期望映射）：{file}")
    try:
        return MonitorConfig.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置文件字段不合法：{exc}") from exc


def save_config(config: MonitorConfig, path: str | Path) -> Path:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(f"{file.name}.tmp")
    tmp.write_text(config.to_yaml(), encoding="utf-8")
    os.replace(tmp, file)
    return file


def parse_schedule(text: str) -> tuple[str, int]:
    lowered = text.lower()
    if "每天" in text or "每日" in text or "daily" in lowered:
        return "daily", 24 * 60
    if "每周" in text or "weekly" in lowered:
        return "weekly", 7 * 24 * 60
    for pattern in (_SCHEDULE_HOUR_RE, _SCHEDULE_EN_RE):
        match = pattern.search(lowered)
        if match:
            hours = int(match.group(1))
            _validate_interval(hours * 60)
            return f"every {hours}h", hours * 60
    match = _SCHEDULE_MIN_RE.search(text)
    if match:
        minutes = int(match.group(1))
        _validate_interval(minutes)
        return f"every {minutes}m", minutes
    raise ConfigError("无法解析调度周期，请写成「每 6 小时」「每天一次」「每 30 分钟」这样的说法")


def _validate_interval(minutes: int) -> None:
    if minutes < 5:
        raise ConfigError("调度周期太短（至少 5 分钟），已拒绝")


def parse_monitor_text(text: str, config: MonitorConfig | None = None) -> list[MonitorSource]:
    """把一句自然语言解析成一个或多个检索目标。"""
    if not text or not text.strip():
        raise ConfigError("描述为空")

    lowered = text.lower()
    apps = [app for app, hints in APP_HINTS if any(hint in lowered for hint in hints)]
    if not apps:
        raise ConfigError("没有识别到要检索的 App，请明确写「微信」「QQ」或「Outlook」")
    if "所有群" in text or "全部群" in text or "all groups" in lowered:
        raise ConfigError("不支持「所有群」这类模糊描述，请给出具体的群名称")

    lookback = _parse_lookback(text)
    quoted = [item.strip() for item in _QUOTE_RE.findall(text)]

    if len(apps) > 1 and not quoted:
        raise ConfigError("同时提到多个 App 时，请分别用「」标出每个 App 要检索的目标名称")

    targets: dict[str, str] = {}
    if quoted:
        if len(quoted) == 1:
            targets = {app: quoted[0] for app in apps}
        elif len(quoted) == len(apps):
            targets = dict(zip(apps, quoted))
        else:
            raise ConfigError("标出的目标数量与 App 数量不一致，无法确定对应关系")
    elif len(apps) == 1:
        target = _extract_target(text, apps[0])
        if not target and apps[0] != "outlook":
            raise ConfigError("没有给出要检索的群/联系人名称")
        targets[apps[0]] = target or "收件箱"

    existing_ids = {source.id for source in (config.sources if config else [])}
    sources: list[MonitorSource] = []
    for app in apps:
        target = targets[app].strip()
        if not target:
            if app == "outlook":
                target = "收件箱"
            else:
                raise ConfigError(f"{APP_LABELS[app]} 没有给出目标名称")
        kind = "inbox" if app == "outlook" else ("group" if "群" in target else "private")
        source = MonitorSource(
            id=_unique_id(app, target, existing_ids),
            app=app,
            kind=kind,
            target=target,
            source_type="official_email" if app == "outlook" else "student_chat",
            lookback_days=lookback,
            nl_text=text.strip(),
            parse_note=(
                f"app={app}; kind={kind}; target={target}; "
                f"lookback={lookback}d; 解析自：{text.strip()[:60]}"
            ),
        )
        existing_ids.add(source.id)
        sources.append(source)
    return sources


def add_from_text(config: MonitorConfig, text: str,
                  schedule_text: str | None = None) -> list[MonitorSource]:
    sources = parse_monitor_text(text, config)
    existing = {(source.app, source.target) for source in config.sources}
    added: list[MonitorSource] = []
    for source in sources:
        if (source.app, source.target) in existing:
            raise ConfigError(f"已存在同样的检索目标：{APP_LABELS[source.app]} / {source.target}")
        config.sources.append(source)
        added.append(source)
    if schedule_text:
        normalized, minutes = parse_schedule(schedule_text)
        config.schedule = normalized
        config.interval_minutes = minutes
    elif "每" in text or "每天" in text or "每周" in text or "daily" in text.lower():
        normalized, minutes = parse_schedule(text)
        config.schedule = normalized
        config.interval_minutes = minutes
    return added


def remove_source(config: MonitorConfig, source_id: str) -> bool:
    before = len(config.sources)
    config.sources = [source for source in config.sources if source.id != source_id]
    return len(config.sources) != before


def update_source(config: MonitorConfig, source_id: str, *, target: str | None = None,
                  app: str | None = None, lookback_days: int | None = None) -> MonitorSource:
    """修改已有来源；保留 id，避免增量游标丢失。"""
    source = next((item for item in config.sources if item.id == source_id), None)
    if source is None:
        raise ConfigError(f"没有找到来源：{source_id}")
    if app is not None:
        if app not in APP_LABELS:
            raise ConfigError(f"不支持的 App：{app}（只支持 wechat / qq / outlook）")
        source.app = app
    if target is not None:
        stripped = target.strip()
        if not stripped:
            raise ConfigError("目标名称不能为空")
        source.target = stripped
    if lookback_days is not None:
        source.lookback_days = max(1, int(lookback_days))

    source.kind = "inbox" if source.app == "outlook" else (
        "group" if "群" in source.target else "private"
    )
    source.source_type = "official_email" if source.app == "outlook" else "student_chat"
    changes = [f"{key}={value}" for key, value in (
        ("app", app), ("target", target), ("lookback", lookback_days)
    ) if value is not None]
    note = "手动编辑 " + ", ".join(changes)
    source.parse_note = f"{source.parse_note}; {note}" if source.parse_note else note
    return source


def set_enabled(config: MonitorConfig, source_id: str, enabled: bool) -> bool:
    for source in config.sources:
        if source.id == source_id:
            source.enabled = enabled
            return True
    return False


def _parse_lookback(text: str) -> int:
    match = _LOOKBACK_RE.search(text)
    if match:
        return max(1, int(match.group(1)))
    for phrase, days in _LOOKBACK_WORD.items():
        if f"最近{phrase}" in text or f"过去{phrase}" in text:
            return days
    return 14


def _extract_target(text: str, app: str) -> str:
    label = APP_LABELS[app]
    index = text.lower().find(label.lower())
    if index < 0:
        for hint in dict(APP_HINTS)[app]:
            index = text.lower().find(hint)
            if index >= 0:
                break
    if index < 0:
        return ""
    remainder = text[index + len(label):]
    # 截掉后面另一个 App 的描述
    for other_app, hints in APP_HINTS:
        if other_app == app:
            continue
        for hint in hints:
            cut = remainder.lower().find(hint)
            if cut >= 0:
                remainder = remainder[:cut]
    remainder = re.split(r"[，。；;,.!?\n]", remainder)[0]
    remainder = _LEADING_NOISE_RE.sub("", remainder).strip()
    remainder = re.sub(r"^(?:的|里|中|内)\s*", "", remainder)
    remainder = _TRAILING_NOISE_RE.sub("", remainder).strip()
    return remainder.strip(" 「」“”\"'：:")


def _unique_id(app: str, target: str, existing: set[str]) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", target).strip("-")[:20] or "target"
    base = f"{app}-{slug}"
    candidate, suffix = base, 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
