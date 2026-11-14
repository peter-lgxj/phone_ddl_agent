import json
import time


class TaskMemory:
    """
    任务记忆管理器。
    记录每一步的 Thought / Action / Observation，
    并提供格式化输出供 Prompt 使用。
    """

    def __init__(self, task: str):
        self.task = task
        self.steps = []          # 每步完整记录
        self.start_time = time.time()

    # def add_step(self, thought: str, action: dict, observation: str):
    #     """记录一步"""
    #     self.steps.append({
    #         "step": len(self.steps) + 1,
    #         "thought": thought,
    #         "action_type": action.get("action_type", ""),
    #         "action_desc": action.get("description", ""),
    #         "observation": observation,
    #     })
    def add_step(self, thought: str, action: dict, observation: str):
        self.steps.append({
            "step": len(self.steps) + 1,
            "thought": thought,
            "action_type": action.get("action_type", ""),
            "action_desc": action.get("description", ""),
            "text": action.get("text", ""),       # ← 新增，保存输入的文字
            "observation": observation,
        })

    # def format_for_prompt(self, last_n: int = 6) -> str:
    #     """
    #     格式化最近 N 步供 Prompt 使用。
    #     只保留最近几步，避免 token 过多。
    #     """
    #     if not self.steps:
    #         return "尚未执行任何操作，刚刚开始。"

    #     recent = self.steps[-last_n:]
    #     lines = []
    #     for s in recent:
    #         lines.append(
    #             f"第{s['step']}步 [{s['action_type']}] {s['action_desc']}\n"
    #             f"  → 执行后观察：{s['observation']}"
    #         )
    #     return "\n".join(lines)
    def format_for_prompt(self, last_n: int = 6) -> str:
        if not self.steps:
            return "尚未执行任何操作，刚刚开始。"

        recent = self.steps[-last_n:]
        lines = []
        for s in recent:
            lines.append(
                f"第{s['step']}步 [{s['action_type']}] {s['action_desc']}\n"
                f"  → 验证结果：{s['observation']}"
            )

        # 单独强调最后一步的结果，让 AI 重点关注
        last = self.steps[-1]
        lines.append(
            f"\n⚠️ 上一步验证结果：{last['observation']}\n"
            f"请根据上一步验证结果决定下一步，不要重复已成功的操作。"
        )

        return "\n".join(lines)

    def get_last_observation(self) -> str:
        """获取最后一步的观察结果"""
        if self.steps:
            return self.steps[-1]["observation"]
        return "尚未有任何观察"

    def summary(self) -> str:
        """任务结束后的统计摘要"""
        elapsed = time.time() - self.start_time
        return (
            f"任务：{self.task}\n"
            f"共执行：{len(self.steps)} 步\n"
            f"耗时：{elapsed:.1f} 秒"
        )

    def save(self, path="task_log.json"):
        """保存完整记录到文件，方便调试和写论文分析"""
        data = {
            "task": self.task,
            "steps": self.steps,
            "total_steps": len(self.steps),
            "elapsed_seconds": round(time.time() - self.start_time, 1)
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"📄  任务日志已保存：{path}")