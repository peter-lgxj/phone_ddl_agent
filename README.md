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

用 `adb devices` 确认手机已连接并完成授权。将 `.env.example` 复制为 `.env`，填写所使用服务的密钥。默认执行模型为 `qwen`，规划模型为 `qwen3`，均读取 `DASHSCOPE_API_KEY`。其他模型与接口配置见 `config.py`。

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

## 项目结构

- `screen_agent.py`：屏幕理解与操作循环
- `task_planner.py` / `multi_agent.py`：任务拆解与多应用协调
- `phone_controller.py` / `app_launcher.py`：手机控制与应用启动
- `ocr_locator.py` / `input_detector.py`：OCR 和输入框检测
- `memory.py` / `ui_state.py`：任务记忆与界面状态
- `app.py` / `templates/`：Flask 网页界面
- `config.py` / `.env.example`：模型配置和密钥模板

输入框检测使用的 `models/input_box.pt` 权重未随代码提供，使用相关功能前需自行准备。`config.json` 为保留的旧版配置；当前模型配置以 `config.py` 为准。

## 注意事项

执行任务会操作真实手机，请在测试设备上验证流程，并关注涉及消息发送、下单和支付的操作。上传前仅进行 Python 语法检查，尚未在连接手机和模型 API 的环境中验证完整流程。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
