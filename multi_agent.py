"""
multi_agent.py  —  多应用任务编排器（Orchestrator）

职责：
  把 TaskPlanner（规划）和 ScreenAgent（执行）连接起来，
  实现「规划 → 执行 → 观察结果 → 重规划 → 执行」的循环。

  emit_fn 回调：用于向前端推送 plan_ready / subtask_start /
               subtask_done / replan 等事件（可选）
"""

import json
import time
import subprocess

from task_planner import TaskPlanner, TaskPlan, SubTask, build_exec_goal
from screen_agent import ScreenAgent
from memory import TaskMemory
import config


# ── 终端颜色辅助 ─────────────────────────────────────
class C:
    RESET  = '\033[0m'
    BOLD   = '\033[1m'
    CYAN   = '\033[96m'
    AMBER  = '\033[93m'
    GREEN  = '\033[92m'
    RED    = '\033[91m'
    PURPLE = '\033[95m'
    BLUE   = '\033[94m'
    DIM    = '\033[2m'


def _sep(char='─', n=56, color=C.DIM):
    print(f"{color}{char * n}{C.RESET}")


class SubTaskScreenAgent(ScreenAgent):
    """
    ScreenAgent 的轻量包装：
    run() 执行完后返回结果摘要字符串。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_summary = ""
        self._sub_index   = 0

    def run(self, task: str) -> str:
        """执行单个子任务，返回结果摘要（共用基类的重试与证据归档）"""
        from pathlib import Path

        memory = TaskMemory(task)
        self.last_memory = memory   # 供实验脚本统计实际步数

        # 每个子任务单独一个证据目录，避免多次执行互相覆盖
        self._sub_index += 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(config.ARTIFACT_DIR) / f"{stamp}_sub{self._sub_index}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        import ui_state
        ui_state.init_state(task)

        for step in range(1, self.max_steps + 1):
            print(f"\n{C.BLUE}  ┌─ Step {step} {'─'*34}{C.RESET}")
            try:
                finished, done, summary = self._execute_step(step, memory)
            except Exception as e:
                self.last_summary = f"第 {step} 步失败：{e}"
                self.last_status = "failed"
                raise
            print(f"{C.DIM}  └{'─'*40}{C.RESET}")

            if finished or done:
                summary = summary or "子任务完成"
                self.last_summary = summary
                self.last_status = "done"
                memory.save(path=f"task_log_sub{len(memory.steps)}.json")
                memory.save(path=str(self.run_dir / "task_log.json"))
                return summary

            time.sleep(0.5)

        summary = f"达到最大步数，最后状态：{memory.get_last_observation()}"
        self.last_summary = summary
        # 超步数不算完成，交给上层标记 failed/timeout
        self.last_status = "timeout"
        memory.save(path=f"task_log_sub{len(memory.steps)}.json")
        memory.save(path=str(self.run_dir / "task_log.json"))
        return summary


# ══════════════════════════════════════════════
#  多应用编排器
# ══════════════════════════════════════════════

class MultiAppAgent:
    """
    多应用任务编排器。

    emit_fn: 可选的事件推送回调，签名 emit_fn(event: str, data: dict)
             用于 Flask-SocketIO 集成（app.py 里传入 socketio.emit）
    """

    def __init__(self, emit_fn=None, exec_agent=None):
        self.planner    = TaskPlanner()
        # 允许注入执行器（Web 端需要子类化以推送步骤事件），避免重复构造
        self.exec_agent = exec_agent or SubTaskScreenAgent(max_steps=config.MAX_STEPS)
        self.emit_fn    = emit_fn or (lambda e, d: None)
        self.log_path   = "multi_task_log.json"
        self.total_steps = 0   # 本次多应用任务累计执行的子任务步数

    def _emit(self, event: str, data: dict):
        try:
            self.emit_fn(event, data)
        except Exception as e:
            # 事件推送失败不应影响任务执行，但必须留下线索
            print(f"{C.DIM}  ⚠️  事件推送失败({event})：{e}{C.RESET}")

    def _go_home(self):
        subprocess.run(["adb", "shell", "input", "keyevent", "3"], capture_output=True)
        time.sleep(1)

    def _save_log(self, plan: TaskPlan):
        data = {
            "raw_task":        plan.raw_task,
            "subtasks":        [t.to_dict() for t in plan.subtasks],
            "elapsed_seconds": round(time.time() - plan.start_time, 1),
        }
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"{C.DIM}📄  多任务日志已保存：{self.log_path}{C.RESET}")

    def run(self, task: str):
        print(f"\n{C.CYAN}{C.BOLD}{'═'*56}{C.RESET}")
        print(f"{C.CYAN}{C.BOLD}  🚀  多应用任务启动{C.RESET}")
        print(f"{C.CYAN}  📝  {task}{C.RESET}")
        print(f"{C.CYAN}{C.BOLD}{'═'*56}{C.RESET}")

        # ── 规划 ─────────────────────────────────
        plan = self.planner.plan(task)

        if not plan.subtasks:
            print(f"{C.RED}  ❌  规划器未生成任何子任务{C.RESET}")
            return

        # 向前端推送规划结果
        self._emit('plan_ready', {
            'analysis':  getattr(plan, 'analysis', ''),
            'subtasks':  [
                {'index': t.index, 'goal': t.goal, 'app': t.app,
                 'context_needed': t.context_needed}
                for t in plan.subtasks
            ]
        })

        print(f"\n{C.CYAN}{'─'*56}{C.RESET}")
        print(f"{C.CYAN}  📋  共 {len(plan.subtasks)} 个子任务{C.RESET}")
        _sep()

        # ── 循环执行 ─────────────────────────────
        max_loops = len(plan.subtasks) * 2
        loop_count = 0

        while not plan.is_all_done() and loop_count < max_loops:
            loop_count += 1

            candidates = plan.pending_tasks()
            if not candidates:
                print(f"{C.AMBER}  ⚠️  无可执行子任务（依赖未满足）{C.RESET}")
                break

            current = candidates[0]
            current.status = "running"

            # 推送子任务开始
            self._emit('subtask_start', {
                'index': current.index,
                'goal':  current.goal,
                'app':   current.app,
            })

            print(f"\n{C.CYAN}{'─'*56}")
            print(f"  📍  子任务 [{current.index}/{len(plan.subtasks)}]")
            print(f"  App：{current.app or '未指定'}")
            print(f"  目标：{current.goal}")
            print(f"{'─'*56}{C.RESET}")

            self._go_home()

            exec_goal = build_exec_goal(current)
            if current.context_needed:
                print(f"{C.DIM}  🔗  注入上下文：{current.context_needed}{C.RESET}")

            try:
                result = self.exec_agent.run(exec_goal)
                current.result = result
                # 只有真正完成才标 done；超步数为 timeout，异常为 failed
                current.status = getattr(self.exec_agent, "last_status", "done")
                if current.status == "done":
                    print(f"\n{C.GREEN}  ✅  子任务 [{current.index}] 完成{C.RESET}")
                else:
                    print(f"\n{C.AMBER}  ⚠️  子任务 [{current.index}] 未完成（{current.status}）{C.RESET}")
                print(f"{C.GREEN}     {result}{C.RESET}")
            except Exception as e:
                current.result = f"执行出错：{e}"
                current.status = "failed"
                print(f"\n{C.RED}  ❌  子任务 [{current.index}] 出错：{e}{C.RESET}")
                import traceback; traceback.print_exc()
            finally:
                # 无论成功失败都累计步数，供实验统计
                memory = getattr(self.exec_agent, "last_memory", None)
                self.total_steps += len(memory.steps) if memory else 0

            # 推送子任务结果
            self._emit('subtask_done', {
                'index':  current.index,
                'status': current.status,
                'result': current.result,
            })

            # ── 重规划 ─────────────────────────
            task_done, summary = self.planner.replan(plan, current)

            # 收集重规划变化并推送
            pending_goals = {
                t.index: t.goal
                for t in plan.subtasks if t.status == 'pending'
            }
            self._emit('replan', {
                'updated': [
                    {'index': idx, 'goal': goal}
                    for idx, goal in pending_goals.items()
                ],
                'added': []
            })

            if task_done:
                print(f"\n{C.GREEN}{C.BOLD}{'═'*56}")
                print(f"  🎉  整体任务已完成！")
                print(f"{'═'*56}{C.RESET}")
                print(f"\n{summary}")
                self._save_log(plan)
                return

        # 结束
        print(f"\n{C.CYAN}{'═'*56}{C.RESET}")
        if plan.is_all_done():
            print(f"{C.GREEN}  ✅  所有子任务执行完毕{C.RESET}")
        else:
            print(f"{C.AMBER}  ⚠️  部分子任务未完成{C.RESET}")
        print(f"{C.CYAN}{'═'*56}{C.RESET}")
        print(plan.summary())
        self._save_log(plan)

        # test.md：任一子任务失败/超步数即整体失败，不能让 runner 记成正常结束
        unfinished = [t for t in plan.subtasks if t.status in ("failed", "timeout")]
        if unfinished:
            detail = "；".join(f"[{t.index}] {t.goal} -> {t.status}" for t in unfinished)
            raise RuntimeError(f"{len(unfinished)} 个子任务未完成：{detail}")


# ══════════════════════════════════════════════
#  命令行入口
# ══════════════════════════════════════════════

if __name__ == "__main__":
    provider = config.ACTIVE_PROVIDER
    model    = config.PROVIDERS[provider]["model"]
    print(f"{C.CYAN}多应用手机智能体  [{provider}/{model}]{C.RESET}")
    print(f"{C.DIM}示例：打开微信阅读包航消息，然后用高德地图查询他提到的地点{C.RESET}")
    _sep()
    task = input(f"{C.BOLD}请输入任务：{C.RESET}").strip()
    if task:
        agent = MultiAppAgent()
        agent.run(task)