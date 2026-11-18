"""依赖校验与未完成判定的单元测试。"""

from task_planner import SubTask, TaskPlan, unfinished_subtasks


def plan_with(deps):
    return TaskPlan("测试", [SubTask(1, "第一步", app="设置", depends_on=deps)])


def test_unknown_dependency_is_dropped():
    plan = plan_with([99])

    assert plan.subtasks[0].depends_on == []
    assert plan.pending_tasks(), "依赖被过滤后子任务应可执行"
    assert any("99" in w for w in plan.warnings)


def test_self_dependency_is_dropped():
    plan = plan_with([1])

    assert plan.subtasks[0].depends_on == []
    assert plan.pending_tasks()


def test_cycle_is_broken():
    a = SubTask(1, "A", depends_on=[2])
    b = SubTask(2, "B", depends_on=[1])
    plan = TaskPlan("循环", [a, b])

    assert plan.pending_tasks(), "成环时至少要有一个子任务可执行"
    assert any("环形依赖" in w for w in plan.warnings)
    assert all(d != t.index for t in plan.subtasks for d in t.depends_on)


def test_unfinished_counts_pending_unless_explicitly_done():
    plan = TaskPlan("测试", [SubTask(1, "第一步", app="设置")])

    assert len(unfinished_subtasks(plan, task_done=False)) == 1
    assert unfinished_subtasks(plan, task_done=True) == []


def test_unfinished_keeps_failures_even_when_done():
    plan = TaskPlan("测试", [SubTask(1, "第一步", app="设置")])
    plan.subtasks[0].status = "timeout"

    assert len(unfinished_subtasks(plan, task_done=True)) == 1
