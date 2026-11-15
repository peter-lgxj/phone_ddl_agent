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
# 需要可复现环境时改用锁定版本（在目标机器上重新生成）：
# pip install -r requirements-lock.txt
```

`requirements-lock.txt` 是本项目在 Windows + Python 3.12 下实际解析出的完整版本清单（含传递依赖），仅作为参考基线；实验室机器或 GPU 环境请重新 `pip freeze`。

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
- 实验：`experiments/`（任务集、重复 runner、指标聚合、baseline 适配层与三个 wrapper 骨架）
- 测试与工程化：`tests/`（33 个用例，其中 22 个不依赖 torch/easyocr，CI 会执行；其余在装齐依赖的本地环境执行）、`pyproject.toml`（ruff + pytest 配置）、`.github/workflows/ci.yml`、`requirements-lock.txt` / `requirements-dev.txt`
- 辅助脚本：`get_packages.py`（导出手机已安装应用包名）、`debug_detector.py`（YOLO 权重自检）、`find_input.py`（手动探测各高度比例能否点出键盘）
- 文档：`test.md` 为对比实验方案（任务集、指标、评判标准）

输入框定位优先使用 uiautomator2 的控件树（`EditText` 等）；`models/input_box.pt` 是 YOLO 视觉兜底权重，未随代码提供，缺失时会跳过兜底并打印提示，不会中断任务。需要视觉定位时请自行准备权重放到 `models/` 下。

## 实验

`experiments/tasks.json` 收录 `test.md` 的 20 个任务（含 App、理论步数、难度等级），runner 按等级自动决定重复次数（Level 1/2 为 3 次，Level 3 为 2 次，可用 `--repeat` 强制统一）：

```bash
# 执行任务集（需要真机与密钥）
python experiments/run_experiment.py --tasks experiments/tasks.json --mode single

# 聚合 TSR / 多 App TSR / SE / AT / ST / CS
python experiments/metrics.py experiments/results/<run_dir>

# 消融：关闭语义分析层
python experiments/run_experiment.py --tasks experiments/tasks.json --no-semantic --tag no_semantic

# 强制每个任务只跑 3 次（覆盖按等级规则）
python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3
```

结果默认写到 `experiments/results/`（已在 `.gitignore` 中）。runner 会在每个任务前回桌面并 force-stop 本任务涉及的已知应用（`--no-reset` 可关闭），`manifest.json` 记录 git SHA、执行/规划模型全名、温度、任务集哈希与设备信息。

任务成功与否仍需人工判定：可写 `results.jsonl` 的 `success` 字段，或同目录 `judgement.json`。注意任务级布尔 `{"T01": true}` 只能算 TSR，要算 CS 必须逐次判定 `{"T01": [true, false, true]}`。每任务的原始执行证据在 `runs/<时间戳>/`。

CI（`.github/workflows/ci.yml`）不连接手机、不调用模型。

## 已知限制（未在真实环境验证）

- 全部运行时行为（点击坐标、OCR 识别、模型决策、重试、跨 App 信息传递）只经过代码审查与导入冒烟，未在真机上跑通。
- 依赖版本：`requirements-lock.txt` 是在 Windows + Python 3.12 的虚拟环境里实际解析得到的；换机器（尤其 GPU/系统不同）需重新 `pip freeze` 替换。
- 基线系统（Open-AutoGLM / Mobile-Agent-v2 / AppAgent）只有 wrapper 骨架与协议，未在真实仓库上跑过。
- 任务前重置只做「回桌面 + force-stop 已知包名」，**无法自动清除应用内草稿、聊天输入框、剪贴板与输入法状态**；重置是否成功以 `results.jsonl` 的 `reset` 字段为准（含 `failed` / `not_mapped`）。
- runner 用 `runs/.device.lock` 做进程级互斥：与 Web 界面或另一个 runner 并发时，拿不到锁会跳过重置而不是打断对方任务。
- 需要 Python 3.10+（使用了 `dict | None` 等注解，3.8/3.9 导入即报错）。

## 开发与测试

```bash
pip install -r requirements-dev.txt

pytest -q          # 单元测试：动作容错、指标聚合、任务集校验、配置加载、baseline 协议
ruff check .       # 只启用能发现真实错误的规则（E9/F63/F7/F82）
```

CI（`.github/workflows/ci.yml`）会做语法编译、任务集 JSON 校验、runner dry-run、ruff 与 pytest。uiautomator2/easyocr/ultralytics 已改为延迟导入，CI 不需要安装它们即可导入 `screen_agent` 并执行全部单元测试（错误导入会直接失败，而不是 skip）。

## 注意事项

执行任务会操作真实手机，请在测试设备上验证流程，并关注涉及消息发送、下单和支付的操作。

Web 界面的 `SECRET_KEY` 与 CORS 可用环境变量 `FLASK_SECRET_KEY`、`CORS_ALLOWED_ORIGINS` 覆盖，默认值只适合本机调试。

截图会经模型 API 上传至对应服务商；涉及聊天记录等隐私内容时请自行评估。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
