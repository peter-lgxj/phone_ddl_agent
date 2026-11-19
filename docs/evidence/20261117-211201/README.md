# 脱敏运行证据

本目录由 `experiments/export_evidence.py` 生成，源 run：`20261117-211201`。

保留：
- `summary.json`：步数、各阶段耗时、P50/P90
- `steps.jsonl`：每步动作类型、`timings`、`usage`、观察长度与 SHA256、图片尺寸
- `README.md`：本说明

剔除（不公开）：
- `step_*_current.png` / `step_*_observe.png` / `step_*_sent.jpg` 全屏截图
- 原始 `task`、`action` 文本、`observation` 文本、`target` 与 OCR 识别文字
- 任何应用账号、群名、课程名或聊天内容

用途：只支持耗时/token 复核与失败步骤定位，不包含可还原的聊天内容。
