"""任务 App 绑定：防止周期性任务在上一个 App 里执行下一个任务。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app_launcher
import screen_agent
from app_launcher import AppLauncher, detect_task_app

WECHAT = "com.tencent.mm"
QQ = "com.tencent.mobileqq"
OUTLOOK = "com.microsoft.office.outlook"


# ---------------------------------------------------------------- 任务文本解析
def test_detect_task_app_reads_leading_app():
    assert detect_task_app("打开QQ，在消息列表里通过上下滚动查找「课程群」") == ("QQ", QQ)
    assert detect_task_app("打开微信，通过搜索框输入「课程群」") == ("微信", WECHAT)
    assert detect_task_app("打开Outlook，进入收件箱查找「DDL」") == ("Outlook", OUTLOOK)


def test_detect_task_app_prefers_longer_alias_and_ignores_unknown():
    # 「QQ音乐」不能被解析成 QQ，否则会把音乐 App 的任务放到 QQ 里跑
    assert detect_task_app("打开QQ音乐，播放歌单") == ("QQ音乐", "com.tencent.qqmusic")
    assert detect_task_app("打开设置，关闭蓝牙") is None


def test_resolve_package_accepts_alias_and_rejects_unknown_chinese():
    launcher = AppLauncher(phone_controller=None)

    assert launcher.resolve_package("QQ") == QQ
    assert launcher.resolve_package("qq") == QQ
    assert launcher.resolve_package(WECHAT) == WECHAT
    assert launcher.resolve_package("不存在的应用") is None


# ---------------------------------------------------------------- 前台校验
def _launcher_with(packages: list[str | None], launch_ok: bool = True):
    launcher = AppLauncher.__new__(AppLauncher)
    launcher.phone = None
    remaining = list(packages)
    launched: list[str] = []
    homes: list[bool] = []

    def _current():
        return remaining.pop(0) if remaining else (packages[-1] if packages else None)

    def _launch(package):
        launched.append(package)
        return launch_ok

    launcher.current_package = _current
    launcher.launch = _launch
    launcher.go_home = lambda: homes.append(True)
    return launcher, launched


def test_ensure_foreground_restart_goes_home_and_relaunches_same_app():
    # 同一个 App 的第二个来源：包名相同也必须回桌面重开，不能沿用上一个会话页
    launcher, launched = _launcher_with([WECHAT, WECHAT])
    launcher.go_home = lambda: launched.append("HOME")

    assert launcher.ensure_foreground(WECHAT, restart=True) is True
    assert launched == ["HOME", WECHAT]


def test_ensure_foreground_noop_when_target_already_active():
    launcher, launched = _launcher_with([WECHAT])

    assert launcher.ensure_foreground(WECHAT) is True
    assert launched == []


def test_ensure_foreground_relaunches_from_previous_app():
    # 周期性任务刚跑完 QQ，物理设备停在 QQ：必须先切到微信并确认前台
    launcher, launched = _launcher_with([QQ, WECHAT])

    assert launcher.ensure_foreground(WECHAT) is True
    assert launched == [WECHAT]


def test_ensure_foreground_fails_when_launch_fails():
    launcher, launched = _launcher_with([QQ], launch_ok=False)

    assert launcher.ensure_foreground(WECHAT) is False
    assert launched == [WECHAT]


def test_ensure_foreground_fails_when_foreground_never_matches():
    launcher, launched = _launcher_with([QQ], launch_ok=True)

    assert launcher.ensure_foreground(WECHAT, timeout=0) is False
    assert launched == [WECHAT]


# ---------------------------------------------------------------- ScreenAgent 绑定
def _agent(launcher) -> screen_agent.ScreenAgent:
    agent = object.__new__(screen_agent.ScreenAgent)
    agent.launcher = launcher
    agent.phone = SimpleNamespace()
    agent.read_only = True
    agent._task_app = None
    agent._task_app_label = ""
    agent._search_input_active = False
    agent._search_target = ""
    agent.last_status = ""
    agent.last_block_reason = ""
    return agent


class _FakeLauncher:
    def __init__(self, foreground: str | None, ensure_ok: bool = True):
        self.foreground = foreground
        self.ensure_ok = ensure_ok
        self.ensured: list[str] = []
        self.restarted: list[bool] = []

    def ensure_foreground(self, package, label="", timeout=8.0, restart=False):
        self.ensured.append(package)
        self.restarted.append(restart)
        return self.ensure_ok

    def current_package(self):
        return self.foreground


def test_bind_task_app_switches_to_wechat_before_running():
    launcher = _FakeLauncher(foreground=QQ)
    agent = _agent(launcher)

    agent._bind_task_app("打开微信，通过搜索框输入「课程群」")

    assert agent._task_app == WECHAT
    assert launcher.ensured == [WECHAT]
    # 每次绑定都强制干净启动，而不是判断「同包名已在前台」就跳过
    assert launcher.restarted == [True]


def test_bind_task_app_uses_explicit_subtask_app_when_goal_lacks_name():
    launcher = _FakeLauncher(foreground=QQ)
    agent = _agent(launcher)

    agent._bind_task_app("在课程群里向上翻阅消息", app_label="微信")

    assert agent._task_app == WECHAT
    assert launcher.ensured == [WECHAT]


def test_bind_task_app_stops_when_switch_impossible():
    launcher = _FakeLauncher(foreground=QQ, ensure_ok=False)
    agent = _agent(launcher)

    with pytest.raises(screen_agent.AppMismatchError) as excinfo:
        agent._bind_task_app("打开微信，通过搜索框输入「课程群」")

    message = str(excinfo.value)
    assert "微信" in message
    assert QQ in message
    assert agent.last_status == "failed"
    assert agent.last_block_reason == message


def test_bind_task_app_skips_when_task_has_no_known_app():
    launcher = _FakeLauncher(foreground=QQ)
    agent = _agent(launcher)

    agent._bind_task_app("打开设置，关闭蓝牙")

    assert agent._task_app is None
    assert launcher.ensured == []


def test_model_open_app_to_wrong_app_is_blocked():
    launcher = _FakeLauncher(foreground=QQ)
    launcher.launch = lambda name: True
    launcher.resolve_package = lambda name: QQ
    agent = _agent(launcher)
    agent._task_app = WECHAT
    agent._task_app_label = "微信"

    with pytest.raises(screen_agent.AppMismatchError, match="不一致"):
        agent._execute({"action_type": "open_app", "app_name": "QQ"})


def test_model_open_app_failure_stops_instead_of_continuing():
    launcher = _FakeLauncher(foreground=QQ, ensure_ok=False)
    launcher.resolve_package = lambda name: WECHAT
    agent = _agent(launcher)

    with pytest.raises(screen_agent.AppMismatchError, match="无法打开"):
        agent._execute({"action_type": "open_app", "app_name": "微信"})


# ---------------------------------------------------------------- 跨任务状态隔离
def test_reset_task_state_clears_previous_rule_state():
    agent = _agent(_FakeLauncher(foreground=QQ))
    agent._rule_state = {"entered_group": "2026 计算机班级群", "search_stage": "result"}
    agent._ocr_nav_state = {"scrolls": 3}
    agent._search_input_active = True
    agent._search_target = "课程群"

    agent._reset_task_state()

    assert agent._rule_state == {}
    assert agent._ocr_nav_state == {}
    assert agent._search_input_active is False
    assert agent._search_target == ""


def test_subtask_run_resets_state_and_binds_declared_app(tmp_path, monkeypatch):
    """多 App 子任务：上一个子任务的规则状态不能串进来，且必须切到本子任务 App。"""
    import multi_agent

    def _run_dir(name: str):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(multi_agent, "make_run_dir", _run_dir)
    agent = object.__new__(multi_agent.SubTaskScreenAgent)
    agent.max_steps = 2
    agent.read_only = True
    agent.launcher = _FakeLauncher(foreground=QQ)
    agent.phone = SimpleNamespace()
    agent.last_status = ""
    agent._sub_index = 0
    agent._rule_state = {"entered_group": "上一个子任务的群"}
    agent._execute_step = lambda step, memory: (True, True, "完成")
    agent.bind_app("微信")

    summary = agent.run("在课程群里向上翻阅消息")

    assert summary == "完成"
    assert agent._rule_state == {}
    assert agent.launcher.ensured == [WECHAT]
    assert agent.launcher.restarted == [True]


def test_alias_table_covers_monitor_apps():
    for label, package in (("微信", WECHAT), ("QQ", QQ), ("Outlook", OUTLOOK)):
        assert AppLauncher.KNOWN_APPS[label] == package
        assert app_launcher.detect_task_app(f"打开{label}") == (label, package)
