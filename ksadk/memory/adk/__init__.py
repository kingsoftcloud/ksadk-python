"""ADK 记忆体集成 - 短期记忆 + 长期记忆

提供与 Google ADK 原生接口兼容的记忆体管理能力。
通过环境变量配置，由 ADKRunner 自动注入。

使用示例:
    from ksadk.memory.adk import ShortTermMemory, LongTermMemory

    # 短期记忆 (会话管理)
    stm = ShortTermMemory(backend="local")

    # 长期记忆 (跨 session 检索)
    ltm = LongTermMemory(backend="local", app_name="my_app")

环境变量:
    # 短期记忆
    KSADK_STM_BACKEND=local          # local | sqlite | database
    KSADK_STM_DB_URL=                # 数据库 URL

    # 长期记忆
    KSADK_LTM_BACKEND=http           # local | http
    KSADK_LTM_HTTP_URL=              # 记忆服务 HTTP 地址
    KSADK_LTM_HTTP_TOKEN=            # 认证 Token
    KSADK_LTM_TOP_K=5                # 检索数量
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ksadk.memory.adk.long_term_memory import LongTermMemory
    from ksadk.memory.adk.short_term_memory import ShortTermMemory


# Lazy loading (避免在未安装 google-adk 时导入失败)
def __getattr__(name):
    if name == "ShortTermMemory":
        from ksadk.memory.adk.short_term_memory import ShortTermMemory
        return ShortTermMemory

    if name == "LongTermMemory":
        from ksadk.memory.adk.long_term_memory import LongTermMemory
        return LongTermMemory

    raise AttributeError(f"module 'ksadk.memory.adk' has no attribute '{name}'")


__all__ = ["ShortTermMemory", "LongTermMemory"]
