"""
InMemoryExporter - 内存 Trace 存储

用于本地 Web UI 查看 Traces
"""

from typing import List, Dict, Any, Optional
from collections import deque
import threading
import time


class InMemoryExporter:
    """内存 Trace Exporter"""
    
    def __init__(self, max_traces: int = 1000):
        self.max_traces = max_traces
        self._traces: deque = deque(maxlen=max_traces)
        self._lock = threading.Lock()
    
    def export(self, spans) -> int:
        """导出 Spans (符合 OpenTelemetry SpanExporter 接口)"""
        from opentelemetry.sdk.trace.export import SpanExportResult
        
        with self._lock:
            for span in spans:
                trace_data = self._span_to_dict(span)
                self._traces.append(trace_data)
        
        return SpanExportResult.SUCCESS
    
    def shutdown(self) -> None:
        """关闭 Exporter"""
        pass
    
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """强制刷新"""
        return True
    
    def _span_to_dict(self, span) -> Dict[str, Any]:
        """将 Span 转换为字典"""
        ctx = span.get_span_context()
        
        return {
            "trace_id": format(ctx.trace_id, '032x'),
            "span_id": format(ctx.span_id, '016x'),
            "parent_span_id": format(span.parent.span_id, '016x') if span.parent else None,
            "name": span.name,
            "kind": str(span.kind),
            "start_time": span.start_time,
            "end_time": span.end_time,
            "duration_ns": span.end_time - span.start_time if span.end_time else 0,
            "attributes": dict(span.attributes) if span.attributes else {},
            "status": {
                "code": str(span.status.status_code),
                "description": span.status.description
            },
            "events": [
                {
                    "name": event.name,
                    "timestamp": event.timestamp,
                    "attributes": dict(event.attributes) if event.attributes else {}
                }
                for event in span.events
            ] if span.events else []
        }
    
    def get_traces(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取 Traces"""
        with self._lock:
            traces = list(self._traces)[-limit:]
            
            # 按 trace_id 分组
            grouped: Dict[str, List[Dict]] = {}
            for span in traces:
                trace_id = span["trace_id"]
                if trace_id not in grouped:
                    grouped[trace_id] = []
                grouped[trace_id].append(span)
            
            # 转换为 trace 列表
            result = []
            for trace_id, spans in grouped.items():
                result.append({
                    "trace_id": trace_id,
                    "spans": sorted(spans, key=lambda x: x["start_time"]),
                    "span_count": len(spans),
                    "start_time": min(s["start_time"] for s in spans),
                    "end_time": max(s["end_time"] for s in spans if s["end_time"]),
                })
            
            return sorted(result, key=lambda x: x["start_time"], reverse=True)[:limit]
    
    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """获取单个 Trace"""
        with self._lock:
            spans = [s for s in self._traces if s["trace_id"] == trace_id]
            
            if not spans:
                return None
            
            return {
                "trace_id": trace_id,
                "spans": sorted(spans, key=lambda x: x["start_time"]),
                "span_count": len(spans)
            }
    
    def get_finished_spans(self) -> List[Dict[str, Any]]:
        """返回所有已完成的 Spans (兼容 OpenTelemetry InMemorySpanExporter 接口)"""
        with self._lock:
            return list(self._traces)
    
    def clear(self) -> None:
        """清空 Traces"""
        with self._lock:
            self._traces.clear()
