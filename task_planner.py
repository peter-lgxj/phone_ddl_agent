"""
task_planner.py  —  多应用任务规划器 v2

修复：
  1. REPLAN_PROMPT JSON 示例里 { } 全部改为 {{ }} 避免 str.format() KeyError
  2. 规划粒度改为「以一个 App 的完整操作为最小单位」
"""

import json
import time
from openai import OpenAI
import config


# 规划模型直接复用 config.PROVIDERS：原先这里维护了一份独立的 provider 表，
# 只有 deepseek/qwen/openai，而 config.PLANNER_PROVIDER 默认是 qwen3，
# 结果规划器静默回落到 qwen-max，与 README 及配置声明的模型不一致。


# ══════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════

class SubTask:
    def __init__(self, index: int, goal: str, app: str = "", depends_on=None,
                 context_needed: str = ""):
        self.index          = index
        self.goal           = goal
        self.app            = app
        self.depends_on     = depends_on or []
        self.context_needed = context_needed   # 需要从前序任务带入的信息（由重规划填充）
        self.status         = "pending"
        self.result         = ""

    def to_dict(self):
        return {"index": self.index, "goal": self.goal, "app": self.app,
                "context_needed": self.context_needed,
                "status": self.status, "result": self.result}

    def __repr__(self):
        return f"[{self.index}]({self.app}) {self.goal} <{self.status}>"


class TaskPlan:
    def __init__(self, raw_task: str, subtasks: list, analysis: str = ""):
        self.raw_task   = raw_task
        self.subtasks   = subtasks
        self.analysis   = analysis
        self.start_time = time.time()
        self.warnings   = []
        self._sanitize_dependencies()

    def _sanitize_dependencies(self):
        """过滤不存在的依赖索引，并去掉成环的依赖边。
        模型给的 depends_on 可能是任意整数，不校验会导致 pending 永远无法满足。
        """
        indexes = {t.index for t in self.subtasks}
        for t in self.subtasks:
            kept = []
            for d in t.depends_on:
                if d not in indexes:
                    self.warnings.append(f"子任务 {t.index} 依赖不存在的子任务 {d}，已忽略")
                elif d == t.index:
                    self.warnings.append(f"子任务 {t.index} 依赖自身，已忽略")
                elif d not in kept:
                    kept.append(d)
            t.depends_on = kept

        graph = {t.index: list(t.depends_on) for t in self.subtasks}
        visiting, finished = set(), set()

        def dfs(node):
            if node in finished:
                return True
            if node in visiting:
                return False
            visiting.add(node)
            for dep in list(graph.get(node, [])):
                if not dfs(dep):
                    graph[node].remove(dep)
                    self.warnings.append(f"检测到环形依赖 {node}->{dep}，已忽略该依赖")
            visiting.discard(node)
            finished.add(node)
            return True

        for node in list(graph):
            dfs(node)
        for t in self.subtasks:
            t.depends_on = list(graph[t.index])

    def pending_tasks(self):
        done_idx = {t.index for t in self.subtasks if t.status in ("done", "skipped")}
        return [t for t in self.subtasks
                if t.status == "pending"
                and all(d in done_idx for d in t.depends_on)]

    def is_all_done(self):
        # failed/timeout 不是 done，否则循环会提前退出并把失败当完成
        return all(t.status in ("done", "skipped") for t in self.subtasks)

    def summary(self):
        icons = {"pending": "⏳", "running": "🔄", "done": "✅",
                 "failed": "❌", "timeout": "⏱", "skipped": "⏭"}
        lines = [f"任务：{self.raw_task}", ""]
        for t in self.subtasks:
            lines.append(f"  {icons.get(t.status,'?')} [{t.index}]({t.app}) {t.goal}")
            if t.result:
                lines.append(f"       └ {t.result}")
        lines.append(f"\n耗时：{time.time()-self.start_time:.1f}s")
        return "\n".join(lines)


# ══════════════════════════════════════════════
#  规划器
# ══════════════════════════════════════════════

def build_exec_goal(subtask: "SubTask") -> str:
    """把前序任务提供的信息显式拼进子任务目标，避免执行层再从零推断"""
    if not subtask.context_needed:
        return subtask.goal
    return (f"{subtask.goal}\n\n"
            f"【前序任务提供的信息（可直接作为事实使用）】\n{subtask.context_needed}")


def unfinished_subtasks(plan: "TaskPlan", task_done: bool = False) -> list:
    """整体任务是否未完成：failed/timeout 一律算，pending 在非显式完成时也算"""
    bad = [t for t in plan.subtasks if t.status in ("failed", "timeout")]
    if task_done:
        return bad
    return bad + [t for t in plan.subtasks if t.status == "pending"]


class TaskPlanner:

    # ── 初始规划 Prompt ───────────────────────
    SYSTEM_PROMPT = """\
你是一个手机多应用任务规划专家。
你的职责是把用户的复杂任务拆解成一个有序的子任务列表，交给手机操作智能体依次执行。

【核心粒度原则（必须严格遵守）】
每个子任务 = 在「一个 App」内完成的「一整块操作」。
正确示例：
  - "打开微信，找到某人的对话，阅读并提取最新消息里的地点信息"   → 1个子任务
  - "打开高德地图，搜索刚才获取的地点，查看路线"               → 1个子任务
  - "打开美团，搜索黄焖鸡米饭，选评分最高的一家，查看菜单价格"  → 1个子任务

错误示例（粒度过细，不能这样拆）：
  - "点击微信图标"   ❌
  - "点击搜索框"     ❌
  - "输入联系人名字" ❌
  以上都是执行层的操作步骤，规划层不应该出现。

【规划规则】
1. 子任务数量通常等于涉及的 App 数量（2~5个，极少超过6个）
2. goal 字段写清楚：在哪个App、做什么事、期望得到什么
3. 如果后续子任务依赖前一步骤的信息（地点/消息/名称等），
   在 context_needed 里注明，执行后会自动注入到后续任务
4. depends_on 填前序子任务 index 的整数列表
5. 不要添加多余步骤

【可直接打开的App】
微信、QQ、微博、钉钉、飞书、美团、饿了么、淘宝、京东、拼多多、
高德地图、百度地图、滴滴、抖音、快手、bilibili、爱奇艺、优酷、
设置、相机、浏览器、支付宝、百度、网易云音乐、QQ音乐

【输出格式】只输出 JSON，不要任何其他内容：
{
  "analysis": "简短分析：涉及哪几个App、执行顺序、关键信息如何传递",
  "subtasks": [
    {
      "index": 1,
      "app": "App中文名",
      "goal": "在[App]里，[完整操作描述]，获取/完成[期望结果]",
      "depends_on": [],
      "context_needed": "需要从前序任务获取的信息（没有则填空字符串）"
    }
  ]
}"""

    # ── 重规划 Prompt（JSON示例里 { } 全部用 {{ }} 转义）─────
    REPLAN_PROMPT_TEMPLATE = """\
你是一个手机多应用任务规划专家，正在动态管理一个复杂任务的执行。

【原始任务】
{raw_task}

【已完成的子任务及结果】
{completed}

【刚完成的子任务】
子任务[{index}]（App：{app}）
目标：{goal}
执行结果：{result}

【原计划中待执行的子任务】
{remaining}

请根据执行结果完成以下判断：
1. 整体任务是否已全部完成？
2. 待执行子任务的 goal 是否需要更新？
   重点：如果执行结果包含后续步骤需要的关键信息（地点名、消息内容、联系人、
   商品名、价格等），必须把这些信息直接嵌入到对应子任务的 goal 字段里。
3. 是否需要增加新的子任务？

【粒度要求】
子任务粒度保持「一个App内的完整操作」，不要细化到单个点击步骤。

【输出格式】只输出 JSON，不要任何其他内容：
{{
  "task_completed": true或false,
  "completion_summary": "完成时写一句总结，未完成时填空字符串",
  "need_replan": true或false,
  "updated_subtasks": [
    {{
      "index": 与原计划一致的数字,
      "app": "App名称",
      "goal": "更新后的目标（如有关键信息请直接写入）",
      "depends_on": [],
      "context_needed": "前序结果中需要带入本任务的关键信息（没有则填空字符串）"
    }}
  ]
}}

注意：
- task_completed 为 true 时，updated_subtasks 填空列表 []
- need_replan 为 false 时，updated_subtasks 原样返回 remaining 列表
- 只要 goal 有更新（哪怕只是嵌入关键信息），need_replan 就应为 true
"""

    def __init__(self):
        name = getattr(config, "PLANNER_PROVIDER", config.ACTIVE_PROVIDER)
        provider = config.PROVIDERS.get(name)
        if provider is None:
            print(f"  ⚠️  未找到规划 provider「{name}」，回退到 {config.ACTIVE_PROVIDER}")
            name = config.ACTIVE_PROVIDER
            provider = config.PROVIDERS[name]
        self.provider_name = name
        self.client = OpenAI(api_key=provider["api_key"], base_url=provider["base_url"])
        self.model  = provider["model"]
        print(f"📋  规划器：{self.provider_name} / {self.model}")

    def _call_llm(self, user_msg: str, system: str = None) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user_msg})
        resp = self.client.chat.completions.create(
            model=self.model, messages=msgs, temperature=0.1)
        return resp.choices[0].message.content.strip()

    def _parse_json(self, raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            inner = lines[1:]
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            raw = "\n".join(inner).strip()
        return json.loads(raw)

    # ── 初始规划 ─────────────────────────────
    def plan(self, task: str) -> TaskPlan:
        print(f"\n🧠  [规划器] 分析任务：{task}")
        raw = self._call_llm(task, system=self.SYSTEM_PROMPT)

        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  ⚠️  规划解析失败（{e}），降级单任务")
            print(f"  原始：{raw[:200]}")
            data = {"analysis": "解析失败，单任务执行",
                    "subtasks": [{"index": 1, "goal": task, "app": "", "depends_on": []}]}

        analysis = data.get("analysis", "")
        print(f"  💡  {analysis}")

        subtasks = []
        for item in data.get("subtasks", []):
            st = SubTask(
                index=item["index"],
                goal=item["goal"],
                app=item.get("app", ""),
                depends_on=item.get("depends_on", []),
                context_needed=item.get("context_needed", ""),
            )
            subtasks.append(st)
            print(f"  📌  [{st.index}]({st.app}) {st.goal}")

        return TaskPlan(task, subtasks, analysis=analysis)

    # ── 动态重规划 ────────────────────────────
    def replan(self, plan: TaskPlan, just_done: SubTask) -> tuple[bool, str]:
        """
        子任务完成后调用。
        返回 (task_completed, completion_summary)
        并就地更新 plan.subtasks 里 pending 任务的 goal。
        """
        completed = [t for t in plan.subtasks if t.status == "done"]
        remaining = [t for t in plan.subtasks if t.status == "pending"]

        if not remaining:
            bad = [t for t in plan.subtasks if t.status in ("failed", "timeout")]
            if bad:
                detail = "；".join(f"[{t.index}] {t.goal} -> {t.status}" for t in bad)
                return False, f"{len(bad)} 个子任务失败或超时，不能视为完成：{detail}\n{plan.summary()}"
            return True, f"所有子任务已完成。\n{plan.summary()}"

        completed_text = "\n".join(
            f"  [{t.index}]({t.app}) {t.goal}\n    → {t.result}"
            for t in completed
        ) or "（无）"

        remaining_text = json.dumps(
            [{"index": t.index, "app": t.app, "goal": t.goal,
              "context_needed": t.context_needed} for t in remaining],
            ensure_ascii=False, indent=2
        )

        # 用 TEMPLATE.format()，JSON示例里的 {{ }} 会被还原成 { }
        prompt = self.REPLAN_PROMPT_TEMPLATE.format(
            raw_task=plan.raw_task,
            completed=completed_text,
            index=just_done.index,
            app=just_done.app,
            goal=just_done.goal,
            result=just_done.result or "（无结果）",
            remaining=remaining_text,
        )

        print(f"\n🔄  [规划器] 子任务[{just_done.index}]完成，评估后续计划...")
        raw = self._call_llm(prompt)

        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  ⚠️  重规划解析失败（{e}），继续原计划")
            print(f"  原始：{raw[:200]}")
            return False, ""

        # 整体完成？
        if data.get("task_completed", False):
            summary = data.get("completion_summary", "任务已完成")
            print(f"  🎉  规划器：整体任务已完成")
            return True, summary

        # 更新后续子任务
        if data.get("need_replan", False):
            updated     = data.get("updated_subtasks", [])
            updated_map = {item["index"]: item for item in updated}
            existing    = {t.index for t in plan.subtasks}

            for t in plan.subtasks:
                if t.status != "pending":
                    continue
                if t.index in updated_map:
                    item = updated_map[t.index]
                    new_goal = item.get("goal", t.goal)
                    new_app  = item.get("app", t.app)
                    new_ctx  = item.get("context_needed", t.context_needed)
                    if new_goal != t.goal or new_app != t.app or new_ctx != t.context_needed:
                        print(f"  ✏️  更新[{t.index}]({new_app})：{new_goal}")
                        t.goal = new_goal
                        t.app  = new_app
                        t.context_needed = new_ctx

            for item in updated:
                if item["index"] not in existing:
                    new_st = SubTask(
                        index=item["index"],
                        goal=item["goal"],
                        app=item.get("app", ""),
                        depends_on=item.get("depends_on", []),
                        context_needed=item.get("context_needed", ""),
                    )
                    plan.subtasks.append(new_st)
                    print(f"  ➕  新增[{new_st.index}]({new_st.app})：{new_st.goal}")

            # 模型可能返回不存在的依赖或成环依赖，重新校验后再决定是否还有 pending
            plan._sanitize_dependencies()
            plan.subtasks.sort(key=lambda t: t.index)

        return False, ""
