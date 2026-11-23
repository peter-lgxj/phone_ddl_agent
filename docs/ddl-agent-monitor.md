# DDL 定期检索使用说明

> 运行环境是电脑，操作对象是手机：电脑通过 ADB 驱动手机，微信 / QQ / Outlook App 的界面全部在手机上被读取。
> 手机端不安装本项目组件，不需要 root。手机读屏与目标定位**未经真实数据验证**。

## 1. 一次性配置

### 1.1 自然语言添加检索目标

```bash
python -m ddl_agent.monitor.cli add "定期读取微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次，最近7天"
python -m ddl_agent.monitor.cli list
python -m ddl_agent.monitor.cli edit wechat-21级课程群 --target "22级课程群" --lookback-days 7
```

- 支持 `微信` / `QQ` / `Outlook`；Outlook 默认读取收件箱。
- 目标名称建议用「」或引号标出；同时提到多个 App 时必须分别标出目标。
- 周期支持「每 6 小时」「每天一次」「每周」「每 30 分钟」，最短 5 分钟。
- 解析不出或过于模糊（如「所有群」「帮我盯一下」）会**拒绝并说明原因**，不会猜测。
- 每条来源都会保存原始句子（`nl_text`）与解析说明（`parse_note`），`list` 会显示“来自：……”。

### 1.2 手工编辑

配置保存在 `sources.yaml`，格式见 `sources.example.yaml`。字段：

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识，CLI 自动生成 |
| `app` | `wechat` / `qq` / `outlook` |
| `kind` | `group` / `private` / `inbox` |
| `target` | App 内要定位的群名、联系人或收件箱 |
| `source_type` | `official_email` / `student_chat` 等，用于冲突解决的可信度排序 |
| `enabled` | 是否参与本轮检索 |
| `lookback_days` | 只看最近 N 天的消息 |

QQ 单群冒烟可直接复制 `sources.qq.example.yaml` 为 `sources.qq.yaml`：它只包含「2026 计算机班级群」一个来源，`output_dir` / `state_dir` 独立，不会影响默认 `sources.yaml`。用于 D04 的最小配置与单轮运行方式见该文件顶部注释。

## 2. 运行

### 2.1 双击启动（推荐）

双击仓库根目录的 `run_ddl_monitor.bat`：

1. 检查 adb 是否可用；
2. 检查手机是否在线、已授权（USB 调试）；
3. 检查设备锁是否被其他窗口占用；
4. 询问「1 仅扫描一次 / 2 定期」；
5. 跑完刷新并打开 `output/calendar.html`。

也可以带参数运行：

```bat
run_ddl_monitor.bat --mode once
run_ddl_monitor.bat --mode loop --config sources.yaml --engine rules
```

常见问题与脚本给出的下一步：

| 现象 | 提示与下一步 |
|---|---|
| 找不到 adb | 安装 Android Platform Tools 并加入 PATH，或设置 `ADB_BINARY` |
| 没有设备 | USB 连接、解锁屏幕、打开「开发者选项 → USB 调试」 |
| `unauthorized` | 在手机弹窗点「允许」；没弹窗就撤销授权后重新插拔 |
| `offline` | 重新插拔，或 `adb kill-server` 后重试 |
| 多台设备 | 只保留一台手机/模拟器 |
| 设备锁被占用 | 关闭其他 `run.py` / `app.py` / 扫描窗口 |

### 2.2 命令行常驻

```bash
# 只跑一轮
python -m ddl_agent.monitor.cli run --once

# 按 sources.yaml 的周期常驻；每轮重新读取配置，增删来源无需重启
python -m ddl_agent.monitor.cli run
```

## 3. 增量与跨轮去重

状态目录默认 `state/`：

| 文件 | 内容 |
|---|---|
| `cursors.json` | 每个来源的 `last_timestamp`、`last_message_id`、`baseline_done` |
| `messages.jsonl` | 已抓到的原始消息，按 `message_id` 去重 |
| `candidates.jsonl` | 已抽取的候选，跨轮保留供冲突解决复用 |

- 首次运行做基线扫描（受 `lookback_days` 限制）；之后每轮只抽取新消息。
- 抓取失败的来源不推进游标，下一轮重试，并在本轮结果里记录 `error`。
- 重置游标（下一次重新做基线扫描）：

  ```bash
  python -m ddl_agent.monitor.cli reset-cursor
  python -m ddl_agent.monitor.cli reset-cursor --source wechat-21级课程群
  ```

- 已写入文档的记录按 `(课程, 归一化任务名)` 匹配：时间变了就 update，内容没变就 skip。
  每轮 `output/cycles/<cycle_id>.json` 记录 `inserted / updated / skipped`，审计日志在
  `output/deadlines.xlsx.audit.csv`。

## 4. 输出

| 文件 | 用途 |
|---|---|
| `output/deadlines.xlsx` | 主文档，含 Deadlines 与 Audit Log |
| `output/data.json` | 网页/二次开发的扁平数据 |
| `output/deadlines.ics` | 导入系统日历，confirmed 记录带提前 1 天提醒 |
| `output/calendar.html` | 自包含日历页，双击打开；每 2 分钟自动重载 |
| `output/cycles/*.json` | 每轮来源、增量、写入与 LLM 用量汇总 |

手机系统日历写入是预留接口（`write_phone_calendar`），默认关闭，需真机验证后再启用。

## 5. 验证边界

- 离线测试覆盖：自然语言配置、显式拒绝、游标基线/增量/重置、跨轮插入→跳过→更新、
  目标未找到报错且不推进游标、`data.json` / `.ics` / `calendar.html` 导出、启动器设备状态提示。
- **未经真实数据验证**：真机读屏、App 内按名称定位、Outlook App 收件箱、手机系统日历写入、
  真实微信/QQ/Outlook 消息格式。
