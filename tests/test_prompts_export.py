"""提示词导出文件必须与代码常量一致，避免提交材料与实现漂移。"""

from __future__ import annotations

from scripts.export_prompts import OUTPUT, render_prompts


def test_prompts_file_matches_code():
    assert OUTPUT.exists(), "缺少 prompts/ddl-agent-prompts.md，先运行 scripts/export_prompts.py"
    assert OUTPUT.read_text(encoding="utf-8") == render_prompts()


def test_prompts_file_covers_all_exported_modules():
    text = OUTPUT.read_text(encoding="utf-8")
    for marker in ("planner", "llm_extractor", "arbiter", "baseline", "屏幕执行层"):
        assert marker in text
