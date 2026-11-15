import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# ============================================================
#  模型配置文件
#  切换模型只需修改 ACTIVE_PROVIDER 和对应的 API_KEY
# ============================================================

# 当前使用的执行模型（视觉模型，用于屏幕理解和操作）
# 可选：zhipu / qwen / deepseek / minimax / openai
ACTIVE_PROVIDER = "qwen"

# 当前使用的规划模型（纯文字大语言模型，用于任务分解与语义校正）
# 必须是 TEXT_ONLY_PROVIDERS 里的纯文本 provider：qwen3（默认）/ deepseek / minimax
# qwen3 走 DashScope 兼容接口，模型为 qwen3.5-flash；deepseek-chat 推理强且便宜，可作替代
PLANNER_PROVIDER = "qwen3"

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
        "model": "qwen-vl-max",          # 可选: qwen-vl-plus / qwen-vl-max
        "support_multi_image": True,
    },
    "qwen3": {
        "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.5-flash",          # 纯文本模型，可选: qwen3.5-flash / qwen-max / qwen-plus
        "support_multi_image": False,      # 纯文字模型，不能直接当视觉执行器
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),   # ← 填入 DeepSeek Key（可作规划器：PLANNER_PROVIDER="deepseek"）
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",         # DeepSeek 暂无视觉模型，纯文字
        "support_multi_image": False,
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
}

# 各 provider 需要的环境变量名，启动校验时用来给出可执行的提示
API_KEY_ENV = {
    "zhipu":    "ZHIPU_API_KEY",
    "qwen":     "DASHSCOPE_API_KEY",
    "qwen3":    "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax":  "MINIMAX_API_KEY",
    "openai":   "OPENAI_API_KEY",
}

# 纯文本 provider：把截图发给它们会直接报错或得到无意义结果，不能当执行模型
TEXT_ONLY_PROVIDERS = {"qwen3", "deepseek", "minimax"}

INPUT_BOX_MODEL = "models/input_box.pt"

# 微信输入框的 resource-id 随版本变化，置空则跳过该分支只走通用控件/YOLO 定位
WECHAT_INPUT_RESOURCE_ID = "com.tencent.mm:id/b4z"

# ============================================================
#  其他配置
# ============================================================

# 最大执行步数：与 test.md 实验规程的 30 步保持一致，可用环境变量覆盖（如 MAX_STEPS=20）
MAX_STEPS = int(os.getenv("MAX_STEPS", "30"))
STEP_RETRIES = 2            # 单步失败后的重试次数，避免一次 API 抖动直接判任务失败
LLM_TEMPERATURE = 0.0       # 决策/观察调用温度，固定后重复实验才可比
ENABLE_SEMANTIC_ANALYSIS = True  # 消融开关：关闭后不调用文字模型做语义校正
ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "runs")  # 每步截图与动作记录的归档目录