"""状态文件与配置文件的健壮性：损坏数据不拖垮整轮检索。"""

from __future__ import annotations

import pytest

from ddl_agent.monitor.config import ConfigError, load_config, save_config, MonitorConfig
from ddl_agent.monitor.cursor import CandidateStore, CursorStore, MessageStore


def test_cursor_store_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text("{not json", encoding="utf-8")

    store = CursorStore(path)

    assert store.get("any").last_timestamp is None


def test_message_store_skips_corrupt_lines(tmp_path):
    path = tmp_path / "messages.jsonl"
    path.write_text('{"message_id": "ok"\nnot json\n{"message_id": "partial"}\n',
                    encoding="utf-8")

    store = MessageStore(path)

    assert len(store) == 0


def test_candidate_store_skips_corrupt_lines(tmp_path):
    path = tmp_path / "candidates.jsonl"
    path.write_text("not json\n{}\n", encoding="utf-8")

    store = CandidateStore(path)

    assert store.all() == []


def test_cursor_save_is_atomic_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "cursors.json"
    CursorStore(path).save()

    assert path.exists()
    assert not (tmp_path / "cursors.json.tmp").exists()


def test_config_rejects_broken_yaml_with_actionable_error(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("sources: [unclosed\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_config_save_is_atomic(tmp_path):
    path = tmp_path / "sources.yaml"
    save_config(MonitorConfig(), path)

    assert path.exists()
    assert not (tmp_path / "sources.yaml.tmp").exists()
