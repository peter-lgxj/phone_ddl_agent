# 手机智能体（Phone Agent）

使用视觉语言模型理解 Android 手机屏幕，通过 ADB 和 uiautomator2 执行操作；支持任务规划、多应用任务、OCR 定位与网页交互界面。

## 操作模型：电脑驱动手机（先读这一段）

- **操作对象是手机**。微信（群聊/私聊）、QQ（群聊）、Outlook App 收件箱的界面，全部在**手机上**被读取和点击。
  agent 不会在电脑上打开微信/QQ/Outlook 桌面客户端，也不读取电脑本地邮件
  （本地 `.eml` / 聊天归档只是没有手机时的离线测试替身）。
- **运行环境是电脑**。本项目在电脑上运行，通过 **ADB（USB 线或 WiFi 调试）** 驱动手机。
  手机端**不安装任何本项目组件**，也**不需要 root**。首次使用时 uiautomator2 可能自行安装一个自动化辅助组件，
  那是 uiautomator2 的既有机制，不是本项目的部署。
- 一句话：**电脑上的 agent 通过 ADB 操作手机 GUI**，把手机里的群聊和邮件 DDL 汇总成日历。

> 手机读屏已在单台真机、微信「文件传输助手」自聊会话上做过一次只读验证
> （见 `docs/real-device-test-report.md`）；真实课程群、QQ / Outlook App、图片型 DDL
> 与真实实验数据仍未验证。
>
> Outlook **只走手机 App 读屏**，不接 Microsoft Graph；Graph 相关代码只是保留的未启用骨架，
> 不是可切换的第二条读取路径。

## 一键启动（Windows）

网页入口：运行 `python app.py` 后打开 `http://127.0.0.1:5000/ddl`，可在页面里用自然语言
添加微信 / QQ / Outlook 手机来源、开始扫描并直接查看日历；首页也有「DDL 日历」入口。
扫描会占用手机，接口默认只允许本机调用；需要从局域网访问时设置 `DDL_WEB_TOKEN`，
并在请求头 `X-DDL-Token` 中带上同一个值。真正的设备互斥由扫描线程持有的设备锁保证，
不会出现「预检通过后被其他窗口抢占」的情况。

1. 首次使用，先告诉 agent 要监控什么（自然语言，会写入 `sources.yaml`）：

   ```bash
   python -m ddl_agent.monitor.cli add "监控微信「21级课程群」和 QQ「机器学习课程群」，每6小时一次"
   python -m ddl_agent.monitor.cli list
   ```

2. 双击仓库根目录的 `run_ddl_monitor.bat`。脚本会依次检查 adb、手机是否在线且已授权、设备锁，
   然后询问「1. 单轮扫描 / 2. 持续监控」，跑完自动刷新并打开 `output/calendar.html`。

3. 产物：`output/deadlines.xlsx`（含审计表）、`data.json`、`deadlines.ics`（可导入系统日历）、`calendar.html`。

没插手机、未授权 USB 调试、找不到 adb、手机被其他窗口占用（设备锁）时，脚本会分别给出对应提示与下一步动作，
不会抛 Python traceback。详细说明见 [docs/ddl-agent-monitor.md](docs/ddl-agent-monitor.md)。

## 离线评测与消融

```bash
# 三方对比：Agent-LLM / Agent-规则 / 单次直出 baseline
python -m ddl_agent.evaluation.run_eval --split test

# RQ4 单模块消融：全开 / 关去重 / 关冲突验证 / 固定来源
python -m ddl_agent.evaluation.run_eval --ablation --split test \
  --out experiments/ddl_agent/results/ablation_distractors2

# 语义匹配三策略对比：词法 / 本地 embedding / 组合
python -m ddl_agent.evaluation.run_eval --similarity-ablation --split test \
  --out experiments/ddl_agent/results/similarity_test4
```

数据集里每个样本都带有与查询无关的干扰来源（实习内推群、家人群、社团活动群、其他课程群），
用于检验动态选源；这些消息不进入 ground truth。

embedding 是**可选**能力：默认不装、不联网也能跑评测。

```bash
# 安装可选依赖（不进入主 requirements / CI）
pip install -r requirements-embedding.txt

# 模型与框架缓存默认在用户目录（~/.cache/ddl-agent/sentence-transformers）；可显式指定
set DDL_EMBEDDING_CACHE=%USERPROFILE%\.cache\ddl-agent\sentence-transformers
set DDL_EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2
```

首次运行会下载多语种小模型；**离线环境**把整个缓存目录拷到目标机器，或让 `DDL_EMBEDDING_CACHE`
指向已有目录即可。模型缺失、下载失败或加载异常时，评测自动退回纯词法，报告的
`similarity.available` 为 `false` 并给出 `fallback_reason`，不会中断流程。
真实模型用例默认跳过，设置 `RUN_EMBEDDING_TESTS=1` 才执行。

逐条 proposal 符合度、RQ4 消融与三种相似度的前后对比数字见
[docs/proposal-compliance.md](docs/proposal-compliance.md)。
所有数字来自合成数据；真机只做过单机、单账号自聊会话的只读读取验证
（`docs/real-device-test-report.md`），真实群聊、QQ / Outlook 与用户研究尚未验证。

## 环境与安装

- Python 3.10 或更高版本（具体兼容性取决于依赖版本）
- Android 手机，开启开发者选项和 USB 调试
- Android Platform Tools（`adb` 已加入 PATH；未加入时可用 `ADB_BINARY` 指向 adb 可执行文件，或直接复用 `uiautomator2` 自带的 adb，`adb_utils.py` 会自动兜底）
- 可用的模型 API 密钥

```bash
git clone https://github.com/peter-lgxj/phone_ddl_agent.git
cd phone_ddl_agent
# 与 CI 完全一致：运行时 + 测试依赖都从这一份锁文件安装
pip install -r requirements-lock.txt
```

`requirements-lock.txt` 是本项目在 Windows + Python 3.12 下实际解析出的完整版本清单（含传递依赖），也是 **CI 与本地安装的唯一真源**；`requirements-dev.txt` 只保留 `-r requirements-lock.txt` 的兼容入口。实验室机器或 GPU 环境请重新 `pip freeze` 替换 lock。

> 新增测试依赖时只改 `requirements-lock.txt`，不要再往 `ci.yml` 里加手写 pip 清单；详见[开发与测试](#开发与测试)。

`requirements.txt` 只维护运行时依赖的最低版本，供需要自行解析环境的场景参考。

用 `adb devices` 确认手机已连接并完成授权。将 `.env.example` 复制为 `.env`，填写所使用服务的密钥。默认执行模型与规划模型都是 `deepseek`（`deepseek-flash`，读 `DEEPSEEK_API_KEY`）；执行模型可切回 `qwen`（`qwen-vl-max`，读 `DASHSCOPE_API_KEY`）。其他模型与接口配置见 `config.py`。

首次运行默认使用 RapidOCR，会下载对应的 ONNX 模型（需要联网，体积较小）；只有显式设置 `OCR_ENGINE=easyocr` 时才会使用 EasyOCR 及其模型。截图定位还依赖 uiautomator2 在手机上安装的自动化组件。

`.env`、任务运行记录和手机界面导出文件不提交到仓库。

## Android 17（API 37）与 ADB 回退驱动

### 现状与判据

真机 OPPO PLJ110 / Android 17 / 1080×2374 上，`uiautomator2` 3.7.1 在第一次
`d.info` RPC 就抛 `ApplicationSharedMemory not initialized`，`ScreenAgent`
无法截图和点击；系统自带的 `adb shell uiautomator dump` 与 `screencap` 正常。

- **升级结论**：截至 2026-11-21，PyPI 最新版就是 `uiautomator2==3.7.1`（2026-10-23）；
  上游 issue [openatx/uiautomator2#1209](https://github.com/openatx/uiautomator2/issues/1209)
  记录从 3.3.1 升到 3.7.1 后错误不变，仍是 open。**目前没有任何已发布版本能解决 Android 17**。
- **版本下限**：`requirements.txt` 写 `uiautomator2>=3.7.1`。这是非 Android 17 设备的
  实测基线；Android 17 上即使满足该下限也会失败，必须用回退驱动。
- **判据**：`SCREEN_DRIVER=auto`（默认）会先 `u2.connect()` 并读取 `device.info`；
  连接失败（无 adb / 未授权）仍给出原来的连接错误，只有“已连接但 RPC 不可用”
  （`ApplicationSharedMemory`、SDK 37 或 `device.info` 异常）才回退 `adb_dump`。
  `SCREEN_DRIVER=uiautomator2` 强制 u2，不兼容时直接给出含 issue 链接与回退命令的明确报错；
  `SCREEN_DRIVER=adb_dump` 强制回退。

### 两种驱动怎么选

| 能力 | `uiautomator2`（完整链路） | `adb_dump`（回退） |
|---|---|---|
| 截图 | `device.screenshot()` | `adb exec-out screencap -p` |
| 控件树 | `device.dump_hierarchy()` | `adb shell uiautomator dump` + `cat` |
| tap / swipe | u2 `click` / `swipe` | `adb shell input tap` / `input swipe` |
| back / home | u2 `press` | `adb shell input keyevent 4/3` |
| 打开 App | u2 / monkey | `adb shell monkey -p <pkg>` |
| 当前界面文本 | 控件树 | dump XML 的 `text` / `content-desc` |
| 输入框 | `EditText` 选择器 | dump 中找 `EditText` 并点中心 |
| type / send / press_enter | 上层只读保护 | **驱动层再拦**：只读时抛 `PermissionError` |
| u2 专有能力 | XPath、`set_text`、长连接 session | 不支持；当前 `ScreenAgent` 不依赖 |

```powershell
# 推荐：自动探测，Android 17 自动回退
python run.py

# 强制回退（已知 u2 故障或要做 A/B）
$env:SCREEN_DRIVER="adb_dump"; python run.py

# 强制 u2（排查用；Android 17 会在这里得到明确报错）
$env:SCREEN_DRIVER="uiautomator2"; python run.py
```

回退发生时：

- `runs/<run>/step_NNN.json` 顶层写 `"driver": "adb_dump"`；
- 任务结束的 `usage_summary.json` 写 `driver` / `driver_mode` / `full_pipeline` /
  `driver_note`，其中 `full_pipeline=false`，明确标注**本次不是完整 uiautomator2 链路**；
- 启动日志与任务结束都会打印回退提示。

### 回退模式的 A/B 与判分口径

回退驱动不会改变 `ScreenAgent` 的决策、观察、OCR、settle、证据归档与 token 统计，
因此 `step_NNN.json` 仍包含 `timings` / `usage` / `image` / `driver`，
`experiments/summarize_timings.py`、`experiments/run_experiment.py`、`experiments/metrics.py`
都能照常运行。若某步画面哈希与上一帧相同，`step_NNN.json` 顶层写 `reused_frame=true`，
`timings.reuse_reason="hash"`，该步跳过重新截图与重编码；settle 保留了稳定帧但画面已变时，
`reuse_reason="settle"`、`reused_frame=false`，只省截图不省编码。但报告口径要注意：

- `driver=adb_dump` 的 run **不能和 `uiautomator2` run 混进同一组均值**。A/B 时把
  `driver` 当作分组变量，或在表格里单列“回退模式”。
- 回退模式能给出：决策/观察/OCR/settle/execute 阶段耗时、每步 token、动作执行结果、
  是否完成；不能给出 u2 专有路径的指标（如 `dump_hierarchy` 内部 RPC 延迟、`set_text`）。
- 离线单测用 `runs/qq_real_device_20261121` 裁出的 dump XML 与截图回放，
  CI 不需要真机；真机验证需要自己连接 Android 17 设备后执行：

```powershell
adb devices
# 预期在 Android 17 上失败，并给出可读错误
python -c "import uiautomator2 as u2; print(u2.connect().info)"
# 回退驱动冒烟：能截图、dump、点击即可
$env:SCREEN_DRIVER="adb_dump"; python run.py
```

### 中文输入与控件树限制

- **中文输入（`adb_dump`）**：`adb shell input text` 只可靠支持 ASCII。非 ASCII 文本改走
  ADBKeyboard 的 `ADB_INPUT_B64` 广播（正文按 UTF-8 编码后 base64，避免 shell 编码问题），
  发送后从控件树校验输入框文字是否真的变化。前置条件：
  1. 安装 ADBKeyboard（`com.android.adbkeyboard`）；
  2. 启用并设为当前输入法：
     `adb shell ime enable com.android.adbkeyboard/.AdbIME`、
     `adb shell ime set com.android.adbkeyboard/.AdbIME`。
  未安装/未启用或校验未通过时，智能体不会重复同一个输入动作：记录 `blocked_reason`
  并提示改用列表滚动定位，连续两次无效输入直接中止任务。
- **微信控件树为空**：部分微信版本/页面（尤其 WebView、自绘列表）经
  `uiautomator dump` 只能拿到很少或空节点。这是平台限制，不做代码绕过；
  读屏回退到截图 + OCR，控件树只作为辅助。
- **QQ 群定位不用搜索**：QQ 群一律走「消息列表 / 联系人 > 群聊 + 截图 OCR 匹配群名」，
  禁止打开搜索框、禁止输入中文。OCR 只接受与群名完全相等或「群名 + 少量未读数」的
  标题，避免把 `2026 计算机班级二群` 当成 `2026 计算机班级群`；连续两屏不变或滚动
  超过 `QQ_LIST_MAX_SCROLLS`（默认 8）就停止。
- **QQ 搜索页 dump**：真机复测 `uiautomator dump` 在搜索页可正常返回（约 360~374 个
  节点），此前空树是 dump 与截图/OCR 并发或页面切换中的瞬时失败，并非
  `could not get idle state`。关闭动画（`animator_duration_scale` 等）后节点数无提升；
  实验后已恢复设备原值。QQ 定位仍不依赖该页控件树。

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
| `ACTIVE_PROVIDER` | 执行模型（视觉），默认 `deepseek` → `deepseek-flash`，需要 `DEEPSEEK_API_KEY`；可设 `qwen` / `zhipu` / `openai` / `local` |
| `PLANNER_PROVIDER` | 规划/语义模型，默认 `deepseek`（`deepseek-flash`），单任务与多应用模式都读这一项；可切回 `qwen3` |
| `MAX_STEPS` / `STEP_RETRIES` | 最大步数（默认 30，可用同名环境变量覆盖）与单步失败重试次数。步数按**模型请求**计：一次响应用 `actions` 数组返回多个连续动作也只算一步 |
| `MAX_ACTIONS_PER_STEP` | 一次响应最多接受的连续动作数，默认 3；超出截断。动作按顺序执行，`step_NNN.json` 里记 `action_type="sequence"` 与 `actions` 数组 |
| `READ_ONLY_MODE` | 只读保护，默认开启：拒绝 `send` / `press_enter` / `type`，monitor 生产读屏强制开启；单独跑任务脚本时可置 0 |
| `SCREEN_DRIVER` | 屏幕驱动：`auto`（默认，u2 不可用时自动回退 `adb_dump`）/ `uiautomator2`（强制 u2，失败明确报错）/ `adb_dump`（Android 17 回退） |
| `LLM_TEMPERATURE` | 决策与观察调用温度，默认 0.0，保证重复实验可比 |
| `ENABLE_SEMANTIC_ANALYSIS` | 语义分析（每步一次纯文本请求），默认关闭；置 1 可开启消融对比 |
| `COMPLETION_CHECK` | 完成判断：`decision`（默认，读决策响应里的 `task_done`）/ `every_step`（旧行为，每步额外一次带图请求）/ `off` |
| `MERGED_OBSERVE` | 观察并入决策，默认开；正常每步 1 次带图请求，响应不合法时本步回退两段式 |
| `RULE_FASTPATH` | 规则快路，默认开：任务里点名的群/会话、返回、以及进入会话后的滚动，只要控件树能证明就直接执行，不调用视觉模型；命中与省下的调用数写入 `step_NNN.json` 的 `timings.rule_*` 和 `usage_summary.json` 的 `rule_fastpath` |
| `RULE_MAX_SCROLLS` | 规则快路在同一个会话里最多连续滚几步（默认 6），到上限交回模型 |
| `CONNECTION_WARMUP` | 启动时预热连接，默认开：视觉与规划共用同一个 httpx 连接池（keep-alive），并发一次 `models.list()` 完成 TLS 握手；设备侧调用驱动 `warmup()`（adb 回退时启动 adb server）。预热耗时写 `usage_summary.json` 的 `warmup_seconds`，不计入第一步 |
| `HTTP_KEEPALIVE_SECONDS` / `HTTP_MAX_CONNECTIONS` | 共用连接池的 keep-alive 秒数（默认 90）与最大连接数（默认 8） |
| `OCR_PREHEAT` | 启动时预热 RapidOCR，默认开 |
| `OBSERVE_SETTLE_MODE` | `adaptive`（默认，界面稳定即返回）/ `fixed`（固定等满 `OBSERVE_SETTLE_SECONDS`） |
| `OBSERVE_SETTLE_POLL_SECONDS` | 自适应 settle 的截图轮询间隔，默认 0.15s |
| `OCR_MODE` | OCR 调用策略：`on_demand`（默认，仅 tap 时跑并吸附落点）/ `always`（旧行为，每步预跑）/ `off` |
| `OCR_SNAP_MODE` | 候选来源：`tree_first`（默认，控件树、YOLO 与 OCR 合并后按目标匹配）/ `ocr_only` / `tree_only` |
| `OCR_ENGINE` | OCR 引擎：`rapidocr`（默认，ONNX CPU 实测 1.2–1.5s/张）/ `easyocr`（旧实现，12.7–23s/张） |
| `OCR_SCALE` | OCR 输入图缩放比例，默认 0.5；检测框按 `1/scale` 映射回原图，tap 坐标仍是设备坐标 |
| `OCR_SNAP_MAX_DISTANCE` | 吸附半径（像素），默认 80；超出则保留模型原始坐标 |
| `SEARCH_ICON_DETECTION` | tap 目标为搜索/联系人时是否补 YOLO 候选，默认开；只有权重含 `search_icon` 类别时才产生搜索图标候选 |
| `GUI_MODEL_PATH` | YOLO 权重路径，默认 `models/input_box.pt`；指向含 `search_icon` 的权重可启用搜索图标视觉候选 |
| `OBSERVE_SETTLE_SECONDS` | 动作后等待界面稳定的秒数，默认 1.0 |
| `HISTORY_IMAGE_WINDOW` / `HISTORY_TEXT_STEPS` | 历史上下文窗口：带最近几张原图（默认 1，当前屏幕始终带图）、更早步骤最多列几条文字摘要（默认 8）；均可用同名环境变量覆盖 |
| `IMAGE_PREPROCESS` | 截图预处理总开关（默认开）；置 0 时原样发送 PNG |
| `IMAGE_JPEG_QUALITY` | JPEG 质量，默认 85；置 0 保留 PNG |
| `IMAGE_MAX_SIDE` | 等比缩放的边长上限，默认 0（不缩放）。**缩放会破坏坐标一致性，见下节** |
| `IMAGE_ENHANCE` | 对比度/锐化增强，默认关；不改变几何尺寸 |
| `ARTIFACT_DIR` | 每步截图与动作记录目录，默认 `runs/` |
| `WECHAT_INPUT_RESOURCE_ID` | 微信输入框 resource-id，随版本变化，置空即跳过该分支 |

可切换的 provider 见 `config.PROVIDERS`；`qwen3` / `minimax` 是纯文本 provider，不能用作 `ACTIVE_PROVIDER`。

规划器默认用 DeepSeek：`base_url=https://api.deepseek.com`（不要加 `/v1`）、模型 `deepseek-flash`、密钥读 `.env` 的 `DEEPSEEK_API_KEY`。规划器只发文字，不接收截图；实测简单任务约 2 秒、三应用任务约 3.5 秒返回可解析 JSON。若开启 `thinking`（通过 `extra_body` 传 `{"thinking": {"type": "enabled"}}`）会返回 `reasoning_content` 并产生 reasoning token，当前默认关闭以降低延迟；实测开关两种模式返回的 `content` 都是合法 JSON。

## DeepSeek 视觉能力（实测）

官方文档写的是 `deepseek-v4-flash-vision-exp` 已下线、旧名请求由 DeepSeek-V4.1-Flash-1017（即 `deepseek-flash`）承接，字面读起来像是纯文本模型。**实测与该表述不一致：`deepseek-flash` 能正常接收并理解 `image_url`。**

- 控制图法：现场生成含随机编号的图片（`ORDER NO: ZX-739146`、`CNY 4287.35` 等），模型逐字读对，可排除「不报错但忽略图片」。
- 真实桌面截图（1707×1067）：10.4s，`prompt_tokens=1012`，准确描述图标、窗口与正文段落。
- 多图：单请求 3 张图（各有唯一编号），三张全部按顺序答对，因此 `support_multi_image=True`。
- 端到端：走 `ScreenAgent._think_and_act` 的真实调用路径，5.9s 返回 `{"action_type": "tap", "x": 540, "y": 1441, ...}`，字段完整、坐标与真值差 1px。

结论：`deepseek-flash` 可作为执行（视觉）模型；本文档保留「官方文档表述与实测不一致」这条记录，后续如服务端行为变化需重新验证。

### 图片限制与 thinking

- **每张图最多 1024 token**：服务端会把图片缩放到约 1300×1300 等效后再计费，实测 1707×1067 的截图 `prompt_tokens=1012`，已贴近上限。**客户端缩图省不到 token**。
- **`detail` 语义**：`low` 会压到 512×512（手机界面小字会糊，不要用于执行模型）；`high` / `original` / `auto` 保留原图。实测控制图 `detail=low` 的 `prompt_tokens=240` vs `detail=high` 的 `631`，代价是细节丢失。
- 图片可放在 user / system / tool 消息；assistant 消息带图会 400；单请求最多 600 张。本项目一次只发 1–3 张，不涉及上限。
- **thinking 开关**（`extra_body`）：同一张截图四种设置的动作完全一致（都是 `home`，JSON 均合法），但耗时与 reasoning token 差异明显：

  | 设置 | 耗时 | completion_tokens | reasoning_tokens |
  |---|---|---|---|
  | `thinking` disabled | 1.42s | 60 | 0 |
  | `reasoning_effort=low` | 2.01s | 139 | 102 |
  | 默认（不传） | 3.51s | 404 | 363 |
  | `thinking` enabled | 3.19s | 296 | 258 |

  质量无差异，因此 provider 默认 `{"thinking": {"type": "disabled"}}`，省时间也省费用。

## 截图预处理与坐标一致性

截图后、发请求前会依次做：编码优化（PNG→JPEG）→ 可选等比缩放 → 可选轻量增强。实现见 `image_preprocess.py`，全部由 `config.py` 的 `IMAGE_*` 开关控制。

**为什么默认不缩放**：实测客户端缩图不会减少 token（服务端本来就封顶 1024/图），但会让模型返回的坐标脱离发送图坐标系，反算后误差远超可接受范围：

| 档位 | 上传字节 | 发送尺寸 | prompt_tokens | 订单号识别 | 坐标误差 |
|---|---|---|---|---|---|
| 原图 PNG | 78,302 | 1080×1920 | 1075 | 正确 | 0 px |
| JPEG q85 | 82,052 | 1080×1920 | 1075 | 正确 | 10 px |
| JPEG q85 + 长边 1300 | 48,165 | 731×1300 | 680 | 正确 | **375 px** |
| JPEG q85 + 长边 1000 | 34,052 | 562×1000 | 451 | 正确 | **691 px** |

（测试图为模拟手机结算页，红按钮真值中心 `(540, 1440)`。缩放档的原始坐标既不是发送图坐标系也不是原图坐标系，反算后偏差数百像素。）

坐标本身有抖动：同一原图重复 3 次，PNG 误差 210/0/5 px，JPEG q85 为 10/146/38 px；格式不是主因，缩放才是。因此默认 `IMAGE_MAX_SIDE=0`，只做 JPEG 转码。

**体积收益**：真实桌面截图 PNG 730 KB → JPEG q85 224 KB（30.7%），token 不变（1044）。上传省流量、减少超时，但不省推理成本。

若确需缩放，`image_preprocess.map_action_coords` 会把 `x/y`、`x1/y1`、`x2/y2` 按 `scale_x/scale_y` 反算、四舍五入并裁到屏幕范围；每步的 `step_NNN.json` 会记录 `image`（原始/发送尺寸与比例）和 `model_action`（模型原始坐标），可事后核对。未缩放时该函数原样返回，不引入偏移。

## 单步耗时与优化

真机读取实测（OPPO PLJ110 / Android 16 / 1080×2374，微信自聊会话，10 步）：修复前 81.42s（8.14s/步），开启滚动观察与 OCR 后 105.59s（10.56s/步），其中 OCR 26.27s。完整记录见 `docs/real-device-test-report.md`；这是单次结果，不作为稳定性能指标。

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 截图 | 0.2–0.9s | `uiautomator2` 截图 |
| OCR | 0 或 1.2–1.5s | 按需模式只在 tap 步触发；RapidOCR + `OCR_SCALE=0.5` 实测 |
| 决策请求 | 1.7–2.6s | DeepSeek，带历史图 + 当前图 |
| 语义分析 | ≈2s | 仅开启时发生，纯文本 |
| 执行动作 | 1.9–3.0s | 含 `tap`/`swipe` 固定等待、`open_app` 约 2.5s |
| 观察请求 | 4.0–6.1s | 含 1.0s 稳定等待 + 截图 + 带图请求 |
| 完成检测 | ≈2.5s | 旧行为每步一次带图请求 |
| 循环间隔 | 0.5s | 固定 |

优化前每步最多 **4 次模型请求**（决策 / 语义 / 观察 / 完成检测），其中 3 次带图。当前改动：

1. 完成判断并入决策响应（`task_done` 字段），默认不再单独发完成检测请求；
2. 语义分析默认关闭（`ENABLE_SEMANTIC_ANALYSIS=0`）；
3. 补上 vision 请求的 `extra_body`，确保 `thinking` 真正关闭（此前只有规划请求带了该参数）；
4. `type_text` 在输入框已聚焦时直接写入，避免重复点击与再次 YOLO 兜底；
5. OCR 改为按需（`OCR_MODE=on_demand`）：决策前不跑，只有模型给出 `tap` 时才跑；并改为「控件树优先、OCR 兜底」（`OCR_SNAP_MODE=tree_first`），控件树命中时完全不跑 OCR；
6. OCR 引擎换成 RapidOCR（`OCR_ENGINE=rapidocr`，ONNX Runtime，跳过方向分类），并默认按 `OCR_SCALE=0.5` 缩小后再识别：1080×2374 真机截图实测 1.2–1.5s（不缩放约 1.3–1.6s；机器并行负载高时两者都会到 5–8s）。EasyOCR 路径保留，可切回对照；
7. **观察并入决策（`MERGED_OBSERVE=1`，默认开）**：一次带图请求同时返回 `observation` 与 `action`；`step_NNN.json` 的 observation 标注 `observation_scope=pre_action`，表示它来自合并响应、描述本步动作执行前的屏幕。响应缺少 `observation` / `action_type` 或 JSON 不合法时，本步回退旧的两段式（observe + decide），动作仍只执行一次；
8. **OCR 启动预热（`OCR_PREHEAT=1`，默认开）**：RapidOCR 在 `ScreenAgent` 初始化时加载，不把首次 7s 加载摊到第一步；
9. **自适应 settle（`OBSERVE_SETTLE_MODE=adaptive`，默认）**：动作后连续两次截图哈希相同就提前返回，最多等 `OBSERVE_SETTLE_SECONDS`；`fixed` 保留旧的固定等待；
10. 每步 `step_NNN.json` 新增 `timings`（screenshot/ocr_pre/ocr/ocr_triggered/ocr_reason/snap_source/snap_tree/snap_yolo/snap_ocr/ocr_snap/preprocess/semantic/decision/decide_total/execute/observe/settle/completion/merged_fallback），便于统计触发率与省下的时间；`timings.ocr` 已包含控件树、YOLO 与 OCR 三段定位耗时，`snap_yolo` 单独记录 YOLO 段。

按上表估算：旧流程每步最多 4 次模型请求；完成判断并入决策、语义分析关闭后为 2 次；`MERGED_OBSERVE=1` 默认再合并观察与决策，**正常每步 1 次带图请求**。整步耗时按旧阶段耗时推算约 **8–10s → 5–7s**（估算）。真机 A/B 见下一节：235deea 上合并观察反而更慢，原因是同期的规则快路固定滚动 6 次，不是合并观察本身；该问题已按「滚动上限 2 + 轻观察」修复：规则开关实测 11.44s vs 17.81s，规则快路在 adb_dump 下默认开启。OCR 预热只省启动后的第一步约 6–7s，自适应 settle 每步最多省约 1s。真机单次实测见上方报告链接，不作为稳定性能指标。

合并观察的回退路径：若合并响应不合法，本步总计 3 次请求（1 次失败的合并请求 + 1 次 observe + 1 次 decide），动作仍只执行一次；`COMPLETION_CHECK=every_step` 时不做合并，保持原两段式 + 完成检测，避免退化出额外的 task_done 调用。`tests/test_ocr_on_demand.py` 用假客户端断言了默认每步 1 次、关闭合并时 2 次、回退时动作只执行一次。

### 逐次 token 与耗时怎么读

- 每步 `runs/<run>/step_NNN.json` 的 `usage` 是该步视觉调用的 token 汇总（`calls` 里按 `decision` / `observe` / `completion` 列出，缺 usage 时为 `null`）；`timings` 是各阶段耗时。
- 任务正常结束或达到最大步数时写 `runs/<run>/usage_summary.json`：总 prompt/completion/total token、每步均值、按 provider 汇总。
- 阶段耗时汇总（单 run 或多 run 对比）：

```bash
python experiments/summarize_timings.py runs/20261117-211201
python experiments/summarize_timings.py runs/merged_off runs/merged_on
```

输出包含 `decision/observe/ocr/yolo/screenshot/execute/settle` 等阶段的样本数、总计、均值、P50、P90；多目录时追加 `mean / p90` 对比表。

### MERGED_OBSERVE A/B 怎么跑

同一任务分别关闭/开启合并观察，然后对比两个 run：

```powershell
# 1) 关闭合并（旧两段式）
$env:MERGED_OBSERVE="0"; python run.py
# 记录本次生成的 run 目录，例如 runs\20261120-101500-off

# 2) 开启合并
$env:MERGED_OBSERVE="1"; python run.py
# 记录 runs\20261120-102000-on

# 3) 对比阶段耗时与 token
python experiments\summarize_timings.py runs\20261120-101500-off runs\20261120-102000-on
Get-Content runs\20261120-101500-off\usage_summary.json
Get-Content runs\20261120-102000-on\usage_summary.json
```

A/B 时保持同一任务文本、同一设备、同一网络与同一 provider；否则数字不可比。`--json` 可把 `summarize_timings.py` 结果交给其他脚本。

### 验证标签：真机实测 / 离线回放 / 未执行

报告和判分表必须带上来源标签，**离线回放 ≠ 真机实测**：

| 标签 | 含义 | 当前结论 |
| --- | --- | --- |
| 真机实测（基线 235deea） | 设备截图、`step_*.json`、`usage_summary.json` 直接产生 | 同一任务同群、`adb_dump`：`MERGED_OBSERVE=0` 20.63s / 3 请求；`=1` 71.07s / 1 请求；序列不一致（`tap→finish` vs `tap→swipe×6→finish`），是策略差异 |
| 真机实测（2026-11-22 改后，默认规则开启） | 同上 | `MERGED_OBSERVE=0` 21.03s / 5 请求 / 2 dumps；`=1` 11.45s / 1 请求 / 3 dumps（规则 `open_group` + 1 次模型完成，规则步 5.47–5.65s）。两组序列不同（模型导航 vs 规则导航），只能算策略差异 |
| 真机实测（合并观察，规则关闭） | 同上 | 0/1 各 3 次：21.27s vs 17.81s（前两次；第三次超时 56.3s），序列一致（`wait→tap→finish`），合并观察约快 16% |
| 真机实测（规则开关） | 同上 | 强制规则 11.44s / 1 请求 vs 默认关闭 17.81s / 3 请求；规则步 5.5s ≈ 模型步 5.3s，但少一次请求，故默认开启 |
| 真机实测（搜索 vs 列表） | 同上 | 同群各 3 次：搜索 39.01s / 7 dumps / 4 步；列表 11.48s / 3 dumps / 2 步。`adb_dump` 下搜索页 dump 明显更慢，搜索不作为默认（`RULE_SEARCH_FORCE=1` 可强制） |
| 真机实测（monitor） | 单进程 `run_cycle`（QQ 单群） | 7 步 / 51.2s / 19,591 tokens；`fetch→extract→dedupe→resolve→write` 全链路，输出 `data.json`、`deadlines.ics`、`calendar.html`；手机日历测试事件写入后已删除，.ics 生成 VEVENT+VALARM |
| 离线回放 | 冻结真机抽取候选，用当前裁决/聚焦重算；不联网、不碰手机 | 判分复跑 20/21 = 95.24%（D02/D03 未采集，已剔除分母）；证据 `runs/_verify/judge_replay_result.json`（`runs/` 不入库） |
| 离线回放 | 规则停止条件 | 真机实时控件树：聊天页已显示「操作系统作业一」时规则 0 次滚动；群列表 `open_group`；发送区域坐标/文案均拒绝 |

同屏复用与连接预热不列为收益项：6 次归档 A/B 中 `reused_frame` 均为 0；复用只在静态页命中，聊天页因光标/动画哈希变化；复用最多省 `preprocess` 0.11–0.15s/步（monitor 中出现过 1 次同屏复用，但不足以改变任务耗时结论）；连接预热 0.22–0.43s 仅启动一次，每步收益 0.00s。

### 脱敏证据

`experiments/export_evidence.py` 从 `runs/` 导出最小可公开集到 `docs/evidence/<run>/`：保留步数、动作类型、阶段耗时、token usage、观察长度与哈希；剔除全屏截图、发送图、原始 observation/action 文本、群名/课程名与 OCR 原文。示例见 `docs/evidence/20261117-211201/`。不要把 `runs/` 整体提交进仓库。

`OCR_SCALE` 取舍（同一批图片实测，二次调用、机器空闲）：

| scale | 缩放后尺寸 | 耗时 | 设置页元素 | 结算页元素 | 关键字段（￥128.00/订单号/确认支付） | 中心点偏差 |
|---|---|---|---|---|---|---|
| 1.0 | 1080×2374 | 1.3–1.6s | 23 | 14 | 全对 | 基准 |
| 0.75 | 810×1780 | 1.2–1.4s | 25 | 14 | 全对 | 均值 1.2–4.4px，最大 32px |
| **0.5（默认）** | 540×1187 | **1.2–1.5s** | 24 | 14 | 全对 | 均值 1.4–2.9px，最大 32px |
| 0.35 | 378×831 | 1.3–1.5s | 23 | 14 | 全对 | 均值 1.4px，最大 3px |

结论：在本机这批图上，0.5 没有出现漏字，`￥128.00`、订单号、按钮文字均正确，坐标偏差均值 1.4–2.9px（个别长文本行因 bbox 中心变化到 32px，仍在 80px 吸附半径内）。选 0.5 而不是 0.35，是给更小字号/低对比度界面留余量；若某些 App 小字识别率下降，把 `OCR_SCALE` 调回 1.0 即可。

按需定位的触发与路径（`ocr_locator.should_trigger_ocr` + `OCR_SNAP_MODE`）：

- 触发条件：`OCR_MODE=on_demand` 且动作为 `tap`；其余动作（`open_app`/`back`/`home`/`wait`/`swipe`/`type`/`send`/`finish`）完全不跑。
- `tree_first`（默认）：合并控件树、OCR 文本与（按需）YOLO 候选；有 `target` 时按文字匹配打分，唯一匹配直接吸附，多个匹配按模型坐标选最近的一个；没有 `target` 时才用距离阈值。
- `ocr_only` / `tree_only`：强制只走一条路径，便于对照。
- 具名目标匹配不到时先保持界面重新观察；连续两次仍匹配不到（常见于无文字图标），回退到模型坐标继续，不会直接终止任务。
- 无 target 的吸附只在距最近元素中心 ≤ `OCR_SNAP_MAX_DISTANCE`（默认 80px）或落在 bbox 内时发生；实际路径与耗时写进 `timings.snap_source`（`tree`/`ocr`/`yolo`/`none`）、`snap_tree`、`snap_yolo`、`snap_ocr`。



## 本地视觉模型（Qwen3-VL-8B-Instruct GGUF）

> **可选配置，非默认**：默认执行模型与规划模型仍是云端 `deepseek`（见 `config.py` 的
> `ACTIVE_PROVIDER` / `PLANNER_PROVIDER`，离线抽取也默认走 `deepseek`）。本地 8B 仅在显式
> 设置 `ACTIVE_PROVIDER=local` 时启用，并需要自行下载权重与 mmproj（仓库不包含）；
> 6 GB 显存只能把视觉编码器留在 CPU，带图约 75–80 秒/次。

用于不依赖云端视觉 API 的离线执行。权重、mmproj、llama.cpp 运行时与 HF 缓存放在仓库外，默认 `%LOCALAPPDATA%\qwen3vl-local`（可用 `-BaseDir` 或 `LOCAL_VLM_HOME` 覆盖），不写进 git。

### 硬件与参数取舍

本机为 RTX 3060 Laptop（6 GB 显存）。Q4_K_M 权重 5,027,784,800 字节（约 4.68 GiB），mmproj F16 1,159,029,824 字节（约 1.08 GiB），两者合计已超过 6 GB，**不可能全部放进显存**。因此：

- 文本层全部卸载到 GPU：`-ngl 36`
- 视觉编码器留在 CPU：`--no-mmproj-offload`
- KV cache 量化：`--cache-type-k q8_0 --cache-type-v q8_0`
- 上下文：`-c 8192`
- 实测稳定占用约 **5.3 GB / 6.1 GB**（`nvidia-smi` 5367 MiB），无 OOM

代价是视觉编码走 CPU：带一张 1707×1067 截图的请求约 **76–78 秒**；纯文本请求约 10 秒级。若把 mmproj 也放回 GPU，显存不足会 OOM；降低 `-ngl` 可留出余量但文本生成变慢。

### 文件与下载

从同一个 HF 仓库取权重与视觉投影（缺 mmproj 模型看不了图）：

```bash
# 目标目录示例（仓库外，可按 -BaseDir 调整）
mkdir -p "$LOCALAPPDATA/qwen3vl-local/models"

curl -L -o Qwen3VL-8B-Instruct-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf
curl -L -o mmproj-Qwen3VL-8B-Instruct-F16.gguf \
  https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf
```

llama.cpp 使用 `b12470` 的 Windows CUDA 12.4 预编译包：`llama-b12470-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`，解压到同一目录（本机 `runtime\llama-extract\`）。显式设置缓存目录：

```powershell
$env:HF_HOME = "$env:LOCALAPPDATA\qwen3vl-local\hf-cache"
$env:HF_HUB_CACHE = "$env:HF_HOME\hub"
```

### 启动与切换

一键启动（前台运行，端口 8080）：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_local_vlm.ps1
```

脚本参数可调：`-Port`、`-NGpuLayers`、`-Ctx`、`-Threads`、`-BaseDir`、`-ModelDir`、`-LlamaDir`。未指定 `-BaseDir` 时依次使用 `LOCAL_VLM_HOME`、`%LOCALAPPDATA%\qwen3vl-local`。服务提供 OpenAI 兼容接口：

```bash
curl http://127.0.0.1:8080/v1/models
# 模型别名：qwen3-vl-8b-instruct；支持 multimodal（image_url base64）
```

切换执行模型（不用改代码）：

```powershell
$env:ACTIVE_PROVIDER = "local"   # 或 "qwen" 切回云端
python run.py
```

`config.PROVIDERS["local"]` 默认 `base_url=http://127.0.0.1:8080/v1`，可用 `LOCAL_VLM_BASE_URL`、`LOCAL_VLM_MODEL` 覆盖；`llama-server` 不校验密钥，`LOCAL_VLM_API_KEY` 仅需非空（默认 `local`）。

真实请求验证（本机实测）：一张真实桌面截图 → 视觉描述正确；按执行提示词返回 `{"action_type": "home", "params": {}, "observation": "返回桌面"}`，JSON 可解析。带图请求 prompt eval 约 76 秒（CPU 视觉编码），生成约 1–2 秒。未在 Android 真机上跑过，仅验证了模型服务与图片协议。

### 上下文窗口（6 GB 卡为什么要限制）

每张截图都会转成数百到上千个视觉 token，多轮任务里把历史原图全带上会同时抬高上下文与显存。默认策略：`HISTORY_IMAGE_WINDOW=1` 只带最近 1 张历史原图，当前屏幕始终带图；更早的步骤压成文字摘要（动作类型、坐标/输入文字、观察结论），由 `HISTORY_TEXT_STEPS` 控制条数。

调大 `HISTORY_IMAGE_WINDOW` 的代价：视觉 token 与显存占用上升，6 GB 卡更容易 OOM，单步耗时也会增加；建议先在当前设备上逐步加 1 并观察 `nvidia-smi` 与请求耗时。

## 项目结构

- 核心链路：`screen_agent.py`（屏幕理解与操作循环）、`phone_controller.py`（uiautomator2 操作）、`phone_driver.py`（驱动选择/回退）、`adb_controller.py`（Android 17 的 adb dump 回退）、`ocr_locator.py`（OCR + 控件树定位）、`input_detector.py`（YOLO 输入框兜底）、`app_launcher.py`（应用启动）
- 记忆与状态：`memory.py`（步骤记录）、`ui_state.py`（Streamlit 状态）
- 规划：`task_planner.py`（子任务拆解与重规划，含 `context_needed` 信息传递）、`multi_agent.py`（多应用编排）
- 界面：`app.py` + `templates/`（Flask + SocketIO），`frontend.py`（可选 Streamlit）
- 配置：`config.py`、`.env.example`
- 实验：`experiments/`（任务集、重复 runner、指标聚合、baseline 适配层与三个上游 wrapper）
- 测试与工程化：`tests/`（326 passed、2 skipped，CI 全量执行且不依赖真机/模型）、`pyproject.toml`（ruff + pytest 配置）、`.github/workflows/ci.yml`、`requirements-lock.txt`（依赖唯一真源）、`scripts/check_test_deps.py`（测试 import 与 lock 一致性检查）、`scripts/create_pr.py`（UTF-8 安全的 REST 建 PR 脚本）
- 辅助脚本：`get_packages.py`（导出手机已安装应用包名）、`debug_detector.py`（YOLO 权重自检）、`find_input.py`（手动探测各高度比例能否点出键盘）
- 文档：`test.md` 为对比实验方案（任务集、指标、评判标准）

输入框定位优先使用 uiautomator2 的控件树（`EditText` 等）；`models/input_box.pt` 是 YOLO 视觉兜底权重。

### 输入框检测权重（可选）

权重不随本仓库提供（`.gitignore` 忽略了 `models/` 与 `*.pt`，二进制不会进版本库）。需要视觉兜底时从组长的模型仓库获取：

```bash
mkdir -p models
curl -L -o models/input_box.pt \
  https://raw.githubusercontent.com/peter-lgxj/gui_yolo_model/main/runs/detect/runs/input_box5/weights/best.pt
```

- 来源：`peter-lgxj/gui_yolo_model` 的 `runs/detect/runs/input_box5/weights/best.pt`（5.4 MB）
- 目标路径：`models/input_box.pt`，与 `config.INPUT_BOX_MODEL` 一致，不需要改代码
- 可选校验（git blob sha1，与模型仓库一致）：

  ```bash
  git hash-object models/input_box.pt
  # 期望 2f5adef8fe64be1898363e8d38b5098ff4c62c2a
  ```

- 该权重是单类别 `input_box` 的 v1 模型；模型仓库的 `dataset_v2`（`input_box` + `search_icon`）尚无对应权重
- 搜索图标默认走控件树 / OCR 文本匹配；只有当 `GUI_MODEL_PATH` 指向含 `search_icon` 类别的权重时，YOLO 才会补充搜索图标候选。没有这类权重时搜索入口仍可用，只是少一条视觉兜底
- 模型仓库未附 LICENSE，公开分发或转存前请先与作者确认
- 置信度差异：`input_detector.py` 默认 `conf=0.3`，模型仓库的 `test_detector.py` 用 `0.5`；真机误检多时可上调
- 无权重或加载失败时自动降级到 uiautomator2 控件树（`EditText`）检测，不会中断任务

## 实验

`experiments/tasks.json` 收录 `test.md` 的 20 个任务（含 App、理论步数、难度等级），runner 按等级自动决定重复次数（`repeat_by_level`，当前 L1/L2 为 3 次、L3 为 2 次；**L3 最终次数是 `test.md` 4.2 的待定项**，当前值只是实现默认，可用 `--repeat` 强制统一）：

> 外部数据集 `测试数据集\微Q邮\ddl_tasks.json` 的 D04 原文只写「进入班级群」，手机上有多个 QQ 群时可能进错群。`run_experiment.py` 在加载任务后按 `task_id` 覆盖为「进入『2026 计算机班级群』」，**不改数据集原文件**；其他任务原样保留。QQ 单群冒烟可用根目录的 `sources.qq.example.yaml`（复制为 `sources.qq.yaml`），它只扫这一个群，输出/状态目录独立，不影响默认 `sources.yaml`。

```bash
# 执行任务集（需要真机与密钥）
python experiments/run_experiment.py --tasks experiments/tasks.json --mode single

# 聚合 TSR / 多 App TSR / SE / AT / ST / CS
python experiments/metrics.py experiments/results/<run_dir>

# 消融：关闭语义分析层
python experiments/run_experiment.py --tasks experiments/tasks.json --no-semantic --tag no_semantic

# 强制每个任务只跑 3 次（覆盖按等级规则）
python experiments/run_experiment.py --tasks experiments/tasks.json --repeat 3

# 多设备：指定 adb serial（等价于设置 ANDROID_SERIAL）
python experiments/run_experiment.py --tasks experiments/tasks.json --serial emulator-5554

# 失败重放：只重跑上次失败/超时/崩溃的任务
python experiments/run_experiment.py --tasks experiments/tasks.json \
  --only-failed experiments/results/<run_dir>/results.jsonl

# 只跑指定等级 / 指定任务号（不影响按等级的重复次数规则）
python experiments/run_experiment.py --tasks experiments/tasks.json --levels L1,L2
python experiments/run_experiment.py --tasks experiments/tasks.json --task-ids T01,T05

# AT 口径可切换：--at-mode 选样本（all/success/best），--at-aggregate 选跨任务聚合
python experiments/metrics.py experiments/results/<run_dir> --at-mode success
python experiments/metrics.py experiments/results/<run_dir> --at-aggregate total_per_task
```

AT 的两种跨任务聚合（`--at-aggregate`）：

- `task_mean`（默认）：先算每个任务的尝试平均耗时，再对任务等权平均；与旧结果一致
- `total_per_task`：所有纳入统计的 attempt 总耗时 ÷ 任务数

每任务尝试次数相同时两者相等；尝试次数不等（超时、失败重放、缺失判定）时会分叉。`--at-mode` 决定哪些 attempt 纳入统计，两者可组合。

结果默认写到 `experiments/results/`（已在 `.gitignore` 中）。筛选条件（`levels` / `task_ids` / `only_failed`）会写进 `manifest.json` 与每条 `results.jsonl` 的 `filters` 字段，事后能看出"只跑了某几个任务"。runner 会在每个任务前回桌面并 force-stop 本任务涉及的已知应用（`--no-reset` 可关闭），`manifest.json` 记录 git SHA、执行/规划模型全名、温度、任务集哈希与设备信息。

baseline 接入：三个 wrapper 已按上游实际入口构造命令，也都可以用环境变量覆盖完整命令。协议是 stdin 一行 JSON（`task` 必填，`apps` 可选）→ stdout 一行 JSON（`summary/steps/elapsed/returncode/upstream_status`），退出码透传。

**命令按上游文档构造，未实跑**：本机无真机，也没有安装这三套系统的依赖/密钥；下面的入口、参数与输出格式来自上游仓库代码核对，接入前请按上游 README 完成环境准备。

```bash
python experiments/run_experiment.py --baseline openautoglm \
  --baseline-command "python experiments/baselines/openautoglm_wrapper.py"
```

- **Open-AutoGLM**（`zai-org/Open-AutoGLM`）
  - 环境变量：`OPEN_AUTOGLM_DIR`；可选 `OPEN_AUTOGLM_MAX_STEPS`
  - 默认命令：`python main.py [--device-id $ANDROID_SERIAL] [--max-steps N] "<task>"`
  - 任务位置参数是最后一个参数（不是 `--task`）；模型服务用上游的 `--base-url/--model/--apikey` 或 `PHONE_AGENT_*` 环境变量
  - 输出：每步打印 `💭 思考：` / `🎯 动作：`，结束时打印 `Result: <message>`（超步数为 `Result: Max steps reached`）
  - 步骤数按 `🎯` 行统计；`upstream_status` 只区分 `finished` / `max_steps`，不当作成功判定
- **Mobile-Agent-v2**（`X-PLUG/MobileAgent` 的 `Mobile-Agent-v2/`）
  - 环境变量：`MOBILE_AGENT_V2_DIR`；可选 `MOBILE_AGENT_V2_CMD`
  - 默认命令：`python run.py`。上游没有命令行参数，任务与 API 配置写在 run.py 顶部（`instruction` / `adb_path` / `API_url` / `token` / `qwen_api`）；仍为空时 wrapper 会直接报错提示
  - 输出：每步 `### Decision ###` 块（Thought/Action/Operation），截图在 `screenshot/`；动作 `Stop` 结束
  - 步骤数按 ` Decision ` 标题行统计；`upstream_status=stopped` 仅表示出现 Stop
- **AppAgent**（`TencentQQGYLab/AppAgent`，原 `mnotgod96/AppAgent` 已迁移）
  - 环境变量：`APPAGENT_DIR`；应用名取 stdin JSON 的 `apps[0]` 或 `APPAGENT_APP`；可选 `APPAGENT_CMD` / `APPAGENT_STDIN`
  - 默认命令：`python run.py --app <app>`；上游是交互式输入，默认把 `APPAGENT_STDIN`（缺省 `y\n<task>\n`）喂给子进程，对应「无文档库 + 单设备」的提示顺序；环境不同请自行设置
  - 输出：每轮 `Round <n>`，日志在 `tasks/task_<app>_<时间戳>/log_*.txt`；结束文案为 `Task completed successfully` / `Task finished due to reaching max rounds` / `Task finished unexpectedly`
  - 步骤数按 `Round ` 行统计；`upstream_status` 记录对应结束文案，仍不当作成功判定

`BASELINE_STEP_MARKER` 可覆盖步骤标记（多个候选标记用默认值，无需设置）。`success` 仍由人工判定，baseline 的 `upstream_status` 只作为复盘线索写进 `baseline_raw`。协议层已有离线假上游测试。

任务成功与否仍需人工判定：可写 `results.jsonl` 的 `success` 字段，或同目录 `judgement.json`。注意任务级布尔 `{"T01": true}` 只能算 TSR，要算 CS 必须逐次判定 `{"T01": [true, false, true]}`。每任务的原始执行证据在 `runs/<时间戳>/`。

CI（`.github/workflows/ci.yml`）不连接手机、不调用模型。

## 已知限制（部分真机验证）

- 单机、单账号、微信「文件传输助手」自聊会话做过一次只读读取与端到端抽取验证（`docs/real-device-test-report.md`）；真实课程群、QQ / Outlook App、图片型 DDL、点击与输入、跨 App 信息传递仍未在真实环境跑通。
- Android 17（API 37）上 uiautomator2 3.7.1 仍不可用（[#1209](https://github.com/openatx/uiautomator2/issues/1209)），程序会回退到 `adb_dump`。回退链路的真机 A/B 已完成一次（235deea，合并观察两组各 1 次，见「验证标签」），但**修复规则成本后尚未复跑**；修复前的回退样本只用于离线解析，不作为提速证据。
- 依赖版本：`requirements-lock.txt` 是在 Windows + Python 3.12 的虚拟环境里实际解析得到的；换机器（尤其 GPU/系统不同）需重新 `pip freeze` 替换。
- 基线系统（Open-AutoGLM / Mobile-Agent-v2 / AppAgent）的命令、输出解析与配置要求已按上游代码写实，协议用假上游测过；但三个真实系统仍未在本机安装、未接真机联调，命令未实跑。
- 任务前重置只做「回桌面 + force-stop 已知包名」，**无法自动清除应用内草稿、聊天输入框、剪贴板与输入法状态**；`reset` 字段会区分 `stopped` / `failed` / `not_installed` / `not_mapped`。
- `run.py`、`app.py`、`frontend.py`、实验 runner 共用 `runs/.device.lock`：锁覆盖「reset + 整个任务执行期」，拿不到锁的入口会直接报错/记为失败，不会在别人操作手机时 force-stop。锁文件记录 token + pid，释放时校验归属；僵尸锁只在持有进程已退出时回收。
- 需要 Python 3.10+（使用了 `dict | None` 等注解，3.8/3.9 导入即报错）。

## 开发与测试

```bash
pip install -r requirements-lock.txt   # 与 CI 同一份依赖（含 pytest/ruff/openpyxl）
python scripts/check_test_deps.py      # 校验 tests 的第三方 import 都已在 lock 里

pytest -q          # 单元测试：动作容错、指标聚合、任务集校验、配置加载、baseline 协议
ruff check .       # 只启用能发现真实错误的规则（E9/F63/F7/F82）
```

### 用脚本创建 PR（中文不乱码）

PowerShell 5 的 `Invoke-RestMethod` 会按本地 ANSI 代码页序列化 JSON，中文标题偶尔变成 `????`（曾出现 `fix: ???? UI ...`）。统一改用脚本：

```bash
# 创建 PR；仓库默认从 origin 推断
python scripts/create_pr.py --head fix/my-branch --title "fix: 中文标题" --body-file pr_body.md

# 更新已有 PR 的标题/正文
python scripts/create_pr.py --update 19 --title "fix: 中文标题" --body-file pr_body.md
```

- 请求体由 Python 以 UTF-8 字节发送（`json.dumps(ensure_ascii=False).encode("utf-8")`），并显式声明 `charset=utf-8`；
- token 从 `git credential fill` 读取，只放 Authorization 头，不打印明文；
- 创建/更新后立即 GET 回读，逐字比对标题与正文（换行统一为 LF），不一致时非零退出；
- PowerShell 5 与 PowerShell 7 都适用：脚本不依赖 shell 的默认编码。正文建议用 `--body-file`（UTF-8 文件），避免长命令行参数与引号转义。

### 新增测试依赖只改一个文件

1. 在 `requirements-lock.txt` 里加**精确版本**（例如 `bs4==4.12.3`），必要时在 `requirements.txt` 补最低版本；
2. 不要改 `.github/workflows/ci.yml` 的依赖清单——CI 直接 `pip install -r requirements-lock.txt`；
3. 本地跑 `python scripts/check_test_deps.py`（CI 也会跑）：
   - 它静态扫描 `tests/` 的 `import`，把第三方模块映射到发行包名；
   - 若某个 import 不在 lock 里，直接非零退出并列出引用文件；
   - `python -m pip check` 另行校验已安装依赖之间没有冲突。
4. 测试输入只能放 `tests/fixtures/`；**不要读 `runs/`、`output/`、`state/`**，这些目录被 `.gitignore` 忽略，CI checkout 后不存在。测试里有一条守卫会扫描这类路径拼接。

### CI 行为

- 触发：`push` 只在 `main`；feature 分支走 `pull_request`；另留 `workflow_dispatch` 手动触发。
- 并发：`concurrency` 按 workflow + ref 分组，同分支新推送取消旧 run。
- 单 job `test`（ubuntu + Python 3.12）：从 lock 安装 → `pip check` → compileall → tasks JSON → dry-run → ruff → 依赖一致性检查 → 导入冒烟 → pytest。

锁文件是 Windows + Python 3.12 下 `pip freeze` 得到的**版本锁（未带 hash）**；Linux/Windows 的 wheel 集不同，缺 wheel 会直接让 job 失败。将来若用 `pip-compile --generate-hashes`（或 `uv pip compile --generate-hashes`）生成 hash 锁，workflow 会自动改用 `pip install --require-hashes`。

> 历史教训：此前 `ci.yml` 另维护一份手写 pip 子集，新增测试用到 `uiautomator2` / `cv2` / `flask` 时 CI 连续红灯；现已统一为 lock 单源，并由 `check_test_deps.py` 兜底。

## 注意事项

执行任务会操作真实手机，请在测试设备上验证流程，并关注涉及消息发送、下单和支付的操作。

Web 界面的 `SECRET_KEY` 与 CORS 可用环境变量 `FLASK_SECRET_KEY`、`CORS_ALLOWED_ORIGINS` 覆盖，默认值只适合本机调试。

截图会经模型 API 上传至对应服务商；涉及聊天记录等隐私内容时请自行评估。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
