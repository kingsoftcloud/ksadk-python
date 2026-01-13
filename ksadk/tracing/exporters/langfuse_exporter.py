"""
Langfuse Exporter for KsADK Tracing

Converts OpenTelemetry spans to Langfuse traces using low-level SDK API.
Compatible with Langfuse SDK v3.
"""

import json
import logging
import os
from datetime import datetime
from typing import Sequence, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LangfuseExporterConfig:
    """Configuration for Langfuse exporter"""
    public_key: str
    secret_key: str
    host: str = "http://localhost:3000"
    enabled: bool = True


# Alias for backward compatibility
LangfuseConfig = LangfuseExporterConfig


class _LangfuseSpanExporter:
    """Internal span exporter that sends spans to Langfuse.
    
    Uses Langfuse low-level SDK API (v3 compatible).
    Agent metadata is loaded from ksadk.configs.settings.
    """
    
    def __init__(self, config: LangfuseExporterConfig):
        self.config = config
        self._langfuse = None
        self._agent_config = None  # Lazy load
        self._init_langfuse()
    
    def _get_agent_config(self):
        """Lazy load agent config from unified settings"""
        if self._agent_config is None:
            try:
                from ksadk.configs import settings
                self._agent_config = settings.agent
                
                # Log loaded metadata
                if self._agent_config.agent_id:
                    logger.info(f"Agent metadata loaded: agent_id={self._agent_config.agent_id}")
                if self._agent_config.tenant_id:
                    logger.debug(f"Tenant ID: {self._agent_config.tenant_id}")
            except ImportError:
                logger.warning("ksadk.configs not available, agent metadata disabled")
                self._agent_config = None
        return self._agent_config
    
    def _init_langfuse(self):
        """Initialize Langfuse client using low-level API"""
        try:
            # Set environment variables for Langfuse
            os.environ["LANGFUSE_PUBLIC_KEY"] = self.config.public_key
            os.environ["LANGFUSE_SECRET_KEY"] = self.config.secret_key
            os.environ["LANGFUSE_HOST"] = self.config.host
            
            # Use Langfuse low-level client
            from langfuse import Langfuse
            self._langfuse = Langfuse(
                public_key=self.config.public_key,
                secret_key=self.config.secret_key,
                host=self.config.host
            )
            logger.info(f"Langfuse client initialized: {self.config.host}")
        except ImportError:
            logger.warning("Langfuse package not installed. Run: pip install langfuse")
            self._langfuse = None
        except Exception as e:
            logger.error(f"Failed to initialize Langfuse: {e}")
            self._langfuse = None
    
    def export(self, spans) -> int:
        """Export spans to Langfuse using low-level API"""
        if not self._langfuse:
            return 0
        
        from opentelemetry.sdk.trace.export import SpanExportResult
        
        try:
            # Group spans by trace_id
            traces = {}
            for span in spans:
                trace_id = format(span.context.trace_id, '032x')
                if trace_id not in traces:
                    traces[trace_id] = []
                traces[trace_id].append(span)
            
            # Process each trace group
            for trace_id, trace_spans in traces.items():
                self._export_trace(trace_id, trace_spans)
            
            self._langfuse.flush()
            return SpanExportResult.SUCCESS
            
        except Exception as e:
            logger.error(f"Failed to export to Langfuse: {e}")
            import traceback
            traceback.print_exc()
            return SpanExportResult.FAILURE
    
    def _export_trace(self, trace_id: str, spans):
        """Export spans using Langfuse ingestion API"""
        # Find root span and categorize
        root_span = None
        llm_spans = []
        tool_spans = []
        
        for span in spans:
            name = span.name
            if name.startswith("langgraph.") or name.startswith("langchain."):
                root_span = span
            elif name == "call_llm":
                llm_spans.append(span)
            elif name.startswith("tool."):
                tool_spans.append(span)
        
        if not root_span:
            root_span = spans[0] if spans else None
        
        if not root_span:
            return
        
        # Extract attributes
        attrs = dict(root_span.attributes) if root_span.attributes else {}
        invocation_id = attrs.get("gcp.vertex.agent.invocation_id", trace_id[:32])
        user_input = attrs.get("user.input", "")
        agent_output = attrs.get("agent.output", "")
        
        # Convert timestamps
        start_time = datetime.fromtimestamp(root_span.start_time / 1e9)
        end_time = datetime.fromtimestamp(root_span.end_time / 1e9)
        
        # Create trace using ingestion endpoint
        try:
            # Build base metadata
            base_metadata = {
                "trace_id": trace_id,
                "framework": "ksadk",
            }
            
            # Get agent config from unified settings
            agent_config = self._get_agent_config()
            
            # Merge with agent metadata if available
            langfuse_params = {}
            trace_name = root_span.name
            
            if agent_config:
                agent_metadata = agent_config.to_langfuse_metadata()
                base_metadata.update(agent_metadata)
                
                langfuse_params = agent_config.to_langfuse_params()
                
                # Use agent_name if available
                if agent_config.agent_name:
                    trace_name = agent_config.agent_name
            
            # Use create_trace method (available in v3)
            trace = self._langfuse.trace(
                id=invocation_id,
                name=trace_name,
                input={"text": user_input} if user_input else None,
                output={"text": agent_output} if agent_output else None,
                metadata=base_metadata,
                **langfuse_params  # user_id, tags, version
            )
            
            # Add LLM generations
            for llm_span in llm_spans:
                self._add_generation(trace, llm_span)
            
            # Add tool spans
            for tool_span in tool_spans:
                self._add_tool_span(trace, tool_span)
                
        except AttributeError:
            # Fallback: use score/event API if trace() not available
            self._export_via_events(invocation_id, root_span, llm_spans, tool_spans)
    
    def _add_generation(self, trace, span):
        """Add LLM generation to trace"""
        attrs = dict(span.attributes) if span.attributes else {}
        
        llm_request_str = attrs.get("gcp.vertex.agent.llm_request", "{}")
        llm_response_str = attrs.get("gcp.vertex.agent.llm_response", "{}")
        
        try:
            llm_request = json.loads(llm_request_str)
            llm_response = json.loads(llm_response_str)
        except json.JSONDecodeError:
            llm_request = {"raw": llm_request_str}
            llm_response = {"raw": llm_response_str}
        
        # Extract text content
        input_text = ""
        output_text = ""
        
        if "contents" in llm_request:
            contents = llm_request.get("contents", [])
            if contents and "parts" in contents[0]:
                parts = contents[0].get("parts", [])
                if parts and "text" in parts[0]:
                    input_text = parts[0]["text"]
        
        if "candidates" in llm_response:
            candidates = llm_response.get("candidates", [])
            if candidates and "content" in candidates[0]:
                content = candidates[0]["content"]
                if "parts" in content:
                    parts = content["parts"]
                    if parts and "text" in parts[0]:
                        output_text = parts[0]["text"]
        
        model = attrs.get("model", attrs.get("gen_ai.request.model", "unknown"))
        
        try:
            trace.generation(
                name=span.name,
                model=model,
                input=input_text or llm_request,
                output=output_text or llm_response,
                metadata={
                    "span_id": format(span.context.span_id, '016x'),
                }
            )
        except AttributeError:
            # Generation method not available, skip
            pass
    
    def _add_tool_span(self, trace, span):
        """Add tool execution span"""
        attrs = dict(span.attributes) if span.attributes else {}
        
        tool_name = attrs.get("tool.name", span.name.replace("tool.", ""))
        tool_input = attrs.get("tool.input", "")
        tool_output = attrs.get("tool.output", "")
        
        try:
            trace.span(
                name=f"tool:{tool_name}",
                input={"args": tool_input} if tool_input else None,
                output={"result": tool_output} if tool_output else None,
                metadata={
                    "tool_name": tool_name,
                }
            )
        except AttributeError:
            pass
    
    def _export_via_events(self, trace_id: str, root_span, llm_spans, tool_spans):
        """Fallback export using event-based API"""
        # This is a fallback for when trace() method is not available
        logger.debug(f"Exporting trace {trace_id} via fallback method")
        # Just log for now - can be enhanced later
        pass
    
    def shutdown(self):
        """Shutdown the exporter"""
        if self._langfuse:
            try:
                self._langfuse.flush()
                self._langfuse.shutdown()
            except Exception:
                pass
    
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush pending spans"""
        if self._langfuse:
            try:
                self._langfuse.flush()
            except Exception:
                pass
        return True


class LangfuseExporter:
    """Langfuse exporter for KsADK tracing system.
    
    Compatible with Langfuse SDK v3.
    """
    
    def __init__(self, config: LangfuseExporterConfig):
        self.config = config
        self.name = "langfuse_exporter"
        
        self._exporter = _LangfuseSpanExporter(config)
        self.processor = self._create_processor()
    
    def _create_processor(self):
        """Create span processor for this exporter"""
        try:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            return SimpleSpanProcessor(self._exporter)
        except ImportError:
            logger.error("OpenTelemetry SDK not installed")
            return None
    
    @property
    def resource_attributes(self) -> dict:
        """Return resource attributes for this exporter"""
        return {
            "service.name": "ksadk",
            "langfuse.host": self.config.host,
        }
