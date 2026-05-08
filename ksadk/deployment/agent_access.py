from __future__ import annotations

import asyncio
import inspect
from contextlib import nullcontext
from typing import Any, Awaitable, Callable, Mapping, Sequence


AgentAccessDetailFetcher = Callable[[str, bool], Awaitable[dict[str, Any]]]
AgentAccessErrorHandler = Callable[[Exception], None]


def canonicalize_agent_access_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化 Agent 详情里的常用访问字段，兼容嵌套和扁平两种结构。"""
    if not isinstance(detail, Mapping):
        return {}

    normalized = dict(detail)
    basic = normalized.get("basic") if isinstance(normalized.get("basic"), Mapping) else {}
    quick = (
        normalized.get("quick_access")
        if isinstance(normalized.get("quick_access"), Mapping)
        else {}
    )
    deployment = (
        normalized.get("deployment")
        if isinstance(normalized.get("deployment"), Mapping)
        else {}
    )

    def _pick(*values: Any) -> Any:
        for value in values:
            if value is not None and str(value).strip() != "":
                return value
        return None

    field_values = {
        "agent_id": _pick(basic.get("agent_id"), normalized.get("agent_id")),
        "name": _pick(basic.get("name"), normalized.get("name")),
        "status": _pick(basic.get("status"), normalized.get("status")),
        "framework": _pick(
            basic.get("framework"),
            deployment.get("framework"),
            normalized.get("framework"),
        ),
        "region": _pick(basic.get("region"), deployment.get("region"), normalized.get("region")),
        "endpoint": _pick(
            quick.get("public_endpoint"),
            quick.get("private_endpoint"),
            normalized.get("endpoint"),
        ),
        "api_key": _pick(quick.get("api_key"), normalized.get("api_key")),
        "artifact_path": _pick(deployment.get("artifact_path"), normalized.get("artifact_path")),
    }
    for key, value in field_values.items():
        if value is not None:
            normalized[key] = value
    return normalized


def extract_agent_access_fields(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """提取 quick access/state 常用字段。"""
    normalized = canonicalize_agent_access_detail(detail)
    return {
        "agent_id": normalized.get("agent_id"),
        "name": normalized.get("name"),
        "endpoint": normalized.get("endpoint"),
        "api_key": normalized.get("api_key"),
    }


def is_agent_not_visible_yet_error(exc: Exception) -> bool:
    """CreateAgent 后短时间内 GetAgent 404，视为可重试的可见性延迟。"""
    text = str(exc or "").lower()
    if not text:
        return False
    return (
        ("http 404" in text or "status=404" in text or "code: 404" in text)
        and ("未找到对应的 agent" in text or "not found" in text)
    )


def should_suppress_transient_get_agent_not_found_log(
    *,
    method: str,
    full_url: str,
    status_code: int,
    resp_text: str,
    details: dict[str, Any],
) -> bool:
    """仅抑制部署后短窗口内的 GetAgent 404 日志。"""
    if method.upper() != "POST" or "Action=GetAgent" not in str(full_url):
        return False
    if int(status_code or 0) != 404:
        return False
    message = " ".join(
        [
            str(details.get("remote_error_message") or "").strip(),
            str(details.get("message") or "").strip(),
            str(resp_text or "").strip(),
        ]
    )
    lowered = message.lower()
    return ("未找到对应的 agent" in lowered) or ("agent not found" in lowered)


def normalize_deployment_status(raw_status: object) -> str:
    """创建后若服务端暂时只回数值状态码，统一视作已提交。"""
    text = str(raw_status or "").strip()
    if not text or text.isdigit():
        return "SUBMITTED"
    return text.upper()


async def get_latest_agent_access(
    client: Any,
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
    include_api_key: bool = True,
    attempts: int = 1,
    interval_seconds: float = 0,
    initial_delay_seconds: float = 0,
    retry_delays: Sequence[float] | None = None,
    require_complete_access: bool = False,
    detail_fetcher: AgentAccessDetailFetcher | None = None,
    suppress_transient_not_found_log: bool = True,
    on_error: AgentAccessErrorHandler | None = None,
) -> dict[str, Any]:
    """回查最新 Agent 访问字段，默认静默处理创建后短暂 GetAgent 404 日志。"""
    agent_ref = str(agent_id or agent_name or "").strip()
    if not agent_ref:
        return {}

    async def _default_detail_fetcher(ref: str, fetch_api_key: bool) -> dict[str, Any]:
        if agent_id:
            detail = await client.get_agent(agent_id=agent_id, include_api_key=fetch_api_key)
        else:
            detail = await client.get_agent(name=agent_name, include_api_key=fetch_api_key)
        return canonicalize_agent_access_detail(detail)

    fetcher = detail_fetcher or _default_detail_fetcher
    retry_delay_values = [float(item) for item in (retry_delays or ())]
    attempts = (
        1 + len(retry_delay_values)
        if retry_delay_values
        else max(1, int(attempts or 1))
    )
    last_exc: Exception | None = None

    suppress_ctx = nullcontext()
    suppress_logs = getattr(client, "suppress_http_error_logging", None)
    if suppress_transient_not_found_log and callable(suppress_logs):
        candidate = suppress_logs(should_suppress_transient_get_agent_not_found_log)
        if inspect.isawaitable(candidate):
            close = getattr(candidate, "close", None)
            if callable(close):
                close()
        elif hasattr(candidate, "__enter__") and hasattr(candidate, "__exit__"):
            suppress_ctx = candidate

    with suppress_ctx:
        for attempt in range(attempts):
            if attempt == 0 and initial_delay_seconds > 0:
                await asyncio.sleep(initial_delay_seconds)
            elif attempt > 0:
                delay = (
                    retry_delay_values[attempt - 1]
                    if retry_delay_values
                    else float(interval_seconds)
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                detail = canonicalize_agent_access_detail(
                    await fetcher(agent_ref, include_api_key)
                )
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1 and is_agent_not_visible_yet_error(exc):
                    continue
                break

            access_ready = bool(str(detail.get("endpoint") or "").strip())
            api_key_ready = (not include_api_key) or bool(str(detail.get("api_key") or "").strip())
            if detail.get("agent_id") and (
                not require_complete_access or (access_ready and api_key_ready)
            ):
                return detail

    if last_exc is not None and on_error is not None:
        on_error(last_exc)
    return {}
