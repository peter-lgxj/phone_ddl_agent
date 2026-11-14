# 手机智能体（Phone Agent）

使用视觉语言模型理解 Android 手机屏幕，通过 ADB 和 uiautomator2 执行操作；支持任务规划、多应用任务、OCR 定位与网页交互界面。

## 环境与安装

- Python 3.10 或更高版本（具体兼容性取决于依赖版本）
- Android 手机，开启开发者选项和 USB 调试
- Android Platform Tools（`adb` 已加入 PATH）
- 可用的模型 API 密钥

```bash
git clone https://github.com/peter-lgxj/phone_ddl_agent.git
cd phone_ddl_agent
pip install -r requirements.txt
```

用 `adb devices` 确认手机已连接并完成授权。将 `.env.example` 复制为 `.env`，填写所使用服务的密钥。默认执行模型为 `qwen`，规划模型为 `qwen3`（单任务与多应用模式都从 `config.PLANNER_PROVIDER` 读取），均读取 `DASHSCOPE_API_KEY`。其他模型与接口配置见 `config.py`。

首次运行会通过 EasyOCR 下载中文识别模型（需要联网，体积较大）；截图定位还依赖 uiautomator2 在手机上安装的自动化组件。

`.env`、任务运行记录和手机界面导出文件不提交到仓库。

## 运行

```bash
# 网页界面（http://localhost:5000）
python app.py

# 单任务命令行
python run.py

# 多应用任务命令行
python multi_agent.py

# 可选 Streamlit 界面
streamlit run frontend.py
```

Windows 用户也可通过 `start.bat` 启动；该脚本要求已有名为 `mobile_agent` 的 Conda 环境。

可选 Streamlit 界面（`streamlit run frontend.py`）读取 `agent_state.json` 展示每步状态，属于调试用途；`streamlit` 不在必需依赖里，未安装就不用该入口。

## 配置

模型与行为开关集中在 `config.py`，`.env` 只放密钥：

| 配置项 | 说明 |
|---|---|
| `ACTIVE_PROVIDER` | 执行模型（视觉），默认 `qwen` → `qwen-vl-max`，需要 `DASHSCOPE_API_KEY` |
| `PLANNER_PROVIDER` | 规划/语义模型，默认 `qwen3`，单任务与多应用模式都读这一项 |
| `MAX_STEPS` / `STEP_RETRIES` | 最大步数（默认 30，可用同名环境变量覆盖）与单步失败重试次数 |
| `LLM_TEMPERATURE` | 决策与观察调用温度，默认 0.0，保证重复实验可比 |
| `ENABLE_SEMANTIC_ANALYSIS` | 消融开关：关闭后跳过文字模型的语义校正 |
| `ARTIFACT_DIR` | 每步截图与动作记录目录，默认 `runs/` |
| `WECHAT_INPUT_RESOURCE_ID` | 微信输入框 resource-id，随版本变化，置空即跳过该分支 |

可切换的 provider 见 `config.PROVIDERS`；`deepseek` / `minimax` 没有视觉能力，不能用作 `ACTIVE_PROVIDER`。

## 项目结构

- 核心链路：`screen_agent.py`（屏幕理解与操作循环）、`phone_controller.py`（ADB/uiautomator2 操作）、`ocr_locator.py`（OCR + 控件树定位）、`input_detector.py`（YOLO 输入框兜底）、`app_launcher.py`（应用启动）
- 记忆与状态：`memory.py`（步骤记录）、`ui_state.py`（Streamlit 状态）
- 规划：`task_planner.py`（子任务拆解与重规划，含 `context_needed` 信息传递）、`multi_agent.py`（多应用编排）
- 界面：`app.py` + `templates/`（Flask + SocketIO），`frontend.py`（可选 Streamlit）
- 配置：`config.py`、`.env.example`
- 实验：`experiments/`（任务集、重复 runner、指标聚合、baseline 适配层）
- 辅助脚本：`get_packages.py`（导出手机已安装应用包名）、`debug_detector.py`（YOLO 权重自检）、`find_input.py`（手动探测各高度比例能否点出键盘）
- 文档：`test.md` 为对比实验方案（任务集、指标、评判标准）

输入框定位优先使用 uiautomator2 的控件树（`EditText` 等）；`models/input_box.pt` 是 YOLO 视觉兜底权重，未随代码提供，缺失时会跳过兜底并打印提示，不会中断任务。需要视觉定位时请自行准备权重放到 `models/` 下。

## 实验

`experiments/tasks.json` 收录 `test.md` 的 20 个任务（含 App、理论步数、难度等级），runner 按 repeat 次数重复执行并落盘：

```bash
# 执行任务集（需要真机与密钥），每个任务重复 3 次
python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3 --mode single

# 聚合 TSR / SE / AT / ST / CS
python experiments/metrics.py experiments/results/<run_dir>

# 消融：关闭语义分析层
python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3 --no-semantic --tag no_semantic
```

结果默认写到 `experiments/results/`（已在 `.gitignore` 中）；判定为"成功/失败"仍需人工复核，脚本只负责采集和聚合。

## 注意事项

执行任务会操作真实手机，请在测试设备上验证流程，并关注涉及消息发送、下单和支付的操作。上传前仅进行 Python 语法检查，尚未在连接手机和模型 API 的环境中验证完整流程。

截图会经模型 API 上传至对应服务商；涉及聊天记录等隐私内容时请自行评估。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
