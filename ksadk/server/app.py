# ksadk/server/app.py
"""
FastAPI 应用 - 提供 HTTP API 接口 (ADK Web 兼容)
"""

import json
import logging
import uuid
import time
import asyncio
from typing import Dict, Any, List, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest, LlmResponse, GenAiContent, Part
from ksadk.tracing import get_memory_exporter
from ksadk.server.session_store import get_session_store, Session

logger = logging.getLogger(__name__)

app = FastAPI(title="KsADK API Server")

# Allow CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Runner instance
runner: BaseRunner = None

def set_runner(r: BaseRunner):
    global runner
    runner = r

# ============================================================
# Core ADK API Endpoints
# ============================================================

@app.get("/health")
async def health_check():
    framework = "unknown"
    agent_name = "unknown"
    if runner and hasattr(runner, 'detection_result'):
        framework = runner.detection_result.type.value  # langgraph, langchain, adk
        agent_name = runner.detection_result.name
    return {"status": "ok", "framework": framework, "agent": agent_name}


@app.get("/list-apps")
async def list_apps(relative_path: str = "./"):
    """Return available apps. For KsADK single-agent mode, returns the current agent."""
    name = runner.detection_result.name if runner else "default_agent"
    return [name]


# ============================================================
# Session Management API (ADK Web Compatible)
# ============================================================

@app.post("/apps/{app_name}/users/{user_id}/sessions")
async def create_session(app_name: str, user_id: str, request: Request):
    """Create a new session"""
    store = get_session_store()
    
    # Check if importing existing events
    body = {}
    try:
        body = await request.json()
    except:
        pass
    
    events = body.get("events", [])
    session = store.create_session(app_name, user_id, events)
    return session.to_dict()


@app.get("/apps/{app_name}/users/{user_id}/sessions")
async def list_sessions(app_name: str, user_id: str):
    """List all sessions for a user"""
    store = get_session_store()
    sessions = store.list_sessions(app_name, user_id)
    return [s.to_dict() for s in sessions]


@app.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def get_session(app_name: str, user_id: str, session_id: str):
    """Get a specific session with its events"""
    store = get_session_store()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@app.delete("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def delete_session(app_name: str, user_id: str, session_id: str):
    """Delete a session"""
    store = get_session_store()
    if store.delete_session(session_id):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Session not found")


# ============================================================
# Memory API - Save session to long-term memory
# ============================================================

@app.post("/apps/{app_name}/users/{user_id}/sessions/{session_id}/save_memory")
async def save_session_to_memory(app_name: str, user_id: str, session_id: str):
    """将指定 session 保存到长期记忆

    当配置了 KSADK_LTM_BACKEND 时，将 session 中的用户消息
    持久化到长期记忆后端，供后续 session 通过 load_memory 工具检索。
    """
    if not runner:
        raise HTTPException(status_code=500, detail="Runner not initialized")

    # 检查 runner 是否支持长期记忆
    from ksadk.runners.adk_runner import ADKRunner as _ADKRunner
    if not isinstance(runner, _ADKRunner):
        raise HTTPException(
            status_code=400,
            detail="Long-term memory is only supported with ADK runner"
        )

    if not runner._long_term_memory:
        raise HTTPException(
            status_code=400,
            detail="Long-term memory not configured. "
                   "Set KSADK_LTM_BACKEND environment variable."
        )

    # 查找 ADK 内部 session ID
    internal_session_id = runner._session_map.get(session_id, session_id)

    success = await runner.save_session_to_long_term_memory(
        session_id=internal_session_id,
        user_id=user_id,
    )

    if success:
        return {"status": "saved", "session_id": session_id}
    else:
        raise HTTPException(
            status_code=500,
            detail="Failed to save session to long-term memory"
        )


# ============================================================
# Run SSE - Core Agent Execution Endpoint
# ============================================================

@app.post("/run_sse")
async def run_sse(request: AgentRunRequest):
    """Unified Streaming Endpoint compatible with ADK Web
    
    Respects the `streaming` parameter:
    - streaming=False: Accumulate full response, send as single event
    - streaming=True: Stream tokens as they arrive (real-time)
    """
    if not runner:
        raise HTTPException(status_code=500, detail="Runner not initialized")

    store = get_session_store()
    
    # Ensure session exists
    session_id = request.sessionId
    if not session_id:
        session = store.create_session(request.appName, request.userId)
        session_id = session.id
    else:
        session = store.get_session(session_id)
        if not session:
            session = store.create_session(request.appName, request.userId)
            session_id = session.id
    
    # Extract user input
    user_input = ""
    if request.newMessage and request.newMessage.parts:
        for part in request.newMessage.parts:
            if part.text:
                user_input += part.text
    
    # Generate invocation ID for this run
    invocation_id = request.invocationId or str(uuid.uuid4())
    
    # Store user message event
    user_event = {
        "id": str(uuid.uuid4()),
        "author": "user",
        "invocationId": invocation_id,
        "content": {
            "role": "user",
            "parts": [{"text": user_input}]
        },
        "timestamp": int(time.time() * 1000)
    }
    store.add_event(session_id, user_event)
    
    # Common metadata for responses
    common_metadata = {
        "modelVersion": "models/gemini-pro" if "gemini" in request.appName.lower() else "models/unknown",
        "usageMetadata": {
            "promptTokenCount": len(user_input), # Approximate
            "candidatesTokenCount": 0,
            "totalTokenCount": len(user_input)
        }
    }
    
    # Build history from session events (for LangGraph memory)
    history = []
    for event in session.events:
        content = event.get("content", {})
        role = content.get("role", "")
        parts = content.get("parts", [])
        text = ""
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                text += part["text"]
        if text and role in ("user", "model"):
            history.append({"role": role, "content": text})

    # Determine streaming mode from request
    use_streaming = request.streaming

    async def event_generator():
        """Generate SSE events for agent response"""
        agent_name = runner.detection_result.name if runner else "agent"
        
        if not use_streaming:
            # ================ NON-STREAMING MODE ================
            # Accumulate complete response, then send as single event
            try:
                result = await runner.invoke({"input": user_input, "history": history})
                final_text = result.get("output", "")
                
                # Create single response event with complete text
                response_event = {
                    "id": str(uuid.uuid4()),
                    "author": agent_name,
                    "invocationId": invocation_id,
                    "content": {
                        "role": "model",
                        "parts": [{"text": final_text}]
                    },
                    "actions": {"finishReason": "STOP"},
                    "modelVersion": common_metadata["modelVersion"],
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": len(final_text),
                        "totalTokenCount": len(user_input) + len(final_text)
                    },
                    "timestamp": int(time.time() * 1000)
                }
                yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                
                # Store assistant event
                store.add_event(session_id, response_event)
                
            except Exception as e:
                logger.error(f"Error in invoke: {e}")
                error_event = {
                    "id": str(uuid.uuid4()),
                    "invocationId": invocation_id,
                    "error": str(e),
                    "errorMessage": str(e),
                    "timestamp": int(time.time() * 1000)
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        else:
            # ================ STREAMING MODE ================
            # Stream tokens as they arrive, then send final accumulated event
            accumulated_text = ""
            
            try:
                input_data = {"input": user_input, "history": history}
                async for chunk in runner.stream(input_data):
                    event_id = str(uuid.uuid4())
                    
                    if chunk.get("type") == "text":
                        delta_text = chunk.get("delta", "")
                        accumulated_text += delta_text
                        
                        # Send streaming chunk - ADK-Web expects partial content
                        response_event = {
                            "id": event_id,
                            "author": chunk.get("node", agent_name),
                            "invocationId": invocation_id,
                            "content": {
                                "role": "model",
                                "parts": [{"text": delta_text}]
                            },
                            # Mark as partial response
                            "partial": True,
                            "timestamp": int(time.time() * 1000)
                        }
                        yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                        
                    elif chunk.get("type") == "tool_call":
                        tool_event = {
                            "id": event_id,
                            "author": chunk.get("node", "tool"),
                            "invocationId": invocation_id,
                            "content": {
                                "role": "model",
                                "parts": [{
                                    "functionCall": {
                                        "name": chunk.get("tool_name", "unknown"),
                                        "args": chunk.get("tool_args", {})
                                    }
                                }]
                            },
                             # Add required ADK fields for tool events
                            "actions": {
                                "finishReason": "STOP",
                                "stateDelta": {} 
                            },
                            "modelVersion": common_metadata["modelVersion"],
                            "timestamp": int(time.time() * 1000)
                        }
                        yield f"data: {json.dumps(tool_event, ensure_ascii=False)}\n\n"
                        # Persist tool event
                        store.add_event(session_id, tool_event)
                        
                    elif chunk.get("type") == "final":
                        final_text = chunk.get("output", "")
                        accumulated_text = final_text
                
                # Send final complete event (for proper trace id)
                if accumulated_text:
                    final_event = {
                        "id": str(uuid.uuid4()),
                        "author": agent_name,
                        "invocationId": invocation_id,
                        "content": {
                            "role": "model",
                            "parts": [{"text": accumulated_text}]
                        },
                        "actions": {"finishReason": "STOP"},
                        "modelVersion": common_metadata["modelVersion"],
                        "usageMetadata": {
                            "promptTokenCount": len(user_input),
                            "candidatesTokenCount": len(accumulated_text),
                            "totalTokenCount": len(user_input) + len(accumulated_text)
                        },
                        "timestamp": int(time.time() * 1000)
                    }
                    # Don't yield final again for streaming (already sent tokens)
                    # Just store for session history
                    store.add_event(session_id, final_event)
                    
            except Exception as e:
                logger.error(f"Error in stream: {e}")
                error_event = {
                    "id": str(uuid.uuid4()),
                    "invocationId": invocation_id,
                    "error": str(e),
                    "errorMessage": str(e),
                    "timestamp": int(time.time() * 1000)
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Trace / Debug API (ADK Web Compatible)
# ============================================================

@app.get("/debug/trace/session/{session_id}")
async def get_session_trace(session_id: str):
    """Get traces for a session - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []  # Return empty array, not object
    
    # Get all spans and transform to ADK-Web expected format
    raw_spans = exporter.get_finished_spans()
    
    # Get session events for invocation mapping
    store = get_session_store()
    session = store.get_session(session_id)
    
    # Build invocation ID mapping from session events
    invocation_ids = {}
    if session:
        for event in session.events:
            event_id = event.get("id")
            invoc_id = event.get("invocationId")
            if event_id and invoc_id:
                invocation_ids[event_id] = invoc_id
    
    # Transform spans to ADK-Web format
    spans = []
    for span in raw_spans:
        # Use session_id as trace_id for grouping
        trace_id = span.get("trace_id", session_id)
        
        # Get or create invocation_id
        invocation_id = span.get("attributes", {}).get("gcp.vertex.agent.invocation_id")
        if not invocation_id:
            # Try to derive from event association
            invocation_id = trace_id[:36] if len(trace_id) >= 36 else trace_id
        
        # Build attributes with required ADK fields
        attrs = span.get("attributes", {}).copy()
        attrs["gcp.vertex.agent.invocation_id"] = invocation_id
        
        # If this is a LLM span, add request/response
        if "llm" in span.get("name", "").lower() or "invoke" in span.get("name", "").lower():
            if "user.input" in attrs:
                attrs["gcp.vertex.agent.llm_request"] = json.dumps({
                    "contents": [{"role": "user", "parts": [{"text": attrs.get("user.input", "")}]}]
                })
            if "agent.output" in attrs:
                attrs["gcp.vertex.agent.llm_response"] = json.dumps({
                    "candidates": [{"content": {"role": "model", "parts": [{"text": attrs.get("agent.output", "")}]}}]
                })
        
        formatted_span = {
            "trace_id": trace_id,
            "span_id": span.get("span_id", str(uuid.uuid4())[:16]),
            "parent_span_id": span.get("parent_span_id"),
            "name": span.get("name", "unknown"),
            "start_time": span.get("start_time", 0),
            "end_time": span.get("end_time", 0),
            "attributes": attrs,
            "status": span.get("status", {})
        }
        spans.append(formatted_span)
    
    return spans  # Return array directly


@app.get("/debug/trace/{event_id}")
async def get_event_trace(event_id: str):
    """Get trace for a specific event - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []
    
    spans = exporter.get_finished_spans()
    # Filter by event_id or return recent spans
    filtered = [s for s in spans if s.get("attributes", {}).get("event_id") == event_id]
    return filtered if filtered else spans[-10:]


@app.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}/events/{event_id}/graph")
async def get_event_graph(app_name: str, user_id: str, session_id: str, event_id: str):
    """Get event graph (DOT format) - placeholder"""
    return {"dotSrc": None}


# ============================================================
# OpenAI Compatible API
# ============================================================

class ChatCompletionRequest(BaseModel):
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    stream: bool = False
    session_id: Optional[str] = None
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI 兼容的聊天补全接口 (支持流式和非流式)"""
    if not runner:
        raise HTTPException(status_code=500, detail="Runner 未初始化")

    # 从 messages 中提取用户输入
    user_input = ""
    request_history = []
    
    # 简单的转换逻辑：最后一条消息作为当前输入，其余作为历史
    if request.messages:
        last_msg = request.messages[-1]
        if last_msg.get("role") == "user":
            user_input = last_msg.get("content", "")
            # 将前面的消息转换为历史记录
            for msg in request.messages[:-1]:
                role = msg.get("role")
                content = msg.get("content")
                if role in ("user", "assistant", "model") and content:
                    # 映射 'assistant' 到 'model' (如果需要)
                    if role == "assistant":
                        role = "model"
                    request_history.append({"role": role, "content": content})
    
    # Session 管理 - 从 store 读取累积历史
    store = get_session_store()
    session_id = request.session_id
    
    if session_id:
        # 使用指定的 session_id
        session = store.get_session(session_id)
        if not session:
            # 创建新 session，使用指定的 ID
            session = Session(
                id=session_id,
                app_name=runner.detection_result.name,
                user_id="user",
                events=[]
            )
            store._sessions[session_id] = session
    else:
        # 未指定 session_id，每次创建新 session（无记忆模式）
        session = store.create_session(runner.detection_result.name, "user", [])
        session_id = session.id
    
    # 构建历史：从 session store 读取 + 请求中的历史
    # 优先使用 session store 中的累积历史
    history = []
    if session and session.events:
        for event in session.events:
            content = event.get("content", {})
            role = content.get("role", "")
            parts = content.get("parts", [])
            text = ""
            for part in parts:
                if isinstance(part, dict) and "text" in part:
                    text += part["text"]
            if text and role in ("user", "model"):
                history.append({"role": role, "content": text})
    
    # 如果 session store 为空，使用请求中的历史
    if not history and request_history:
        history = request_history
    
    # 保存用户消息到 session
    user_event = {
        "id": str(uuid.uuid4()),
        "content": {"role": "user", "parts": [{"text": user_input}]},
        "timestamp": int(time.time() * 1000)
    }
    store.add_event(session_id, user_event)
    
    # 分支：流式 vs 非流式
    if request.stream:
        async def openai_stream_generator():
            input_data = {"input": user_input, "history": history}
            response_id = f"chatcmpl-{uuid.uuid4()}"
            created_time = int(time.time())
            accumulated_text = ""
            
            async for chunk in runner.stream(input_data):
                if chunk.get("type") == "text":
                    content = chunk.get("delta", "")
                    if content:
                        accumulated_text += content
                        yield f"data: {json.dumps({
                            'id': response_id,
                            'object': 'chat.completion.chunk',
                            'created': created_time,
                            'model': request.model or 'agent',
                            'choices': [{
                                'index': 0,
                                'delta': {'content': content},
                                'finish_reason': None
                            }]
                        }, ensure_ascii=False)}\n\n"
                elif chunk.get("type") == "thinking":
                    # 处理思考过程
                    content = chunk.get("delta", "")
                    if content:
                        yield f"data: {json.dumps({
                            'id': response_id,
                            'object': 'chat.completion.chunk',
                            'created': created_time,
                            'model': request.model or 'agent',
                            'choices': [{
                                'index': 0,
                                'delta': {'reasoning_content': content},
                                'finish_reason': None
                            }]
                        }, ensure_ascii=False)}\n\n"
                elif chunk.get("type") == "final":
                    final_text = chunk.get("output", "")
                    if final_text:
                        accumulated_text = final_text
                    # 发送结束标志
                    yield f"data: {json.dumps({
                        'id': response_id,
                        'object': 'chat.completion.chunk',
                        'created': created_time,
                        'model': request.model or 'agent',
                        'choices': [{
                            'index': 0,
                            'delta': {},
                            'finish_reason': 'stop'
                        }]
                    }, ensure_ascii=False)}\n\n"
            
            # 保存助手响应到 session
            if accumulated_text:
                assistant_event = {
                    "id": str(uuid.uuid4()),
                    "content": {"role": "model", "parts": [{"text": accumulated_text}]},
                    "timestamp": int(time.time() * 1000)
                }
                store.add_event(session_id, assistant_event)
            
            yield "data: [DONE]\n\n"

        return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")

    else:
        # 非流式模式
        input_data = {"input": user_input, "history": history}
        result = await runner.invoke(input_data)
        output_text = result.get("output", "")
        
        # 保存助手响应到 session
        assistant_event = {
            "id": str(uuid.uuid4()),
            "content": {"role": "model", "parts": [{"text": output_text}]},
            "timestamp": int(time.time() * 1000)
        }
        store.add_event(session_id, assistant_event)
        
        return {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model or "agent",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": output_text
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(user_input),
                "completion_tokens": len(output_text),
                "total_tokens": len(user_input) + len(output_text)
            },
            "session_id": session_id  # 返回 session_id 方便客户端使用
        }


# ============================================================
# Stub Endpoints for ADK-Web Compatibility
# ============================================================

@app.get("/apps/{app_name}/eval_sets")
async def list_eval_sets(app_name: str):
    """List evaluation sets - stub for ADK-Web"""
    return []


@app.get("/apps/{app_name}/eval_results")
async def list_eval_results(app_name: str):
    """List evaluation results - stub for ADK-Web"""
    return []


@app.get("/builder/app/{app_name}")
async def get_agent_builder(app_name: str, ts: int = 0, tmp: bool = False, file_path: str = None):
    """Get agent builder config - stub for ADK-Web"""
    # Return minimal YAML config for non-ADK projects
    return f"""name: {app_name}
model: deepseek-v3.2
description: {app_name} agent
instruction: You are a helpful assistant.
"""


@app.post("/builder/save")
async def save_agent_builder(request: Request, tmp: bool = False):
    """Save agent builder config - stub for ADK-Web"""
    return True


@app.post("/builder/app/{app_name}/cancel")
async def cancel_agent_changes(app_name: str):
    """Cancel agent builder changes - stub for ADK-Web"""
    return True


# Legacy /traces endpoint
@app.get("/traces")
async def get_traces(limit: int = 50):
    """Get recent traces (OpenTelemetry)"""
    exporter = get_memory_exporter()
    if not exporter:
        return {"traces": []}
        
    spans = exporter.get_finished_spans()
    traces = []
    for span in spans[-limit:]:
        traces.append({
            "name": span.get("name", "unknown"),
            "status": span.get("status", {}).get("code", "UNSET"),
            "start_time": span.get("start_time"),
            "end_time": span.get("end_time"),
            "attributes": span.get("attributes", {})
        })
    return {"traces": traces}


# ============================================================
# Static File Hosting
# ============================================================

# 静态文件目录
STATIC_DIR = Path(__file__).parent / "static"
LANGCHAIN_UI_DIR = STATIC_DIR / "langchain"

# 挂载 LangChain Web UI（/langchain/ 路径）
if LANGCHAIN_UI_DIR.exists():
    app.mount("/langchain", StaticFiles(directory=str(LANGCHAIN_UI_DIR), html=True), name="langchain-ui")
    logger.info(f"LangChain Web UI mounted at /langchain/ from: {LANGCHAIN_UI_DIR}")

# 使用 StaticFiles 挂载 ADK Web 静态文件（官方推荐方式）
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    # html=True 使得访问目录时自动返回 index.html
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    logger.info(f"ADK Web UI mounted from: {STATIC_DIR}")
else:
    logger.warning(f"Static files not found at: {STATIC_DIR}")
    logger.warning("Run 'make sync-static' to build and sync the Web UI")





