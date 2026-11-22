"""建 PR 脚本的 UTF-8 编码与回读比对。"""

from __future__ import annotations

import json

from scripts.create_pr import build_payload, compare_fields, normalize_body

TITLE = "fix: 解析读屏 UI 相对时间并合并超时重复 DDL"
BODY = "## 背景\n\n中文正文：昨天 06:33、周五 23:45。\n"


def test_build_payload_keeps_chinese_as_utf8_bytes():
    payload = build_payload({"title": TITLE, "body": BODY})

    assert TITLE.encode("utf-8") in payload
    assert b"\\u" not in payload
    decoded = json.loads(payload.decode("utf-8"))
    assert decoded["title"] == TITLE
    assert decoded["body"] == BODY


def test_compare_fields_accepts_exact_chinese():
    assert compare_fields(TITLE, BODY, {"title": TITLE, "body": BODY}) == []


def test_compare_fields_rejects_mojibake_title():
    problems = compare_fields(
        TITLE, BODY, {"title": "fix: ???? UI ??????????? DDL", "body": BODY}
    )

    assert problems
    assert "标题不一致" in problems[0]


def test_normalize_body_treats_crlf_as_lf():
    assert normalize_body("a\r\nb\r\n\r\n") == "a\nb"
