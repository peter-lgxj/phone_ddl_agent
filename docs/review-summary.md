# PR #1 评审摘要：`fix/p0-run-blockers` → `main`

面向：peter-lgxj、calmmmiu
范围：52 个 commit（从 main `967c58a` 起），覆盖 P0 阻塞、P1 正确性、P2 实验支撑与工程化。

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
- `tests/`：59 个用例，全部不依赖真机/模型；CI 执行 compileall、tasks 校验、dry-run、ruff、pytest
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

- Python 3.12.14 venv：`pytest` **59 passed**、`ruff` 全绿、`run_experiment.py --dry-run` 通过
- 模拟 CI（屏蔽 uiautomator2/easyocr/ultralytics/torch/cv2）：**59 passed，0 skipped**
- 全模块导入冒烟（torch/easyocr 环境）
- GitHub Actions：最新提交 #17/#18 **success**，日志实测 `All checks passed!` + `59 passed`

### 静态审查/推断（未连真机）

- 重试边界、观察有效性、context 注入、依赖修正、锁互斥、reset 结果——都有单元测试覆盖逻辑，但**未在真机上验证实际点击/输入效果**
- 动作执行、OCR/YOLO 定位、键盘弹出检测只做了代码级检查

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
| 真机交互验证 | 缺设备，未执行 |
| 真实 API 调用 | 缺 key，未执行 |
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
