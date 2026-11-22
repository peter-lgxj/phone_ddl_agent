"""监控配置、增量游标与定期检索的离线测试。

全部使用本地假连接器，不访问网络、不接触真机。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ddl_agent import output
from ddl_agent.connectors.base import SourceError
from ddl_agent.models import RawMessage
from ddl_agent.monitor.cli import main as cli_main
from ddl_agent.monitor.config import (
    ConfigError,
    MonitorConfig,
    MonitorSource,
    add_from_text,
    load_config,
    parse_monitor_text,
    parse_schedule,
    remove_source,
    save_config,
    set_enabled,
    update_source,
)
from ddl_agent.monitor.cursor import CandidateStore, CursorStore, MessageStore
from ddl_agent.monitor import launcher
from ddl_agent.monitor.runner import run_cycle
from ddl_agent.monitor.sources import MobileAppSource, TargetNotFound
from ddl_agent.settings import TZ

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)


# --------------------------------------------------------------------------- #
# 自然语言配置
# --------------------------------------------------------------------------- #
def test_parse_two_apps_with_targets_and_lookback():
    text = "监控微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次，最近7天"
    sources = parse_monitor_text(text)

    assert [source.app for source in sources] == ["wechat", "qq"]
    assert [source.target for source in sources] == ["21级课程群", "机器学习课程群"]
    assert all(source.kind == "group" for source in sources)
    assert all(source.lookback_days == 7 for source in sources)
    # 每条配置都能追溯到原始自然语言
    assert all(source.nl_text == text for source in sources)
    assert all("21级课程群" in source.parse_note or "机器学习课程群" in source.parse_note
               for source in sources)


def test_add_from_text_sets_schedule_and_dedupes():
    config = MonitorConfig()
    added = add_from_text(config, "监控微信「21级课程群」，每6小时一次")
    assert len(added) == 1
    assert config.schedule == "every 6h"
    assert config.interval_minutes == 360

    with pytest.raises(ConfigError, match="已存在"):
        add_from_text(config, "监控微信「21级课程群」")


def test_outlook_defaults_to_inbox():
    sources = parse_monitor_text("监控 Outlook 收件箱，每天一次")
    assert len(sources) == 1
    assert sources[0].app == "outlook"
    assert sources[0].kind == "inbox"
    assert sources[0].target == "收件箱"


def test_qq_only_sources_template_parses():
    path = Path(__file__).resolve().parent.parent / "sources.qq.example.yaml"
    config = load_config(path)

    assert len(config.sources) == 1
    source = config.sources[0]
    assert source.app == "qq"
    assert source.kind == "group"
    assert source.target == "2026 计算机班级群"
    assert source.enabled is True
    assert config.output_dir == "output/qq-smoke"
    assert config.state_dir == "state/qq-smoke"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("帮我盯着 DD 群", "App"),
        ("监控微信所有群", "所有群"),
        ("监控微信", "名称"),
        ("监控微信和QQ的群消息", "分别用"),
    ],
)
def test_unparsable_text_is_rejected_with_reason(text, reason):
    with pytest.raises(ConfigError, match=reason):
        parse_monitor_text(text)


def test_schedule_parsing_and_rejection():
    assert parse_schedule("每天一次") == ("daily", 1440)
    assert parse_schedule("每 30 分钟") == ("every 30m", 30)
    assert parse_schedule("every 2 hours") == ("every 2h", 120)
    with pytest.raises(ConfigError, match="无法解析"):
        parse_schedule("有空就看看")
    with pytest.raises(ConfigError, match="太短"):
        parse_schedule("每 1 分钟")


# --------------------------------------------------------------------------- #
# YAML 持久化 / 增删改查
# --------------------------------------------------------------------------- #
def test_edit_source_keeps_id_and_updates_kind():
    config = MonitorConfig(sources=[
        MonitorSource(id="wechat-a", app="wechat", kind="group", target="A群",
                      source_type="student_chat"),
    ])
    source = update_source(config, "wechat-a", target="B群", app="qq", lookback_days=3)
    assert source.id == "wechat-a"  # 保留 id，游标不丢
    assert (source.app, source.target, source.kind, source.lookback_days) == ("qq", "B群", "group", 3)
    assert "手动编辑" in source.parse_note

    with pytest.raises(ConfigError, match="没有找到"):
        update_source(config, "missing", target="X")
    with pytest.raises(ConfigError, match="不支持的 App"):
        update_source(config, "wechat-a", app="feishu")


def test_yaml_roundtrip_and_mutations(tmp_path: Path):
    config = MonitorConfig(sources=[
        MonitorSource(id="wechat-a", app="wechat", kind="group", target="A群",
                      source_type="student_chat"),
        MonitorSource(id="qq-b", app="qq", kind="group", target="B群",
                      source_type="student_chat"),
    ])
    path = tmp_path / "sources.yaml"
    save_config(config, path)

    loaded = load_config(path)
    assert [source.target for source in loaded.sources] == ["A群", "B群"]

    assert set_enabled(loaded, "qq-b", False)
    assert loaded.sources[1].enabled is False
    assert remove_source(loaded, "wechat-a")
    assert [source.id for source in loaded.sources] == ["qq-b"]
    save_config(loaded, path)
    assert "wechat-a" not in path.read_text(encoding="utf-8")

    assert load_config(tmp_path / "missing.yaml").sources == []


def test_cli_add_list_and_reject(tmp_path: Path, capsys):
    config_path = tmp_path / "sources.yaml"
    assert cli_main(["--config", str(config_path), "add",
                     "监控微信「21级课程群」，每6小时一次"]) == 0
    assert cli_main(["--config", str(config_path), "list"]) == 0
    listed = capsys.readouterr().out
    assert "21级课程群" in listed
    assert "来自：监控微信" in listed

    assert cli_main(["--config", str(config_path), "add", "监控所有群"]) == 2
    assert "拒绝执行" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 增量游标
# --------------------------------------------------------------------------- #
def _message(message_id: str, body: str, published_at: datetime = NOW) -> RawMessage:
    return RawMessage(
        message_id=message_id,
        source_name="21级课程群",
        source_type="student_chat",
        sender="TA",
        published_at=published_at,
        subject="微信 / 21级课程群",
        body=body,
    )


def test_cursor_baseline_then_reset(tmp_path: Path):
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    assert store.get("wechat-a").baseline_done is False

    store.advance("wechat-a", [_message("m1", "hello")])
    reloaded = CursorStore(path).get("wechat-a")
    assert reloaded.baseline_done is True
    assert reloaded.last_message_id == "m1"
    assert reloaded.last_timestamp is not None

    CursorStore(path).reset("wechat-a")
    assert CursorStore(path).get("wechat-a").baseline_done is False


def test_message_store_dedupes_by_id(tmp_path: Path):
    store = MessageStore(tmp_path / "messages.jsonl")
    assert len(store.add([_message("m1", "a")])) == 1
    assert store.add([_message("m1", "a changed body")]) == []
    assert len(store.add([_message("m2", "b")])) == 1
    assert MessageStore(tmp_path / "messages.jsonl").seen("m2")


def test_candidate_store_persists(tmp_path: Path):
    from ddl_agent.models import DeadlineCandidate

    store = CandidateStore(tmp_path / "candidates.jsonl")
    candidate = DeadlineCandidate(
        candidate_id="m1:1", task_name_raw="HW1", course="COMP7608",
        deadline_raw="2026-11-20 23:59", deadline_iso="2026-11-20T23:59:00+08:00",
        source_type="student_chat", source_name="21级课程群",
        published_at=NOW, message_id="m1",
    )
    store.add([candidate])
    store.add([candidate])
    assert [item.candidate_id for item in CandidateStore(tmp_path / "candidates.jsonl").all()] == ["m1:1"]


# --------------------------------------------------------------------------- #
# 定期检索：基线 → 跳过 → 更新
# --------------------------------------------------------------------------- #
class FakeConnector:
    name = "fake"
    source_type = "student_chat"

    def __init__(self, messages: list[RawMessage] | Exception):
        self.messages = messages

    def fetch(self, plan=None):
        if isinstance(self.messages, Exception):
            raise self.messages
        return list(self.messages)


class FakeAgent:
    def __init__(self, observations: list[str], status: str = "done"):
        self.last_status = status
        self.last_memory = type("Memory", (), {
            "steps": [
                {"step": index, "observation": observation}
                for index, observation in enumerate(observations, start=1)
            ]
        })()
        self.run_dir = ""
        self.tasks: list[str] = []

    def run(self, task: str):
        self.tasks.append(task)


def _monitor_config(tmp_path: Path) -> MonitorConfig:
    return MonitorConfig(
        output_dir=str(tmp_path / "out"),
        state_dir=str(tmp_path / "state"),
        sources=[MonitorSource(
            id="wechat-21级课程群", app="wechat", kind="group", target="21级课程群",
            source_type="student_chat", lookback_days=14, nl_text="监控微信「21级课程群」",
        )],
    )


def _run(config: MonitorConfig, messages, now: datetime):
    return run_cycle(
        config,
        engine="rules",
        now=now,
        connector_factory=lambda source: FakeConnector(messages),
    )


def test_cycle_baseline_skip_then_update(tmp_path: Path):
    config = _monitor_config(tmp_path)

    first = _run(config, [_message("m1", "COMP7608 HW1 deadline 2026-11-20 23:59")], NOW)
    assert first.write_ok
    assert (first.inserted, first.updated, first.skipped) == (1, 0, 0)
    assert first.sources[0]["baseline"] is True
    assert first.sources[0]["new_candidates"] == 1
    assert (tmp_path / "out" / "data.json").exists()
    assert (tmp_path / "out" / "deadlines.ics").exists()

    second = _run(config, [_message("m1", "COMP7608 HW1 deadline 2026-11-20 23:59")],
                  NOW.replace(hour=12))
    assert (second.inserted, second.updated, second.skipped) == (0, 0, 1)
    assert second.sources[0]["baseline"] is False
    assert second.sources[0]["new_messages"] == 0

    third = _run(
        config,
        [
            _message("m1b", "COMP7608 HW1 deadline 2026-11-22 23:59",
                     published_at=NOW.replace(hour=18)),
            _message("m2", "COMP7608 HW2 deadline 2026-11-25 23:59",
                     published_at=NOW.replace(hour=18)),
        ],
        NOW.replace(hour=18),
    )
    assert (third.inserted, third.updated, third.skipped) == (1, 1, 0)

    rows = json.loads((tmp_path / "out" / "data.json").read_text(encoding="utf-8"))["records"]
    hw1 = next(row for row in rows if "hw1" in row["task_name"].lower())
    assert hw1["deadline"].startswith("2026-11-22")
    cycle_log = json.loads(
        (tmp_path / "out" / "cycles" / f"{third.cycle_id}.json").read_text(encoding="utf-8")
    )
    assert cycle_log["inserted"] == 1 and cycle_log["updated"] == 1


def test_cycle_reports_target_not_found_and_keeps_cursor(tmp_path: Path):
    config = _monitor_config(tmp_path)
    source = config.sources[0]
    agent = FakeAgent([], status="not_found")

    def factory(_source):
        return MobileAppSource(source, agent=agent, use_device_lock=False, now=NOW)

    result = run_cycle(config, engine="rules", now=NOW, connector_factory=factory)
    assert result.errors and "未找到" in result.errors[0]
    assert result.sources[0]["error"] and "未找到" in result.sources[0]["error"]
    assert CursorStore(tmp_path / "state" / "cursors.json").get(source.id).baseline_done is False
    assert result.inserted == 0


def test_cycle_continues_when_one_source_fails(tmp_path: Path):
    config = _monitor_config(tmp_path)
    config.sources.append(MonitorSource(
        id="qq-机器学习课程群", app="qq", kind="group", target="机器学习课程群",
        source_type="student_chat", lookback_days=14,
    ))

    def factory(source):
        if source.id.startswith("qq"):
            return FakeConnector(SourceError("设备锁被占用，本轮未读取手机"))
        return FakeConnector([_message("m1", "COMP7608 HW1 deadline 2026-11-20 23:59")])

    result = run_cycle(config, engine="rules", now=NOW, connector_factory=factory)
    assert result.inserted == 1
    assert result.status == "partial"
    assert result.sources_ok == 1
    assert result.outputs
    assert any("设备锁被占用" in error for error in result.errors)


def test_cycle_all_sources_failed_writes_no_success_artifacts(tmp_path: Path):
    config = _monitor_config(tmp_path)

    def factory(_source):
        raise RuntimeError("device offline")

    result = run_cycle(config, engine="rules", now=NOW, connector_factory=factory)

    assert result.status == "failed"
    assert result.sources_ok == 0
    assert result.write_ok is False
    assert result.outputs == []
    assert not (tmp_path / "out" / "data.json").exists()
    assert not (tmp_path / "out" / "deadlines.ics").exists()
    assert not (tmp_path / "out" / "calendar.html").exists()
    cycle_log = json.loads(
        (tmp_path / "out" / "cycles" / f"{result.cycle_id}.json").read_text(encoding="utf-8")
    )
    assert cycle_log["status"] == "failed"
    assert cycle_log["sources_ok"] == 0
    assert "device offline" in " ".join(cycle_log["errors"])


def test_mobile_source_builds_search_task_and_reads_steps():
    source = MonitorSource(id="wechat-a", app="wechat", kind="group", target="21级课程群",
                           source_type="student_chat", lookback_days=7)
    agent = FakeAgent(["COMP7608 HW1 deadline 2026-11-20 23:59"])
    mobile = MobileAppSource(source, agent=agent, use_device_lock=False, now=NOW)

    messages = mobile.fetch()
    assert len(messages) == 1
    assert "搜索框输入「21级课程群」" in agent.tasks[0]
    assert "最近 7 天" in agent.tasks[0]
    assert messages[0].source_name == "21级课程群"


def test_mobile_source_forces_read_only_on_production_agent():
    source = MonitorSource(id="qq-a", app="qq", kind="group", target="2026 计算机班级群",
                           source_type="student_chat")
    agent = FakeAgent(["COMP7608 HW1 deadline 2026-11-20 23:59"])
    mobile = MobileAppSource(source, agent=agent, use_device_lock=False, now=NOW)

    mobile.fetch()

    assert agent.read_only is True


def test_mobile_source_raises_when_target_missing():
    source = MonitorSource(id="wechat-a", app="wechat", kind="group", target="不存在的群",
                           source_type="student_chat")
    mobile = MobileAppSource(source, agent=FakeAgent(["未找到该群"]), use_device_lock=False)
    with pytest.raises(TargetNotFound, match="未找到目标"):
        mobile.fetch()


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def test_data_json_and_ics_content(tmp_path: Path):
    from ddl_agent.models import ResolvedRecord

    records = [
        ResolvedRecord(task_name="HW1", course="COMP7608",
                       deadline_iso="2026-11-20T23:59:00+08:00", status="confirmed",
                       source="微信 / 21级课程群", confidence=0.95),
        ResolvedRecord(task_name="HW2", course="COMP7608", deadline_iso=None,
                       status="uncertain", source="QQ", confidence=0.4),
    ]
    paths = output.export_outputs(records, tmp_path / "out", NOW)
    assert {path.name for path in paths} == {"data.json", "deadlines.ics", "calendar.html"}

    payload = json.loads((tmp_path / "out" / "data.json").read_text(encoding="utf-8"))
    assert payload["counts"] == {"total": 2, "confirmed": 1, "uncertain": 1, "missing_deadline": 0}

    page = (tmp_path / "out" / "calendar.html").read_text(encoding="utf-8")
    assert "DDL 日历" in page and '"task_name": "HW1"' in page

    ics = (tmp_path / "out" / "deadlines.ics").read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 1  # 只有 confirmed 记录进日历
    assert "VALARM" in ics and "TRIGGER:-P1D" in ics
    # 本地时间 + TZID，跨时区客户端导入后仍是北京时间
    assert "TZID:Asia/Shanghai" in ics
    assert "DTSTART;TZID=Asia/Shanghai:20261120T235900" in ics


def test_phone_calendar_defaults_to_off_and_requires_agent():
    writer = output.PhoneCalendarWriter()
    assert writer.write([])["skipped"] is True

    writer = output.PhoneCalendarWriter(enabled=True)
    with pytest.raises(output.SourceNotConfigured):
        writer.write([])

    agent = FakeAgent([])
    result = output.PhoneCalendarWriter(enabled=True, agent_factory=lambda: agent).write([])
    assert result["ok"] is True
    assert agent.tasks


# --------------------------------------------------------------------------- #
# 双击启动器：设备检查与人话报错
# --------------------------------------------------------------------------- #
def test_adb_device_states_produce_human_messages():
    devices = launcher.parse_devices("List of devices attached\nABC123\tdevice\n")
    assert devices == [("ABC123", "device")]
    assert launcher.device_problem(devices) is None

    assert "没有检测到手机" in launcher.device_problem([])
    assert "未授权" in launcher.device_problem([("ABC", "unauthorized")])
    assert "offline" in launcher.device_problem([("ABC", "offline")])
    assert "多台设备" in launcher.device_problem([("A", "device"), ("B", "device")])


def test_launcher_reports_missing_adb_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "resolve_adb", lambda: None)
    assert launcher.main([]) == 2
    out = capsys.readouterr().out
    assert "找不到 adb" in out and "ADB_BINARY" in out
    assert "Traceback" not in out
