import config
import base64
import json
from zhipuai import ZhipuAI

class VisionAnalyzer:
    """视觉分析器：调用 GLM-4V 分析手机截图"""

    def __init__(self, api_key):
        self.client = ZhipuAI(api_key=api_key)

    def analyze_screen(self, image_path, task=""):
        """分析屏幕截图，返回结构化描述"""

        # 将图片转为 base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        prompt = f"""你是一个手机操作智能体，请分析这张手机截图。

当前任务：{task if task else "描述当前屏幕内容"}

请用 JSON 格式回复，包含以下字段：
{{
  "current_app": "当前 App 名称或页面名称",
  "screen_description": "简短描述当前页面内容",
  "visible_elements": [
    {{"element": "元素名称", "type": "按钮/输入框/列表项等", "description": "描述"}}
  ],
  "suggested_action": "为了完成任务，下一步应该做什么",
  "task_completed": false
}}

只返回 JSON，不要其他文字。"""

        response = self.client.chat.completions.create(
            model="glm-4v-flash",  # 免费模型
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
        )

        raw = response.choices[0].message.content
        # 清理可能的 markdown 代码块
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {"raw_response": raw}

        return result


# ===== 测试入口 =====
if __name__ == "__main__":
    API_KEY = config.PROVIDERS["zhipu"]["api_key"]

    analyzer = VisionAnalyzer(API_KEY)

    print("=== 分析屏幕截图 ===")
    result = analyzer.analyze_screen("screen.png", task="点外卖")

    print(json.dumps(result, ensure_ascii=False, indent=2))