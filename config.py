import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# ============================================================
#  模型配置文件
#  切换模型只需修改 ACTIVE_PROVIDER 和对应的 API_KEY
# ============================================================

# 当前使用的执行模型（视觉模型，用于屏幕理解和操作）
# 可选：deepseek（默认）/ zhipu / qwen / openai / local
# deepseek 经实测能读图（见 README「DeepSeek 视觉能力」），单请求 3 张图均被正确理解
# local 指向本机 llama-server（Qwen3-VL-8B GGUF），当前「暂缓」，需要时先启动 8080 服务
# 可用环境变量 ACTIVE_PROVIDER 临时切换（如 ACTIVE_PROVIDER=qwen python run.py），不用改代码
ACTIVE_PROVIDER = os.getenv("ACTIVE_PROVIDER", "deepseek")

# 当前使用的规划模型（用于任务分解与语义校正，只发文字不发截图）
# 可选：deepseek（默认）/ qwen3 / minimax；deepseek 同时具备视觉能力，但规划阶段不使用
# deepseek 走 https://api.deepseek.com（OpenAI 兼容，不要加 /v1），模型 deepseek-flash
# 也可切回 qwen3（DashScope 兼容接口，模型 qwen3.5-flash）
PLANNER_PROVIDER = "deepseek"

# ============================================================
#  各平台配置
# ============================================================

PROVIDERS = {
    "zhipu": {
        "api_key": os.getenv("ZHIPU_API_KEY", ""),
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "model": "glm-4v-plus",          # 可选: glm-4v-flash / glm-4v-plus
        "support_multi_image": True,      # 是否支持单次传多张图
    },
    "qwen": {
        "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-flash",          # 可选: qwen-vl-plus / qwen-vl-max
        "support_multi_image": True,
    },
    "qwen3": {
        "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-flash",          # 纯文本模型，可选: qwen3.5-flash / qwen-max / qwen-plus
        "support_multi_image": False,      # 纯文字模型，不能直接当视觉执行器
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        # 官方 OpenAI 兼容地址不要带 /v1：SDK 会自行拼 /chat/completions
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",        # DeepSeek-V4.1-Flash-1017，实测支持图像输入
        # 实测：单请求 3 张图全部被正确理解（各图唯一编号逐张答对）
        "support_multi_image": True,
        # 关闭 thinking：实测同一张截图动作一致（均为 home、JSON 合法），
        # 但耗时 1.4s vs 3.5s、reasoning_tokens 从 363 降到 0
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "minimax": {
        "api_key": os.getenv("MINIMAX_API_KEY", ""),
        "base_url": "https://api.minimax.chat/v1",
        "model": "abab6.5s-chat",
        "support_multi_image": False,
    },
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "support_multi_image": True,
    },
    # 本机 llama.cpp 的 llama-server（OpenAI 兼容），部署见 README「本地视觉模型」
    # llama-server 不校验密钥，默认给一个非空值只为满足 OpenAI 客户端
    "local": {
        "api_key": os.getenv("LOCAL_VLM_API_KEY", "local"),
        "base_url": os.getenv("LOCAL_VLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        "model": os.getenv("LOCAL_VLM_MODEL", "qwen3-vl-8b-instruct"),
        "support_multi_image": True,
    },
}

# 各 provider 需要的环境变量名，启动校验时用来给出可执行的提示
API_KEY_ENV = {
    "zhipu":    "ZHIPU_API_KEY",
    "qwen":     "DASHSCOPE_API_KEY",
    "qwen3":    "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax":  "MINIMAX_API_KEY",
    "openai":   "OPENAI_API_KEY",
    "local":    "LOCAL_VLM_API_KEY",
}

# 纯文本 provider：把截图发给它们会直接报错或得到无意义结果，不能当执行模型
# deepseek 已实测支持图像输入，不在此列
TEXT_ONLY_PROVIDERS = {"qwen3", "minimax"}

# YOLO 权重默认是单类别 input_box 模型；如另有含 search_icon 类别的权重，
# 可用 GUI_MODEL_PATH 指向它，否则搜索图标只走控件树/OCR 兜底。
INPUT_BOX_MODEL = os.getenv("GUI_MODEL_PATH", "models/input_box.pt")

# 微信输入框的 resource-id 随版本变化，置空则跳过该分支只走通用控件/YOLO 定位
WECHAT_INPUT_RESOURCE_ID = "com.tencent.mm:id/b4z"

# ============================================================
#  其他配置
# ============================================================

# 最大执行步数：与 test.md 实验规程的 30 步保持一致，可用环境变量覆盖（如 MAX_STEPS=20）
# 步数按「模型请求」计：一次响应里用 actions 数组返回多个连续动作也只算一步。
MAX_STEPS = int(os.getenv("MAX_STEPS", "30"))
# 一次响应最多接受几个连续动作；超出部分截断，避免单个请求里塞入不可控的动作序列
MAX_ACTIONS_PER_STEP = int(os.getenv("MAX_ACTIONS_PER_STEP", "3"))
STEP_RETRIES = 2            # 单步失败后的重试次数，避免一次 API 抖动直接判任务失败
# 生产读屏默认只读：拒绝 send / press_enter / type 等会在群内发言或改动内容的动作。
# 单独跑任务脚本（run.py / multi_agent.py）时可设 READ_ONLY_MODE=0 关闭。
READ_ONLY_MODE = os.getenv("READ_ONLY_MODE", "1").strip().lower() not in (
    "0", "false", "off", ""
)

# 屏幕驱动：
#   auto        —— 先试 uiautomator2；Android 17 等 RPC 不兼容时自动回退 adb_dump
#   uiautomator2 —— 强制 u2，不兼容时给出明确报错
#   adb_dump    —— 强制用系统 uiautomator dump + screencap（Android 17 回退）
SCREEN_DRIVER = os.getenv("SCREEN_DRIVER", "auto")

LLM_TEMPERATURE = 0.0       # 决策/观察调用温度，固定后重复实验才可比

# 语义分析：每步额外一次纯文本请求。实测每步约 2s，且多数场景下决策模型已能自行理解历史，
# 默认关闭；需要消融对比时置 1。
ENABLE_SEMANTIC_ANALYSIS = os.getenv("ENABLE_SEMANTIC_ANALYSIS", "0") not in ("0", "", "false", "False")

# 完成判断策略（减少每步一次带图请求）：
#   decision    —— 由决策响应里的 task_done 字段给出（默认，省一次视觉请求）
#   every_step  —— 旧行为：每步额外发一张图问 YES/NO
#   off         —— 完全依赖模型主动 finish
COMPLETION_CHECK = os.getenv("COMPLETION_CHECK", "decision")

# OCR 调用策略（1080x2374 上每步全屏 OCR 约 10-14s，是单步最大固定开销）：
#   on_demand —— 默认：决策前不跑 OCR；仅当模型给出 tap（需要精确落点）时跑一次，
#                根据 target 匹配元素后吸附；没有 target 时才使用吸附半径
#   always    —— 旧行为：每步决策前都跑 OCR，并把结果写进提示词
#   off       —— 完全不跑 OCR
OCR_MODE = os.getenv("OCR_MODE", "on_demand")
# 无 target 的 tap 使用此吸附半径；有 target 时，唯一匹配直接吸附，
# 多个匹配按模型坐标选择最近的候选。
OCR_SNAP_MAX_DISTANCE = int(os.getenv("OCR_SNAP_MAX_DISTANCE", "80"))
# 是否在 tap 目标为搜索图标/联系人时启用 YOLO 候选；默认开，
# 但只有加载到含 search_icon 类别的权重时才会产生搜索图标候选。
SEARCH_ICON_DETECTION = os.getenv("SEARCH_ICON_DETECTION", "1").strip().lower() not in (
    "0", "false", "off", ""
)
# 候选来源：tree_first（默认，控件树与 OCR 合并）/ ocr_only / tree_only
OCR_SNAP_MODE = os.getenv("OCR_SNAP_MODE", "tree_first")
# OCR 引擎：rapidocr（默认，ONNX CPU 约 1-2s）/ easyocr（旧实现，保留对照）
OCR_ENGINE = os.getenv("OCR_ENGINE", "rapidocr")
# OCR 输入图缩放比例（只影响 OCR，与 VLM 的 IMAGE_MAX_SIDE 无关）：
# 识别框会按 1/scale 映射回原始截图坐标，tap 坐标仍是设备坐标系。
# 1.0 关闭缩放；默认值按实测速度/小字识别率取平衡。
OCR_SCALE = float(os.getenv("OCR_SCALE", "0.5"))

# 动作执行后等待界面稳定的秒数（再截图观察）
OBSERVE_SETTLE_SECONDS = float(os.getenv("OBSERVE_SETTLE_SECONDS", "1.0"))
# settle 策略：adaptive（默认，截图哈希连续两次相同就提前返回，最多等 OBSERVE_SETTLE_SECONDS）
# / fixed（旧行为，固定等待满）
OBSERVE_SETTLE_MODE = os.getenv("OBSERVE_SETTLE_MODE", "adaptive")
OBSERVE_SETTLE_POLL_SECONDS = float(os.getenv("OBSERVE_SETTLE_POLL_SECONDS", "0.15"))

# 观察并入决策：默认开。一次带图请求同时返回 observation 与 action，
# 默认 COMPLETION_CHECK=decision 时每步 1 次视觉请求（旧行为 2 次）。
# 合并响应 JSON 不合法时本步回退旧的两段式（observe + decide），动作仍只执行一次。
MERGED_OBSERVE = os.getenv("MERGED_OBSERVE", "1").strip().lower() not in (
    "0", "false", "off", ""
)
# 启动时预热 OCR 模型（RapidOCR 首次加载约 7s，挪到启动阶段）
OCR_PREHEAT = os.getenv("OCR_PREHEAT", "1").strip().lower() not in (
    "0", "false", "off", ""
)

# 滚动观察时是否额外跑 OCR：模型只给概括时，仍能把屏幕文字带进 observation。
# 开关默认开启，只对 OBSERVE_OCR_ACTIONS 中的动作生效，不影响点击/输入等场景。
OBSERVE_OCR_ENABLED = os.getenv("OBSERVE_OCR_ENABLED", "1").strip().lower() not in (
    "0", "false", "off", ""
)
OBSERVE_OCR_ACTIONS = {
    item.strip().lower()
    for item in os.getenv("OBSERVE_OCR_ACTIONS", "swipe,scroll").split(",")
    if item.strip()
}

# 规则快路：开群、返回、滚动、点已知 resource-id 的输入框这类能被控件树证明的动作
# 不调用视觉模型，直接执行。只有需要看懂截图语义时才走 VLM。命中与省下的调用数
# 会写进 step JSON 的 timings.rule_* 与 usage_summary.json 的 rule_fastpath。
#
# 成本核算：规则步至少要两次控件树 dump（判断 + 观察取文字）加一次截图；
# adb_dump 下单次 dump 实测约 2.5s，规则步估算 6.3s，已高于一次合并决策请求
# （235deea 真机 A/B：MERGED_OBSERVE=1 反而从 20.63s 变 71.07s）。
# auto（默认）：规则步估算 <= 模型决策耗时才开；on/off 强制开关。
_rule_env = os.getenv("RULE_FASTPATH", "auto").strip().lower()
RULE_FASTPATH_MODE = (
    "off" if _rule_env in ("0", "false", "off")
    else "on" if _rule_env in ("1", "true", "on")
    else "auto"
)
# 兼容旧读取点：只有显式 off 才是 False，auto 交由启动时的成本核算决定
RULE_FASTPATH = RULE_FASTPATH_MODE != "off"
RULE_DUMP_SECONDS = float(os.getenv("RULE_DUMP_SECONDS", "2.5"))        # adb_dump
RULE_DUMP_SECONDS_U2 = float(os.getenv("RULE_DUMP_SECONDS_U2", "0.3"))  # uiautomator2
RULE_SCREENSHOT_SECONDS = float(os.getenv("RULE_SCREENSHOT_SECONDS", "1.0"))
RULE_EXECUTE_SECONDS = float(os.getenv("RULE_EXECUTE_SECONDS", "0.3"))
# 一次合并决策请求（含截图预处理与模型往返）的实测耗时上界。235deea 之后的
# 真机 A/B：模型步 5.20–5.58s，规则步（轻观察）5.52–5.59s；规则步能省掉一次
# 列表导航请求，任务总耗时 11.44s vs 17.81s，因此默认开启。可用环境变量覆盖。
RULE_MODEL_DECISION_SECONDS = float(os.getenv("RULE_MODEL_DECISION_SECONDS", "5.6"))
# 同一次会话里规则最多连续滚多少步；到上限后交回 VLM，避免无限翻页。
# 默认 2：真实群消息翻两屏仍未出现答案时，规则继续滚的收益抵不过每次 dump 的开销。
RULE_MAX_SCROLLS = int(os.getenv("RULE_MAX_SCROLLS", "2"))
# 实验开关：1 时目标即使在可见行也强制走搜索，用于对比搜索/列表两条路径；
# 默认 0，正常行为是「可见行优先 open_group，不可见才搜索」。
RULE_SEARCH_FORCE = os.getenv("RULE_SEARCH_FORCE", "0").strip().lower() in (
    "1", "true", "on"
)

ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "runs")  # 每步截图与动作记录的归档目录

# 连接预热：启动时复用同一个 HTTP 连接池，并预热设备通道（u2 RPC / adb server）。
# 关掉后每次请求仍由 SDK/驱动自行建连，不影响正确性，只是第一步可能多一次握手。
CONNECTION_WARMUP = os.getenv("CONNECTION_WARMUP", "1").strip().lower() not in (
    "0", "false", "off", ""
)
# 视觉与规划两个 provider 共用连接池；keep-alive 秒数与最大连接数按单机单任务设置
HTTP_KEEPALIVE_SECONDS = float(os.getenv("HTTP_KEEPALIVE_SECONDS", "90"))
HTTP_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "8"))

# 历史上下文窗口：只带最近 N 张历史原图（当前屏幕始终带图），更早步骤压成文字摘要。
# 6GB 显存跑本地 Qwen3-VL 时，多张原图会显著抬高视觉 token 与显存占用，默认只带 1 张。
HISTORY_IMAGE_WINDOW = int(os.getenv("HISTORY_IMAGE_WINDOW", "1"))
# 没有原图可带的历史步骤，最多列多少条文字摘要（含动作类型、关键参数与观察结论）
HISTORY_TEXT_STEPS = int(os.getenv("HISTORY_TEXT_STEPS", "8"))

# ============================================================
#  截图预处理（截图之后、发请求之前）
#  注意：服务端每张图最多 1024 token 并自动缩放到约 1300x1300 等效，
#  因此这里缩放只省上传带宽，不省 token；缩放过必须做坐标反向映射。
# ============================================================

# 总开关，置 0 时原样发送 PNG（不缩放、不转码、不增强）
IMAGE_PREPROCESS = os.getenv("IMAGE_PREPROCESS", "1") not in ("0", "", "false", "False")
# JPEG 质量；置 0 保留 PNG
IMAGE_JPEG_QUALITY = int(os.getenv("IMAGE_JPEG_QUALITY", "85"))
# 等比缩放的长边上限；0 表示不缩放
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "0"))
# 轻量增强（对比度 + 锐化），不改变几何尺寸；默认关闭
IMAGE_ENHANCE = os.getenv("IMAGE_ENHANCE", "0") not in ("0", "", "false", "False")