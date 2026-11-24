"""手机 GUI 数据源：微信 / QQ / Outlook App，按目标名称定位会话。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..connectors.base import SourceError
from ..connectors.mobile_reading import build_mobile_messages
from ..models import RawMessage, RetrievalPlan
from ..settings import TZ
from .config import APP_LABELS, MonitorConfig, MonitorSource, source_display_name

_NOT_FOUND_STATUS = {"not_found", "target_not_found", "missing_target"}


class TargetNotFound(SourceError):
    """App 内找不到用户指定的群/联系人/邮箱文件夹。"""


class MobileAppSource:
    """复用现有手机智能体读屏；与 MobileChatConnector 共用解析与 timeout 语义。

    真机验证范围见 ``docs/real-device-test-report.md``：单机、自聊会话、只读。
    """

    def __init__(self, source: MonitorSource, agent: Any | None = None,
                 agent_factory: Any | None = None, use_device_lock: bool = True,
                 now: datetime | None = None):
        self.source = source
        self.name = source.id
        self.source_type = source.source_type
        self.agent = agent
        self.agent_factory = agent_factory
        self.use_device_lock = use_device_lock
        self.now = now
        self.partial = False
        self.last_status: str | None = None
        self.last_fetch_meta: dict[str, Any] = {}
        self.ambiguous: dict[str, Any] | None = None

    def fetch(self, plan: RetrievalPlan | None = None) -> list[RawMessage]:
        self.partial = False
        self.last_status = None
        self.last_fetch_meta = {}
        self.ambiguous = None
        agent = self._get_agent()
        # 生产读屏路径强制只读：即使模型给出 send/type/press_enter 也不会执行
        agent.read_only = True
        # 微信真机不暴露控件树：直接跳过 dump，省掉失败重试与清理开销
        agent.tree_unsupported = self.source.app == "wechat"
        task = self._build_task()

        if self.use_device_lock:
            try:
                from device_lock import device_lock
            except ImportError as exc:
                raise SourceError(f"设备锁不可用：{exc}") from exc
            with device_lock(timeout=30) as acquired:
                if not acquired:
                    raise SourceError(
                        "设备锁被占用，本轮未读取手机；"
                        "请关闭其他扫描窗口（Web / 命令行）或等待其结束后重试"
                    )
                agent.run(task)
        else:
            agent.run(task)

        status = str(getattr(agent, "last_status", "") or "").lower()
        self.last_status = status
        ambiguous = getattr(agent, "last_ambiguous", None)
        if status == "ambiguous" or ambiguous:
            # 群名歧义：不猜、不读消息、也不当失败；由上层把候选汇总到 /ddl 页面
            self.partial = True
            self.ambiguous = dict(ambiguous or {})
            self.ambiguous.setdefault("target", self.source.target)
            self.last_fetch_meta = {
                "partial": True,
                "last_status": "ambiguous",
                "ambiguous": self.ambiguous,
                "message_count": 0,
            }
            return []
        if status == "failed":
            raise SourceError(f"手机读取失败（status={status}）")
        if status in _NOT_FOUND_STATUS or self._mentions_not_found(agent):
            raise TargetNotFound(
                f"未找到目标「{self.source.target}」（{APP_LABELS.get(self.source.app, self.source.app)}）"
            )
        if status == "timeout":
            # 到达步数上限不等于没读到：返回已归档观察，由上层标记为部分结果
            self.partial = True

        messages = self._messages_from_steps(agent)
        self.last_fetch_meta = {
            "partial": self.partial,
            "last_status": status,
            "message_count": len(messages),
        }
        return messages

    # ------------------------------------------------------------------ #
    def _build_task(self) -> str:
        app = APP_LABELS.get(self.source.app, self.source.app)
        kind = {"group": "群聊", "private": "联系人", "inbox": "收件箱"}.get(
            self.source.kind, "会话"
        )
        if self.source.app == "qq":
            list_hint = "消息列表或“联系人 > 群聊”" if kind == "群聊" else "消息列表"
            return (
                f"打开{app}。先看首屏{list_hint}里有没有「{self.source.target}」："
                f"有就直接点它进入{kind}；没有就用顶部搜索框输入「{self.source.target}」，"
                "在搜索结果里只点名称逐字符完全一致的那一个。"
                "只有搜索不可用或没有结果时，才上下滚动列表兜底；"
                "滚动过程中绝不点开任何不是该名称的会话，尤其不要点名字相似、"
                f"只有大小写不同或带后缀的群。仍然找不到就明确报告“未找到”。\n"
                f"进入后向上翻阅最近 {self.source.lookback_days} 天的消息，逐条读出所有包含截止时间"
                "（deadline / due / DDL / 截止 / 提交）的消息原文、发送者和时间。\n"
                "只记录属于该会话的消息，不要猜测，不要修改任何内容。"
            )
        return (
            f"打开{app}。先看首屏列表里有没有「{self.source.target}」：有就直接点它进入{kind}；"
            f"没有就用搜索框输入「{self.source.target}」，在结果里只点名称逐字符完全一致的那一个。"
            "只有搜索不可用或没有结果时，才上下滚动列表兜底；"
            "滚动过程中绝不点开任何不是该名称的会话，尤其不要点名字相似、"
            "只有大小写不同或带后缀的群。仍然找不到就明确报告“未找到”。\n"
            f"进入后向上翻阅最近 {self.source.lookback_days} 天的消息，逐条读出所有包含截止时间"
            "（deadline / due / DDL / 截止 / 提交）的消息原文、发送者和时间。\n"
            "只记录属于该会话的消息，不要猜测，不要修改任何内容。"
        )

    def _get_agent(self):
        if self.agent is not None:
            return self.agent
        if self.agent_factory is not None:
            self.agent = self.agent_factory()
            return self.agent
        try:
            from screen_agent import ScreenAgent
        except ImportError as exc:
            raise SourceError(f"手机自动化层不可用：{exc}") from exc
        self.agent = ScreenAgent(read_only=True)
        return self.agent

    def _messages_from_steps(self, agent) -> list[RawMessage]:
        return build_mobile_messages(
            agent,
            # 只用 target 会让「微信 / 课程群」和「QQ / 课程群」在冲突裁决里
            # 被当成同一来源；展示名带上 App 才能区分。
            source_name=source_display_name(self.source),
            source_type=self.source.source_type,
            subject=f"{APP_LABELS.get(self.source.app, self.source.app)} / {self.source.target}",
            thread_id=self.source.id,
            now=self.now or datetime.now(TZ),
        )

    @staticmethod
    def _mentions_not_found(agent) -> bool:
        memory = getattr(agent, "last_memory", None)
        steps = list(getattr(memory, "steps", []) or [])
        text = " ".join(str(step.get("observation") or "") for step in steps[-2:])
        return "未找到" in text or "not found" in text.lower()


def build_sources(config: MonitorConfig, agent_factory: Any | None = None,
                  use_device_lock: bool = True) -> list[MobileAppSource]:
    return [
        MobileAppSource(source, agent_factory=agent_factory, use_device_lock=use_device_lock)
        for source in config.sources
        if source.enabled
    ]
