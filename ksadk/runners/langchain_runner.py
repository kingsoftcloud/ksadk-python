"""
LangChainRunner - LangChain 框架运行时

支持:
- Langfuse Tracing (via CallbackHandler)
- OpenTelemetry Tracing
"""

import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from ksadk.runners.base_runner import BaseRunner
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


def _get_langfuse_callback():
    """获取 Langfuse CallbackHandler
    
    Returns:
        CallbackHandler 实例，未配置时返回 None
    """
    # 检查是否配置了 Langfuse
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    if not public_key:
        return None
    
    try:
        from langfuse.langchain import CallbackHandler
        
        # CallbackHandler 会自动从环境变量读取配置
        handler = CallbackHandler()
        
        import logging
        logging.getLogger(__name__).info(f"Langfuse CallbackHandler initialized (host: {os.getenv('LANGFUSE_BASE_URL', 'default')})")
        
        return handler
    except ImportError as e:
        import logging
        logging.getLogger(__name__).warning(f"Langfuse not installed: {e}")
        return None
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to create Langfuse CallbackHandler: {e}")
        return None


def _get_langfuse_metadata(session_id: str = None) -> dict:
    """获取 Langfuse 的 metadata 字典
    
    根据 Langfuse 官方文档，通过 metadata 字段传递 trace 属性:
    - langfuse_user_id
    - langfuse_session_id  
    - langfuse_tags
    
    Args:
        session_id: 会话 ID (可选)
    
    Returns:
        包含 Langfuse 属性的 metadata 字典
    """
    metadata = {}
    
    # 设置 session_id
    if session_id:
        metadata["langfuse_session_id"] = session_id
    
    # 从 ksadk settings 获取 agent 元数据 (作为 fallback)
    try:
        from ksadk.configs import settings
        agent_config = settings.agent
        
        # 设置 user_id
        if agent_config.user_id:
            metadata["langfuse_user_id"] = agent_config.user_id
        
        # 设置 session_id (如果未传入，使用环境变量)
        if not session_id and agent_config.session_id:
            metadata["langfuse_session_id"] = agent_config.session_id
        
        # 设置 tags
        tags = list(agent_config.tags or [])
        if agent_config.environment and agent_config.environment not in tags:
            tags.append(agent_config.environment)
        if agent_config.agent_name and agent_config.agent_name not in tags:
            tags.append(agent_config.agent_name)
        if tags:
            metadata["langfuse_tags"] = tags
            
    except ImportError:
        pass
    except Exception:
        pass
    
    return metadata


def _get_langfuse_metadata(session_id: str = None) -> dict:
    """获取 Langfuse 的 metadata 字典 (Legacy support)"""
    metadata = {}
    if session_id:
        metadata["langfuse_session_id"] = session_id
    return metadata


class LangChainRunner(BaseRunner):
    """LangChain 框架运行时"""
    
    def load_agent(self) -> None:
        """加载 LangChain Agent/Chain"""
        if self.project_dir not in sys.path:
            sys.path.insert(0, self.project_dir)
        
        package_path = Path(self.detection_result.package_path)
        package_name = package_path.name
        
        try:
            module = __import__(package_name, fromlist=[self.detection_result.agent_variable])
            self._agent = getattr(module, self.detection_result.agent_variable)
        except ImportError as e:
            raise ImportError(f"无法导入模块 {package_name}: {e}")
        except AttributeError:
            raise AttributeError(f"模块 {package_name} 中未找到 {self.detection_result.agent_variable}")
    
    def _prepare_trace_metadata(self, session_id: str):
        """准备 Trace 元数据 (Tags, UserID, etc.)"""
        user_id = None
        tags = []
        version = None
        agent_name = None
        
        try:
            from ksadk.configs import settings
            agent_config = settings.agent
            
            user_id = agent_config.user_id
            version = agent_config.version
            tags = list(agent_config.tags or [])
            
            # Add Environment
            if agent_config.environment and agent_config.environment not in tags:
                tags.append(agent_config.environment)
            
            # Add Region (Kingsoft Cloud)
            if settings.cloud.region and settings.cloud.region not in tags:
                tags.append(settings.cloud.region)
                
            # Add Model Name
            if settings.model.model_name and settings.model.model_name not in tags:
                tags.append(settings.model.model_name)
            
            # Add Agent Name (Configured -> Fallback)
            agent_name = agent_config.agent_name
            if not agent_name and hasattr(self, "detection_result"):
                 try:
                     # Fallback to package name
                     agent_name = Path(self.detection_result.package_path).name
                 except Exception:
                     pass
            
            if agent_name and agent_name not in tags:
                tags.append(agent_name)
                
            # Add Agent ID
            if agent_config.agent_id and agent_config.agent_id not in tags:
                tags.append(agent_config.agent_id)
                
            # Add Tenant ID (Account ID)
            if agent_config.tenant_id and agent_config.tenant_id not in tags:
                tags.append(agent_config.tenant_id)
                
        except ImportError:
            pass
        except Exception:
            pass
            
        return user_id, tags, version, agent_name

    async def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        session_id = input_data.get("session_id") or invocation_id
        
        # 1. 准备 Metadata (提前以此获取 Agent Name)
        user_id, tags, version, agent_name = self._prepare_trace_metadata(session_id)
        trace_name = agent_name or "langchain.invoke"
        
        with tracer.start_as_current_span(trace_name) as span:
            span.set_attribute("user.input", user_input[:200])
            
            # 2. 更新 OTel Span Attributes (兼容 OTel exporter)
            span.set_attribute("langfuse.session_id", session_id)
            if user_id:
                span.set_attribute("langfuse.user_id", user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))
            
            # 3. 配置 Langfuse CallbackHandler (通过 metadata)
            langfuse_cb = _get_langfuse_callback()
            if langfuse_cb:
                # 合并 metadata
                langfuse_metadata = _get_langfuse_metadata(session_id=session_id)
                
                # 如果有从 _prepare_trace_metadata 获取的更准确的 tags/user_id，覆盖之
                if user_id:
                    langfuse_metadata["langfuse_user_id"] = user_id
                if tags:
                    langfuse_metadata["langfuse_tags"] = tags
                
                config = {
                    "callbacks": [langfuse_cb],
                    "metadata": langfuse_metadata 
                }
            else:
                config = None
            
            # 支持多种调用方式
            if hasattr(self._agent, 'ainvoke'):
                result = await self._agent.ainvoke({"input": user_input}, config=config)
                if isinstance(result, dict):
                    output = result.get("output", result.get("text", str(result)))
                else:
                    output = str(result)
            elif hasattr(self._agent, 'invoke'):
                result = self._agent.invoke({"input": user_input}, config=config)
                if isinstance(result, dict):
                    output = result.get("output", result.get("text", str(result)))
                else:
                    output = str(result)
            elif callable(self._agent):
                result = self._agent(user_input)
                output = str(result)
            else:
                raise TypeError("Agent 不支持 invoke 调用")
            
            span.set_attribute("agent.output", output[:500] if output else "")
            return {"output": output}
    
    async def stream(self, input_data: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """流式调用 LangChain Agent/Chain"""
        user_input = input_data.get("input", "")
        invocation_id = str(uuid.uuid4()).replace("-", "")
        
        session_id = input_data.get("session_id") or invocation_id
        
        # 1. 准备 Metadata (提前以此获取 Agent Name)
        user_id, tags, version, agent_name = self._prepare_trace_metadata(session_id)
        trace_name = agent_name or "langchain.stream"
        
        with tracer.start_as_current_span(trace_name) as span:
            span.set_attribute("user.input", user_input[:200])
            
            # 2. 更新 OTel Span Attributes (兼容 OTel exporter)
            span.set_attribute("langfuse.session_id", session_id)
            if user_id:
                span.set_attribute("langfuse.user_id", user_id)
            if tags:
                span.set_attribute("langfuse.tags", ",".join(tags))
            
            # 3. 配置 Langfuse CallbackHandler (通过 metadata)
            langfuse_cb = _get_langfuse_callback()
            if langfuse_cb:
                # 合并 metadata
                langfuse_metadata = _get_langfuse_metadata(session_id=session_id)
                
                # 如果有从 _prepare_trace_metadata 获取的更准确的 tags/user_id，覆盖之
                if user_id:
                    langfuse_metadata["langfuse_user_id"] = user_id
                if tags:
                    langfuse_metadata["langfuse_tags"] = tags
                
                config = {
                    "callbacks": [langfuse_cb],
                    "metadata": langfuse_metadata 
                }
            else:
                config = None
            
            accumulated_text = ""
            
            # 尝试流式调用
            if hasattr(self._agent, 'astream'):
                async for chunk in self._agent.astream({"input": user_input}, config=config):
                    if isinstance(chunk, dict):
                        if "output" in chunk:
                            accumulated_text += chunk["output"]
                            yield {"delta": chunk["output"], "type": "text"}
                        elif "text" in chunk:
                            accumulated_text += chunk["text"]
                            yield {"delta": chunk["text"], "type": "text"}
                    else:
                        accumulated_text += str(chunk)
                        yield {"delta": str(chunk), "type": "text"}
            elif hasattr(self._agent, 'stream'):
                for chunk in self._agent.stream({"input": user_input}, config=config):
                    if isinstance(chunk, dict):
                        if "output" in chunk:
                            accumulated_text += chunk["output"]
                            yield {"delta": chunk["output"], "type": "text"}
                    else:
                        accumulated_text += str(chunk)
                        yield {"delta": str(chunk), "type": "text"}
            else:
                # 回退到同步调用
                result = await self.invoke(input_data)
                accumulated_text = result.get("output", "")
                yield {"output": accumulated_text, "type": "final"}
            
            span.set_attribute("agent.output", accumulated_text[:500])

