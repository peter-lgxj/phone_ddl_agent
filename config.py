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

# 当前使用的规划模型（纯文字大语言模型，用于任务分解）
# 可选：deepseek / qwen / openai
# deepseek-chat 推理强且便宜，强烈推荐
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
        "model": "qwen3.5-flash",          # 可选: qwen-vl-plus / qwen-vl-max
        # "support_multi_image": True,
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),   # ← 填入 DeepSeek Key（规划器默认用这个）
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

INPUT_BOX_MODEL = "models/input_box.pt"

# ============================================================
#  其他配置
# ============================================================

MAX_STEPS = 20          # 最大执行步数
OBSERVE_CONFIDENCE = 0.4  # OCR 置信度阈值