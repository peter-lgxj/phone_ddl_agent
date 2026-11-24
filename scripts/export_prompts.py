"""把代码里的提示词导出为可提交的文本，避免评审只能翻源码。

用法：
  python scripts/export_prompts.py

生成文件：prompts/ddl-agent-prompts.md
修改提示词后需要重新运行本脚本；tests/test_prompts_export.py 会检查两者一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ddl_agent.evaluation import baseline  # noqa: E402
from ddl_agent.extract import llm_extractor  # noqa: E402
from ddl_agent.llm import arbiter, planner  # noqa: E402

OUTPUT = ROOT / "prompts" / "ddl-agent-prompts.md"

# (标题, 源码位置, 用途, system, prompt)
SPECS = [
    (
        "规划器 planner",
        "ddl_agent/llm/planner.py",
        "把自然语言请求解析成来源选择与时间窗；只发文字，不接收截图。",
        planner._SYSTEM,
        planner._PROMPT,
    ),
    (
        "截止时间抽取器 llm_extractor",
        "ddl_agent/extract/llm_extractor.py",
        "从单条消息中抽取任务名、课程、截止时间与置信度。",
        llm_extractor._SYSTEM,
        llm_extractor._PROMPT,
    ),
    (
        "同源冲突裁决器 arbiter",
        "ddl_agent/llm/arbiter.py",
        "同一任务出现多个截止时间时，判断哪一条是最终值以及原因。",
        arbiter._SYSTEM,
        arbiter._PROMPT,
    ),
    (
        "单次直出 baseline",
        "ddl_agent/evaluation/baseline.py",
        "评测对照用的单次直出基线，不参与默认运行路径。",
        baseline._SYSTEM,
        baseline._PROMPT,
    ),
]

SCREEN_NOTE = """## 屏幕执行层（Phone GUI）

屏幕执行层的提示词在运行时按当前屏幕、历史动作和 OCR/控件树结果拼装，不能静态导出为完整文本。
位置：

- `screen_agent.py`：`ScreenAgent._build_prompt`（约 L627）负责系统提示与当步上下文；
- `screen_agent.py`：`ScreenAgent._observe`（约 L1343）负责动作后的观察描述；
- `screen_agent.py`：`ScreenAgent._decide`（约 L2409）负责决策重试与 `TargetNotLocated` 的安全停止。

只读约束、步数上限、时间窗和“两次未定位即失败”等规则由代码与配置共同执行，
详见 `docs/ddl-agent-design.md` 与 `docs/ddl-agent-monitor.md`。
"""


def render_prompts() -> str:
    """返回 prompts/ddl-agent-prompts.md 的完整内容。"""
    lines = [
        "# DDL Agent 提示词（由脚本生成）",
        "",
        "本文件由 `python scripts/export_prompts.py` 从代码常量生成，不要手工修改。",
        "改完提示词后重新生成；`tests/test_prompts_export.py` 会检查代码与本文件是否一致。",
        "",
    ]
    for title, path, purpose, system, prompt in SPECS:
        lines.extend(
            [
                f"## {title}",
                "",
                f"来源：`{path}`",
                "",
                f"用途：{purpose}",
                "",
                "### System",
                "",
                "```text",
                system.strip(),
                "```",
                "",
                "### User prompt 模板",
                "",
                "```text",
                prompt.strip(),
                "```",
                "",
            ]
        )
    lines.append(SCREEN_NOTE)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_prompts(), encoding="utf-8", newline="\n")
    print(f"已写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
