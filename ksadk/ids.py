"""统一 id 铸造规范。

历史上 session / run(invocation) id 各层格式混杂：

- agentengine-server hosted 会话: ``sess-<16 hex>``（conversation_service）
- ksadk 本地 session: 裸 ``<16 hex>``（sessions/base.generate_id）
- ksadk 后台 run: ``inv_<32 hex>``（server/app RunAgent）
- ksadk conversation turn: 裸 ``uuid4``（conversations/runtime）
- research-ui run_agent 模式: ``run_<session段>``（由 session id 推导）

新代码统一走这里：

- session id: ``sess-<16 hex>``，与 agentengine-server 对齐。
- run/invocation id: ``run_<32 hex>``，每次 run 唯一且固定长度。

id 均为不透明字符串，仅在铸造时保证格式，消费方不做结构解析。
"""

from __future__ import annotations

import uuid


def new_session_id() -> str:
    """铸造 session id：``sess-<16 hex>``。"""
    return f"sess-{uuid.uuid4().hex[:16]}"


def new_run_id(session_id: str | None = None) -> str:
    """铸造固定长度 opaque run/invocation id：``run_<32 hex>``。"""
    del session_id
    return f"run_{uuid.uuid4().hex}"
