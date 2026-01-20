"""示例 Agent - 使用 Memory Manager 管理会话记忆

这个示例展示如何在 Agent 中使用 Memory Manager SDK 来管理：
- 短期记忆 (消息历史)
- 会话状态

运行方式:
    # 本地测试 (InMemory 后端)
    python examples/memory_agent/agent.py

    # 使用 Redis 后端
    KSADK_MEMORY_BACKEND=redis KSADK_MEMORY_URL=redis://localhost:6379 python examples/memory_agent/agent.py
"""

import os
import sys

# 确保可以导入 ksadk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from ksadk.memory import MemoryManager, get_memory_manager


def create_agent_with_memory():
    """创建一个使用 Memory Manager 的 Agent"""
    
    # 方式1: 自动从环境变量配置
    memory = get_memory_manager()
    
    # 方式2: 手动配置
    # memory = MemoryManager(backend="redis", url="redis://localhost:6379")
    
    return memory


def simulate_conversation(memory: MemoryManager, session_id: str):
    """模拟一次对话"""
    
    print(f"\n=== 会话 {session_id} ===\n")
    
    # 1. 检查是否有历史消息
    messages = memory.get_messages(session_id)
    if messages:
        print(f"📜 发现历史消息 ({len(messages)} 条):")
        for m in messages[-3:]:  # 只显示最近 3 条
            print(f"   {m['role']}: {m['content']}")
        print()
    
    # 2. 检查会话状态
    state = memory.get_state(session_id)
    turn_count = state.get("turn_count", 0)
    print(f"🔢 当前轮次: {turn_count}")
    
    # 3. 模拟用户输入
    user_input = f"这是第 {turn_count + 1} 轮对话"
    print(f"👤 用户: {user_input}")
    memory.add_message(session_id, "user", user_input)
    
    # 4. 模拟 Assistant 回复
    assistant_reply = f"好的，我记录了这是第 {turn_count + 1} 轮。"
    print(f"🤖 助手: {assistant_reply}")
    memory.add_message(session_id, "assistant", assistant_reply)
    
    # 5. 更新状态
    memory.update_state(session_id, {"turn_count": turn_count + 1})
    
    # 6. 保存一些自定义数据
    memory.set("last_topic", user_input, session_id=session_id)
    
    print(f"\n✅ 状态已更新: turn_count = {turn_count + 1}")
    print(f"📝 消息历史: {len(memory.get_messages(session_id))} 条")


def main():
    print("=" * 60)
    print("Memory Manager SDK - Client-Server 集成示例")
    print("=" * 60)
    
    # 显示当前配置
    backend = os.environ.get("KSADK_MEMORY_BACKEND", "memory")
    url = os.environ.get("KSADK_MEMORY_URL", "(none)")
    print(f"\n配置:")
    print(f"  后端: {backend}")
    print(f"  URL: {url}")
    
    # 创建 Agent
    memory = create_agent_with_memory()
    
    # 模拟多轮对话
    session_id = "test-session-001"
    
    for i in range(3):
        simulate_conversation(memory, session_id)
    
    # 最终状态
    print("\n" + "=" * 60)
    print("最终状态:")
    print(f"  会话状态: {memory.get_state(session_id)}")
    print(f"  消息历史: {len(memory.get_messages(session_id))} 条")
    print(f"  last_topic: {memory.get('last_topic', session_id=session_id)}")
    
    # 清理 (可选)
    # memory.clear_session(session_id)
    
    print("\n✅ 示例完成!")


if __name__ == "__main__":
    main()
