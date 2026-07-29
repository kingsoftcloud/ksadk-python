"""开关与灰度配置(v2.5):默认关闭,按需启用,一键回退直连。

review v1 要求:全局默认关,支持按 agent/按模型白名单开启,故障一键回退。
设计为纯配置判定(env/agentengine.yaml 解析后传入),不含网络/proxy 生命周期——
后者由 v2.1(codex)/v2.2(env 框架)各自接线时调用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ProxyGate:
    """协议转换层的开关/灰度判定(纯函数,不持有运行时状态)。

    - ``enabled``:全局开关,默认 False。
    - ``agent_allowlist``/``model_allowlist``:细粒度白名单;空=不按此维度放行。
    - ``denylist``:显式拒绝(优先级最高),用于故障一键回退某 agent/模型。

    判定优先级:denylist > 全局 enabled > 白名单。
    """

    enabled: bool = False
    agent_allowlist: set[str] = field(default_factory=set)
    model_allowlist: set[str] = field(default_factory=set)
    denylist: set[str] = field(default_factory=set)

    def is_on(self, agent: str | None = None, model: str | None = None) -> bool:
        """是否对该 (agent, model) 启用转换层。

        - denylist 命中(agent 或 model)→ False(一键回退)
        - 全局 enabled=False → False(默认关)
        - 全局 enabled=True → True(除非 denylist)
        - 全局关但白名单命中(agent 或 model)→ True(灰度放行)
        """
        if agent and agent in self.denylist:
            return False
        if model and model in self.denylist:
            return False
        if self.enabled:
            return True
        if agent and agent in self.agent_allowlist:
            return True
        if model and model in self.model_allowlist:
            return True
        return False

    @classmethod
    def from_env(cls) -> "ProxyGate":
        """从 env 读开关与白名单(逗号分隔)。

        - ``KSADK_MODEL_PROXY_ENABLED=1`` 全局开
        - ``KSADK_MODEL_PROXY_AGENTS=a,b`` agent 白名单
        - ``KSADK_MODEL_PROXY_MODELS=glm-5.2`` model 白名单
        - ``KSADK_MODEL_PROXY_DENY=c`` 一键回退
        """
        def _split(name: str) -> set[str]:
            v = os.environ.get(name, "").strip()
            return {x.strip() for x in v.split(",") if x.strip()} if v else set()

        return cls(
            enabled=os.environ.get("KSADK_MODEL_PROXY_ENABLED") == "1",
            agent_allowlist=_split("KSADK_MODEL_PROXY_AGENTS"),
            model_allowlist=_split("KSADK_MODEL_PROXY_MODELS"),
            denylist=_split("KSADK_MODEL_PROXY_DENY"),
        )
