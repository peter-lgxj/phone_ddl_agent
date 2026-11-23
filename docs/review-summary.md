# PR #1 评审摘要：`fix/p0-run-blockers` → `main`

面向：peter-lgxj、calmmmiu
范围：PR #1（squash 合并后 main 上自 `967c58a` 起的 1 个提交），覆盖 P0 阻塞、P1 正确性、P2 实验支撑与工程化。

---

## 1. 这次改了什么（按优先级归纳）

### P0：不修就跑不起来 / 会重复发消息

| 问题 | 处理 |
|---|---|
| `phone_controller.py` 同名方法重复定义（`type_text`、`tap_input_box` 各两份），后定义覆盖前定义，导致“输入即自动发送”且再按一次回车 | 合并为单一实现，输入与发送分离，`send` 不再重复回车 |
| 缺 `models/input_box.pt` 时输入链路直接崩溃 | `InputDetector` 初始化失败降级到 uiautomator2 控件树（`EditText`），只打印提示 |
| 启动无前置校验，报错指向底层异常 | 新增 API key / adb / 设备三类检查与可执行提示 |
| Streamlit 状态永不更新（`ui_state` 无调用），页面永远 running | 主循环写入 running/finished/error，完成、超步数、异常都有明确状态 |
| 多应用模式重复构造执行器（连两次 u2、加载两次 OCR） | 复用同一执行器 |

### P1：正确性与可复现性

- **跨 App 信息传递落地**：`context_needed` 之前只写在 prompt 里、解析时被丢弃；现在持久化到 `SubTask`，重规划更新，执行前拼入子任务目标（`build_exec_goal`）
- **失败不再被当成完成**：单步重试只包“决策”，动作只执行一次（微信不会重复发消息）；观察失败作废旧截图，且不再用旧图判定完成
- **多应用整体判定**：子任务失败/超时/依赖卡死的 pending 都会传播为整体失败，`results.jsonl` 记 `success=false`；`depends_on` 现在会过滤非法索引、断开环形依赖并打印修正日志
- **采样固定**：决策/观察/完成检测统一 `temperature=0`，重试次数与退避可配
- **去硬编码**：微信 resource-id 可配置；删除写死坐标；滑动按分辨率计算
- **留证**：每步 `step_NNN.json`（含完整 action 与坐标）+ 前后截图，`task_log.json` 保存完整动作
- **设备互斥**：`runs/.device.lock` 覆盖 reset + 整个任务，token/pid 归属校验；Web、CLI、实验 runner 共用，抢不到锁直接失败而不是互相 force-stop
- **重置如实上报**：只有 adb 返回 0 且已安装才计 stopped；离线设备与未安装应用分开记录

### P2：实验支撑与工程化

- `experiments/tasks.json`：20 个任务（L1/L2/L3，含理论步数），供 TSR/SE/AT/ST 计算
- `experiments/run_experiment.py`：重复执行、单/多应用与 baseline 模式、每任务重置、manifest（git SHA/dirty、模型全名、温度、设备信息）
- `experiments/metrics.py`：TSR、多 App TSR、SE、AT、ST、CS；CS 在判定粒度混合时拒绝计算并给出覆盖率
- `experiments/baselines/`：stdin/stdout JSON 协议 + 通用子进程适配器 + 三个 wrapper 骨架（**未安装、未联调**）
- `tests/`：540 passed、2 skipped（跳过的是需要本地 embedding 模型的可选用例）；其余用例不依赖真机/模型；CI 执行 compileall、tasks 校验、dry-run、ruff、pytest
- 依赖：`requirements.txt`（版本下限）+ `requirements-lock.txt`（本机 3.12 解析）+ `requirements-dev.txt`
- 文档：README 补运行前置、配置表、项目结构、实验用法、权重来源、隐私提示；`test.md` 的 30 步口径与 T19 残留文本已清理

---

## 2. 为什么改（对应真实缺陷）

主要来自 `现状审查清单` 与五轮复审，都是可复现缺陷，不是风格偏好：

1. 重复方法定义是**功能性 bug**：`type_text` 内置回车 + `send` 再回车，微信会连发两条
2. 权重缺失导致 `InputDetector` 构造抛异常，任务根本进不到输入环节
3. `ui_state` 不更新使 Web 端无法判断任务是否结束
4. 重试包住动作、finish 直接 return、pending 不计入失败，会让**失败被记录成成功**，直接污染实验结论
5. `judgement` 粒度混用会把 CS 抬成 TSR，属于**指标失真**
6. reset 只包锁/不包任务，会让两个进程同时操作手机；这些都在复审里实测过

---

## 3. 验证到什么程度

### 已实跑（本机）

- Python 3.12.14 venv：`pytest` **540 passed、2 skipped**、`ruff` 全绿、`run_experiment.py --dry-run` 通过
- 真机：OPPO PLJ110 / Android 17 / 1080×2374（因 Android 17 与 uiautomator2 3.7.1 不兼容，真机走 `SCREEN_DRIVER=adb_dump`），微信「文件传输助手」自聊会话，单机、单账号、只读；包括滚动 OCR 观察、timeout 部分结果、端到端抽取，详见 `docs/real-device-test-report.md`
- GitHub Actions：以本 PR 最新一次 run 为准

### 静态审查/有限真机

- 重试边界、观察有效性、context 注入、依赖修正、锁互斥、reset 结果——都有单元测试覆盖逻辑；真机只验证了只读读取与抽取，**未验证实际点击/输入/发送效果**
- 动作执行、OCR/YOLO 定位、键盘弹出检测只做了代码级检查
- 真实课程群、QQ / Outlook App、图片型 DDL 未验证；读屏视觉模型的 token/成本未统计

### 必须真机/真实环境才能确认

- 跨分辨率点击与滑动、微信输入框实际定位、发送行为
- YOLO 权重实际检测效果与阈值（0.3/0.5）取舍
- 模型 API 的真实稳定性、重试是否足够、temperature=0 的实际表现与成本
- 20 任务 ×（3/3/2）真实实验数据与人工判分，才能得到 TSR/CS/SE/AT/ST 数值
- 三个 baseline 的安装与联调

---

## 4. 仍然缺什么

| 项 | 状态 |
|---|---|
| 真机交互验证 | 部分执行：单机自聊会话只读读取与一次端到端抽取；真实群聊/QQ/Outlook 未执行 |
| 真实 API 调用 | 已执行：DeepSeek 读屏与抽取；稳定性与成本统计未做 |
| 真实实验数据 | 只有任务集与 runner，无 results/人工判分 |
| baseline 实装 | 只有协议与 wrapper 骨架，三个系统均未安装 |
| 权重 | 仓库不含二进制；需从 `peter-lgxj/gui_yolo_model` 取 `best.pt` 放到 `models/input_box.pt`（README 有直链与 sha 校验） |
| LICENSE | `gui_yolo_model` 未附 LICENSE，权重能否随项目分发需组长确认 |
| 分支保护 | 需 admin 配置 |

---

## 5. 建议的合并方式与合并后注意事项

**建议**：用 **Squash merge** 合并本 PR，保留 PR 正文与本文档作为变更记录；不建议直接 push main。

**合并后组员操作**：

```bash
git checkout main
git pull --ff-only
git checkout -b <自己的分支>
```

注意：

1. `models/input_box.pt` 不在仓库内，拉代码后权重仍需单独下载（README「输入框检测权重」一节）
2. 依赖装 `requirements.txt`；需要完全复现实验环境时用 `requirements-lock.txt`（Windows + Python 3.12 解析，换平台请重新生成）
3. 运行前确认 `adb devices` 有设备、`.env` 配好对应的 `*_API_KEY`；无 key 会在启动校验阶段直接报错
4. 同一台手机不要同时跑 Web 界面和实验 runner：设备锁会阻止并发，抢不到锁的任务会直接失败
5. 实验前先读 `experiments/README`（若有）与 `test.md`：CS 需要**逐次判定**（`{"T01": [true,false,true]}`），任务级布尔会导致 CS 拒绝计算
6. `runs/` 会持续增长（每步截图 + JSON），跑批前记得清理或归档

---

## 6. 2026-11-22 复验补充：读数标签与判分口径

这一节只登记**带来源标签**的结论，不改变上文的历史评审记录。

### 6.1 标签定义

| 标签 | 含义 |
| --- | --- |
| 真机实测 | 设备截图、`step_*.json`、`usage_summary.json` 直接产生 |
| 离线回放 | 冻结真机抽取候选或归档控件树，用当前代码重算；不联网、不碰手机；不能写成实测 |
| 未执行 | 设备断开或缺少语料，当前没有可引用的数字 |

### 6.2 真机实测（2026-11-22，`adb_dump`，同一任务同群）

| 指标 | `MERGED_OBSERVE=0` | `MERGED_OBSERVE=1` |
| --- | --- | --- |
| 任务总耗时 | 21.03 s | 11.45 s |
| 请求数 | 5 | 1 |
| dump 次数 | 2 | 3 |
| 动作序列 | `wait→tap→finish` | `open_group→finish` |

两组序列不同（模型导航 vs 规则导航），不能当作纯合并观察的提速。把规则关掉后 0/1 各 3 次、序列一致：21.27 s vs 17.81 s（前两次；第三次超时 56.3 s），合并观察约快 16%。规则开关单独对照：强制规则 11.44 s / 1 请求 vs 关闭 17.81 s / 3 请求，规则步 5.5 s ≈ 模型步 5.3 s，但少一次请求，故 adb_dump 下默认开启。

基线 235deea 的 20.63 s vs 71.07 s 是 6 次固定滚动造成的策略差异，已由「滚动上限 2 + 命中答案即停 + 轻观察」修复；当时不能归因于合并观察。

### 6.3 离线回放

- 判分复跑：20/21 = **95.24%**（hit 20、partial 1、fail 0、violation 0）；D02/D03 数据未采集，标「不可评」并从分母剔除。
- D09 口径写死：「月底前」不猜日期，`status=uncertain` 且 `deadline_iso=null` 即算 hit。
- D15：离线固定规划器选源（COMP7503课程群）后回放下游，判 hit；**选源能力本身仅真机可评**。
- D18：明确任务无匹配时输出空结果并标 `no_match`，不再输出无关的 `llm` 记录。
- 规则停止条件：归档聊天页已有答案关键词时 0 次滚动；群列表开群与发送区域拒绝均按预期。

### 6.4 真机其余冒烟

- 搜索 vs 列表：同群各 3 次，搜索 39.01 s / 7 dumps / 4 步，列表 11.48 s / 3 dumps / 2 步；adb_dump 下搜索页 dump 更慢，搜索不作为默认（`RULE_SEARCH_FORCE=1` 可强制）。
- monitor 单进程 `run_cycle`：7 步 / 51.2 s / 19,591 tokens，输出 `data.json`、`deadlines.ics`、`calendar.html`；手机日历测试事件写入后删除，.ics 生成 VEVENT + VALARM。
- 只读保护：搜索输入放开后，send / press_enter / 消息输入框 type 仍拒绝；规则 tap 发送区域断言保留。

### 6.5 未执行

- 修复后的真机判分复跑（当前判分数字仍来自离线回放）。

同屏复用与连接预热不作为收益项：6 次归档 A/B 中 `reused_frame` 均为 0；复用最多省 `preprocess` 0.11–0.15 s/步；连接预热 0.22–0.43 s 仅启动一次，每步收益 0.00 s。

