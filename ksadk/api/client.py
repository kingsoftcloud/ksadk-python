"""
AgentEngine Server API 客户端

支持 AWS V4 签名认证，用于通过 KOP 网关访问 AgentEngine Server。
"""

import os
import re
import json
import uuid
import socket
import logging
import mimetypes
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, Sequence, Callable, Iterator
from urllib.parse import quote, urlparse

import requests
import urllib3

from ksadk.common.auth import AWSV4Auth

logger = logging.getLogger(__name__)


HttpErrorLogSuppressor = Callable[..., bool]


class DryRunExit(Exception):
    """DryRun 模式退出异常。"""

    def __init__(self, message: str = "Dry Run finished.", *, payload: Optional[Dict[str, Any]] = None):
        self.payload = payload or {}
        super().__init__(message)


class AgentEngineAPIError(Exception):
    """服务端 Action API 返回非零 Code 时抛出的结构化异常。"""

    def __init__(
        self,
        code: Any,
        message: Optional[str] = None,
        *,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.raw_code = code
        try:
            self.code = int(code)
        except (TypeError, ValueError):
            self.code = None
        self.message = (message or "Unknown API error").strip() or "Unknown API error"
        self.details = dict(details or {})
        super().__init__(self.__str__())

    def __str__(self) -> str:
        code_text = self.code if self.code is not None else self.raw_code
        return f"Server API Error (Code: {code_text}): {self.message}"


class AgentEngineClient:
    """AgentEngine Server API 客户端
    
    使用 AWS V4 签名认证访问 KOP 网关后的 AgentEngine Server。
    
    凭证来源 (优先级从高到低):
    1. 构造函数参数
    2. 环境变量 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY
    3. 环境变量 KS3_ACCESS_KEY / KS3_SECRET_KEY
    """

    _DEFAULT_PERMISSION_ROLE = "KsyunAgentEngineDefaultRole"
    _PERMISSION_PROBE_ACTIONS = {"CreateAgentProduct", "CreateAgent", "ListAgents", "GetAgent"}
    _permission_probe_cache: dict[tuple[str, str, str], bool] = {}

    def __init__(
        self, 
        base_url: Optional[str] = None, 
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "cn-beijing-6",
        service: Optional[str] = None,
        timeout: float = 60.0,
        dry_run: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = (
            base_url 
            or os.getenv("AGENTENGINE_SERVER_URL")
        )
        if not self.base_url:
            self.base_url = self._detect_default_base_url()
        
        # 本地调试覆盖 (如果需要)
        # self.base_url = "http://localhost:8081"
        self.timeout = timeout
        self.logical_region = region
        self.region = self._normalize_control_region(region)
        self.custom_source = self._resolve_custom_source(region)
        self.dry_run = bool(dry_run or self._is_global_dry_run_enabled())
        self.extra_headers = extra_headers or {}
        # 签名 service 可通过环境变量覆盖（例如 aicp）
        self.service = service or os.getenv("AGENTENGINE_SIGN_SERVICE", "aicp")
        
        # AWS V4 签名
        self._auth = AWSV4Auth(
            access_key_id=access_key or "",
            secret_access_key=secret_key or "",
            region=self.region,
            service=self.service,
        )
        
        if self._auth.is_enabled:
            logger.debug(f"AgentEngineClient initialized with V4Auth: {self.base_url}")
        else:
            logger.debug("AgentEngineClient: No credentials, signing disabled")
            
        self._session: Optional[requests.Session] = None
        self._http_error_log_suppressors: list[HttpErrorLogSuppressor] = []

    @staticmethod
    def _ssl_verify_enabled() -> bool:
        insecure = (
            os.getenv("AGENTENGINE_SSL_INSECURE")
            or os.getenv("CURL_SSL_INSECURE")
            or ""
        ).strip().lower()
        if insecure in {"1", "true", "yes", "on"}:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            return False
        return True

    @staticmethod
    def _is_global_dry_run_enabled() -> bool:
        """根命令 --dry-run 开关：支持未显式透传 dry_run 参数的命令。"""
        return os.getenv("AGENTENGINE_GLOBAL_DRY_RUN", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _is_connectable(url: str, timeout: float = 1.0) -> bool:
        """检查目标地址 TCP 连通性。"""
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            if not host:
                return False
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _detect_default_base_url(self) -> str:
        """默认使用公开控制面地址。"""
        return "https://aicp.api.ksyun.com"

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _get_host(self) -> str:
        """提取 Host (用于签名)"""
        host = self.base_url
        if host.startswith("http://"):
            host = host[7:]
        elif host.startswith("https://"):
            host = host[8:]
        return host.split("/")[0].rstrip("/")

    def _is_kop_mode(self) -> bool:
        """是否使用 KOP 协议模式（按 base_url 是否包含 aicp 判断）。"""
        return "aicp" in (self.base_url or "").lower()

    @staticmethod
    def _normalize_control_region(region: Optional[str]) -> str:
        """归一化控制面 Region（KOP 头部与签名使用）。"""
        region = (region or "cn-beijing-6").strip()
        if region.lower() == "pre-online":
            return os.getenv("AGENTENGINE_PRE_CONTROL_REGION", "cn-beijing-6")
        return region

    @staticmethod
    def _resolve_custom_source(region: Optional[str]) -> Optional[str]:
        """根据逻辑环境决定是否附加 X-KSC-CUSTOM-SOURCE。"""
        if (region or "").strip().lower() == "pre-online":
            return os.getenv("AGENTENGINE_PRE_CUSTOM_SOURCE", "pre")
        return None

    @staticmethod
    def _should_fallback_get_agent_with_legacy_id(error: Exception) -> bool:
        """仅在明确识别为字段兼容问题时，才回退到旧控制面的 Id 字段。"""
        text = str(error or "").lower()
        if not text:
            return False

        # 404 / not found / 缺少入参都属于正常业务错误，不应被误判为字段兼容问题。
        non_compat_markers = (
            "http 404",
            "status=404",
            "not found",
            "未找到对应的 agent",
            "请至少传入 agentid 或 name",
            "请至少传入 agent_id 或 name",
        )
        if any(marker in text for marker in non_compat_markers):
            return False

        agent_id_markers = (
            "agentid",
            "agent_id",
            '"agentid"',
            "'agentid'",
            '"agent_id"',
            "'agent_id'",
        )
        if not any(marker in text for marker in agent_id_markers):
            return False

        compat_markers = (
            "422",
            "invalid",
            "validation error",
            "field required",
            "extra inputs are not permitted",
            "extra fields not permitted",
            "unexpected field",
            "unknown field",
            "unrecognized field",
            "not permitted",
            "not allowed",
        )
        return any(marker in text for marker in compat_markers)

    def _normalize_payload_region(self, region: Optional[str]) -> str:
        """归一化请求体中的 Region 字段。"""
        return self._normalize_control_region(region or self.logical_region or "cn-beijing-6")

    @staticmethod
    def _extract_http_error_details(resp_text: str) -> Dict[str, Any]:
        text = str(resp_text or "").strip()
        details: Dict[str, Any] = {}
        if not text:
            return details
        try:
            payload = json.loads(text)
        except Exception:
            return details
        if not isinstance(payload, dict):
            return details

        request_id = str(payload.get("RequestId") or payload.get("RequestID") or "").strip()
        if request_id:
            details["request_id"] = request_id

        error = payload.get("Error")
        if isinstance(error, dict):
            remote_code = str(error.get("Code") or "").strip()
            remote_message = str(error.get("Message") or "").strip()
            remote_type = str(error.get("Type") or "").strip()
            if remote_code:
                details["remote_error_code"] = remote_code
            if remote_message:
                details["remote_error_message"] = remote_message
            if remote_type:
                details["remote_error_type"] = remote_type

        message = str(payload.get("Message") or "").strip()
        if message:
            details["message"] = message

        return details

    @staticmethod
    def _is_auth_related_error_details(details: Dict[str, Any]) -> bool:
        remote_code = str(details.get("remote_error_code") or "").strip().lower()
        remote_message = str(details.get("remote_error_message") or details.get("message") or "").strip().lower()
        if remote_code in {
            "missingaccesskey",
            "missingsecretkey",
            "invalidaccesskey",
            "signaturedoesnotmatch",
            "invalidsignature",
            "signaturemismatch",
            "accessdenied",
            "accessdeniedexception",
            "unauthorized",
            "unauthorizedoperation",
            "invalidclienttokenid",
            "authfailure",
        }:
            return True
        markers = (
            "access key is missing",
            "secret key is missing",
            "invalid access key",
            "access key id you provided does not exist",
            "signature",
            "access denied",
            "unauthorized",
            "没有",
            "权限",
            "permission",
        )
        return any(marker in remote_message for marker in markers)

    @contextmanager
    def suppress_http_error_logging(
        self,
        predicate: Optional[HttpErrorLogSuppressor] = None,
    ) -> Iterator[None]:
        suppressor = predicate or (
            lambda *, method, full_url, status_code, resp_text, details: True
        )
        self._http_error_log_suppressors.append(suppressor)
        try:
            yield
        finally:
            if self._http_error_log_suppressors:
                self._http_error_log_suppressors.pop()

    def _log_http_error(self, *, method: str, full_url: str, status_code: int, resp_text: str, details: Dict[str, Any]) -> None:
        for suppressor in reversed(self._http_error_log_suppressors):
            try:
                if suppressor(
                    method=method,
                    full_url=full_url,
                    status_code=status_code,
                    resp_text=resp_text,
                    details=details,
                ):
                    return
            except Exception:
                continue
        log_fn = logger.debug if self._is_auth_related_error_details(details) else logger.error
        log_fn(
            "Request failed: method=%s, url=%s, status=%s, body=%s",
            method,
            full_url,
            status_code,
            resp_text,
        )

    def _build_headers(self, request_id: str = "", action: str = "", kop_mode: bool = False) -> Dict[str, str]:
        if not request_id:
            request_id = str(uuid.uuid4())
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": self._get_host(),
            "X-Ksc-Request-Id": request_id,
            "X-Ksc-Region": self.region,
            "X-Ksc-Source": "ksadk-cli",
        }
        if kop_mode and action:
            headers["X-Action"] = action
            headers["X-Version"] = os.getenv("AGENTENGINE_API_VERSION", "2024-06-12")
        if self.custom_source:
            headers["X-KSC-CUSTOM-SOURCE"] = self.custom_source
        
        # 统一使用 X-Ksc-Account-Id (废弃 X-Ksc-Account-Id)
        account_id = os.getenv("KSYUN_ACCOUNT_ID")
        if account_id:
            headers["X-Ksc-Account-Id"] = account_id
        if self.extra_headers:
            headers.update(self.extra_headers)
        return headers

    def _resolve_account_id(self) -> Optional[str]:
        for key, value in self.extra_headers.items():
            if key.lower() == "x-ksc-account-id" and str(value or "").strip():
                return str(value).strip()
        account_id = os.getenv("KSYUN_ACCOUNT_ID", "").strip()
        return account_id or None

    def _resolve_permission_role_name(self, action: str, params: Dict[str, Any]) -> str:
        if action in {"CreateAgentProduct", "CreateAgent"}:
            access = params.get("Access") if isinstance(params, dict) else None
            if isinstance(access, dict):
                explicit_role = (
                    access.get("IamRole")
                    or access.get("iamRole")
                    or access.get("iam_role")
                )
                if str(explicit_role or "").strip():
                    return str(explicit_role).strip()
        return self._DEFAULT_PERMISSION_ROLE

    def _maybe_precheck_permission(self, action: str, params: Dict[str, Any]) -> None:
        if action not in self._PERMISSION_PROBE_ACTIONS or action == "CheckIamRole":
            return

        account_id = self._resolve_account_id()
        if not account_id:
            logger.warning("Skip permission precheck for %s: missing KSYUN_ACCOUNT_ID", action)
            return

        role_name = self._resolve_permission_role_name(action, params)
        cache_key = (account_id, self.region, role_name)
        cached = self._permission_probe_cache.get(cache_key)
        if cached is True:
            return
        if cached is False:
            raise AgentEngineAPIError(403, f"当前账号没有 {role_name} 权限")

        try:
            result = self._request(
                "POST",
                "/agentengine/api/v1/CheckIamRole",
                {"RoleName": role_name},
            )
        except Exception as exc:
            details = getattr(exc, "details", {}) if isinstance(exc, AgentEngineAPIError) else {}
            if self._is_auth_related_error_details(details):
                logger.debug("Permission probe auth failure for %s: %s", action, exc)
            else:
                logger.warning("Permission probe failed for %s: %s", action, exc)
            return

        code = int(result.get("Code", 0) or 0)
        data = result.get("Data") or {}
        has_permission = data.get("HasPermission")
        if has_permission is False or code in {401, 403}:
            self._permission_probe_cache[cache_key] = False
            raise AgentEngineAPIError(
                code=code or 403,
                message=result.get("Message") or f"当前账号没有 {role_name} 权限",
            )

        if code != 0:
            logger.warning(
                "Permission probe unavailable for %s: code=%s message=%s",
                action,
                code,
                result.get("Message", ""),
            )
            return

        self._permission_probe_cache[cache_key] = True

    def _request(
        self, 
        method: str,
        path: str, 
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步 HTTP 请求"""
        action = path.rstrip("/").split("/")[-1] if path else ""
        kop_mode = self._is_kop_mode()
        headers = self._build_headers(action=action, kop_mode=kop_mode)
        if kop_mode:
            version = os.getenv("AGENTENGINE_API_VERSION", "2024-06-12")
            full_url = f"{self.base_url.rstrip('/')}/?Action={action}&Version={version}"
        else:
            full_url = f"{self.base_url}{path}"
        body_str = json.dumps(body, ensure_ascii=False) if body else ""
        
        # DryRun 模式
        if self.dry_run:
            signed_headers = headers
            if self._auth.is_enabled:
                # Dry-run 展示真实即将发送的签名头（不暴露明文 SK）
                signed_headers = self._auth.sign_headers(
                    method=method,
                    url=full_url,
                    headers=headers.copy(),
                    body=body_str,
                )

            # 生成 Curl 命令
            curl_cmd = f"curl -X {method} \"{full_url}\" \\\n"
            for k, v in signed_headers.items():
                curl_cmd += f"  -H \"{k}: {v}\" \\\n"
            if body_str:
                curl_cmd += f"  -d '{body_str}'"

            # 抛出异常以中断流程 (CLI 应捕获此异常)
            raise DryRunExit(
                "Dry Run finished.",
                payload={
                    "method": method,
                    "url": full_url,
                    "headers": signed_headers,
                    "body": body,
                    "curl": curl_cmd,
                },
            )
            
        logger.debug(f"Request: {method} {full_url}")
        
        session = self._get_session()
        
        response = session.request(
            method=method,
            url=full_url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            auth=self._auth.get_auth(),  # AWS V4 签名
            timeout=self.timeout,
            verify=self._ssl_verify_enabled(),
        )
        
        logger.debug(f"Response: {response.status_code}")

        if response.status_code >= 400:
            resp_text = response.text or ""
            details = self._extract_http_error_details(resp_text)
            details.setdefault("http_status", response.status_code)
            self._log_http_error(
                method=method,
                full_url=full_url,
                status_code=response.status_code,
                resp_text=resp_text,
                details=details,
            )
            message = (
                str(details.get("remote_error_message") or details.get("message") or "").strip()
                or resp_text
            )
            if details:
                raise AgentEngineAPIError(response.status_code, message, details=details)
            raise Exception(f"HTTP {response.status_code} {method} {full_url}: {resp_text}")

        if response.text:
            try:
                return response.json()
            except Exception as e:
                raise Exception(f"Invalid JSON response from {full_url}: {response.text}") from e
        return {}
    
    # Async 包装 (保持兼容性)
    async def close(self):
        if self._session:
            self._session.close()
            self._session = None
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ===== Action API (推荐使用) =====
    
    @staticmethod
    def _pascal_key(key: str) -> str:
        """PascalCase/camelCase 键名转 snake_case"""
        # "MCPs" -> "mcps", "AgentId" -> "agent_id", "QuickAccess" -> "quick_access"
        s1 = re.sub('([A-Z]+)([A-Z][a-z])', r'\1_\2', key)
        s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
        return s2.lower()

    @classmethod
    def _to_snake_case(cls, data):
        """递归将字典的 PascalCase 键名转为 snake_case"""
        if isinstance(data, dict):
            return {cls._pascal_key(k): cls._to_snake_case(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls._to_snake_case(item) for item in data]
        return data

    @classmethod
    def _fix_endpoints_protocol(cls, data):
        """递归将 endpoint 相关字段的 https:// 替换为 http:// (预发环境)"""
        _ENDPOINT_KEYS = {"endpoint", "public_endpoint", "private_endpoint"}
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if k in _ENDPOINT_KEYS and isinstance(v, str) and v.startswith("https://"):
                    result[k] = "http://" + v[8:]
                else:
                    result[k] = cls._fix_endpoints_protocol(v)
            return result
        elif isinstance(data, list):
            return [cls._fix_endpoints_protocol(item) for item in data]
        return data

    @staticmethod
    def _parse_container_image_ref(image_ref: str) -> tuple[str, str, str]:
        """解析容器镜像地址，返回 (namespace, repo, tag)。"""
        raw = (image_ref or "").strip()
        if not raw:
            return "default", "", "latest"

        image = raw
        for prefix in ("http://", "https://"):
            if image.startswith(prefix):
                image = image[len(prefix):]
                break

        image_no_tag = image
        tag = "latest"
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        if last_colon > last_slash:
            image_no_tag = image[:last_colon]
            tag = image[last_colon + 1 :] or "latest"

        path = image_no_tag
        first = image_no_tag.split("/", 1)[0].strip()
        has_registry = "." in first or ":" in first or first == "localhost"
        if has_registry and "/" in image_no_tag:
            path = image_no_tag.split("/", 1)[1]

        if "/" in path:
            namespace, repo = path.split("/", 1)
        else:
            namespace, repo = "default", path

        return namespace or "default", repo, tag

    @staticmethod
    def _normalize_framework_name(framework: Optional[str]) -> str:
        """规范化 framework 名称，默认 langgraph。"""
        normalized = (framework or "langgraph").strip().lower()
        return normalized or "langgraph"

    @staticmethod
    def _normalize_framework_filters(framework: Optional[str | Sequence[str]]) -> str | None:
        """规范化 ListAgents 的 framework 过滤，支持 CSV 和字符串序列。"""
        if framework is None:
            return None

        raw_values: list[str]
        if isinstance(framework, str):
            raw_values = [framework]
        else:
            raw_values = [str(item) for item in framework if str(item).strip()]

        normalized_values: list[str] = []
        seen: set[str] = set()
        for raw in raw_values:
            for part in raw.split(","):
                normalized = part.strip().lower()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    normalized_values.append(normalized)

        if not normalized_values:
            return None
        return ",".join(normalized_values)

    @staticmethod
    def _extract_runtime_access(detail: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(detail, dict):
            return {}

        basic = detail.get("basic") if isinstance(detail.get("basic"), dict) else {}
        quick = detail.get("quick_access") if isinstance(detail.get("quick_access"), dict) else {}
        deployment = detail.get("deployment") if isinstance(detail.get("deployment"), dict) else {}
        framework = (
            deployment.get("framework")
            or basic.get("framework")
            or detail.get("framework")
        )
        return {
            "agent_id": basic.get("agent_id") or detail.get("agent_id"),
            "name": basic.get("name") or detail.get("name"),
            "endpoint": quick.get("public_endpoint")
            or quick.get("private_endpoint")
            or detail.get("endpoint"),
            "api_key": quick.get("api_key") or detail.get("api_key"),
            "framework": str(framework or "").strip().lower() or None,
        }

    @staticmethod
    def _encode_workspace_runtime_path(remote_path: str) -> str:
        raw = str(remote_path or "").strip().replace("\\", "/")
        if not raw or raw in {".", "/"}:
            raise ValueError("workspace file path must not be empty")
        segments = [segment for segment in raw.split("/") if segment not in {"", "."}]
        if not segments:
            raise ValueError("workspace file path must not be empty")
        return "/".join(quote(segment, safe="") for segment in segments)

    def _workspace_runtime_error(self, response: requests.Response) -> AgentEngineAPIError:
        message = (response.text or "").strip() or f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            message = (
                str(payload.get("detail") or payload.get("Message") or message).strip()
                or message
            )
        return AgentEngineAPIError(response.status_code, message)

    @staticmethod
    def _compact_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
        return {
            key: value
            for key, value in (params or {}).items()
            if value is not None
        }

    @staticmethod
    def _workspace_prefers_action_proxy(detail: Dict[str, Any] | None) -> bool:
        if not isinstance(detail, dict):
            return False
        access = AgentEngineClient._extract_runtime_access(detail)
        framework = str(access.get("framework") or "").strip().lower()
        return framework == "openclaw"

    async def _resolve_workspace_transport(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        endpoint_value = str(endpoint or "").strip()
        api_key_value = str(api_key or "").strip() or None
        if endpoint_value:
            return {
                "mode": "runtime",
                "agent_id": agent_id,
                "name": name,
                "access": {
                    "endpoint": endpoint_value.rstrip("/"),
                    "api_key": api_key_value,
                },
            }

        detail = await self.get_agent(agent_id=agent_id, name=name, include_api_key=True)
        access = self._extract_runtime_access(detail)
        resolved_agent_id = str(access.get("agent_id") or agent_id or "").strip() or None
        resolved_name = str(access.get("name") or name or "").strip() or None
        resolved_api_key = api_key_value or (
            str(access.get("api_key") or "").strip() or None
        )
        if self._workspace_prefers_action_proxy(detail):
            return {
                "mode": "action",
                "agent_id": resolved_agent_id,
                "name": resolved_name,
                "access": access,
            }

        resolved_endpoint = str(access.get("endpoint") or "").strip()
        if not resolved_endpoint:
            raise AgentEngineAPIError(404, "Agent runtime endpoint is not ready")
        return {
            "mode": "runtime",
            "agent_id": resolved_agent_id,
            "name": resolved_name,
            "access": {
                "endpoint": resolved_endpoint.rstrip("/"),
                "api_key": resolved_api_key,
            },
        }

    def _action_raw_request(
        self,
        method: str,
        action: str,
        *,
        params: Dict[str, Any] | None = None,
        data: Dict[str, Any] | None = None,
        files: Dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> requests.Response:
        kop_mode = self._is_kop_mode()
        headers = self._build_headers(action=action, kop_mode=kop_mode)
        headers["Accept"] = accept
        if files is not None:
            headers.pop("Content-Type", None)
        full_url = (
            f"{self.base_url.rstrip('/')}/?Action={action}&Version={os.getenv('AGENTENGINE_API_VERSION', '2024-06-12')}"
            if kop_mode
            else f"{self.base_url}/agentengine/api/v1/{action}"
        )
        session = self._get_session()
        response = session.request(
            method=method,
            url=full_url,
            params=self._compact_params(params),
            data=self._compact_params(data) if data is not None else None,
            files=files,
            headers=headers,
            auth=self._auth.get_auth(),
            timeout=self.timeout,
            verify=self._ssl_verify_enabled(),
        )
        if response.status_code >= 400:
            resp_text = response.text or ""
            details = self._extract_http_error_details(resp_text)
            details.setdefault("http_status", response.status_code)
            self._log_http_error(
                method=method,
                full_url=full_url,
                status_code=response.status_code,
                resp_text=resp_text,
                details=details,
            )
            message = (
                str(details.get("remote_error_message") or details.get("message") or "").strip()
                or resp_text
            )
            raise AgentEngineAPIError(response.status_code, message, details=details or None)
        return response

    async def _resolve_workspace_runtime_access(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        if transport["mode"] != "runtime":
            raise AgentEngineAPIError(400, "Workspace runtime direct access is unavailable for this agent")
        return transport["access"]

    def _workspace_runtime_request(
        self,
        *,
        access: Dict[str, Any],
        method: str,
        path: str,
        params: Dict[str, Any] | None = None,
        files: Dict[str, Any] | None = None,
    ) -> requests.Response:
        url = f"{access['endpoint'].rstrip('/')}/_ksadk/workspace/v1/{path.lstrip('/')}"
        headers: Dict[str, str] = {}
        if access.get("api_key"):
            headers["Authorization"] = f"Bearer {access['api_key']}"
        response = self._get_session().request(
            method=method,
            url=url,
            headers=headers or None,
            params=params,
            files=files,
            stream=False,
            timeout=self.timeout,
            verify=self._ssl_verify_enabled(),
        )
        setattr(response, "_ksadk_workspace_url", url)
        if response.status_code >= 400:
            raise self._workspace_runtime_error(response)
        return response

    def _workspace_runtime_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            url = str(
                getattr(response, "_ksadk_workspace_url", "")
                or getattr(response, "url", "")
            ).strip()
            body = (response.text or "").strip()
            body_preview = body[:200] if body else "<empty response body>"
            raise AgentEngineAPIError(
                502,
                (
                    "workspace runtime returned invalid JSON"
                    + (f" from {url}" if url else "")
                    + f": {body_preview}"
                ),
                details={
                    "workspace_runtime_url": url or None,
                    "workspace_runtime_status": getattr(response, "status_code", None),
                    "workspace_runtime_body_preview": body_preview,
                },
            ) from exc
        return self._to_snake_case(payload)

    @staticmethod
    def _annotate_workspace_payload(payload: Dict[str, Any], *, transport_mode: str) -> Dict[str, Any]:
        annotated = dict(payload or {})
        annotated["transport_mode"] = transport_mode
        return annotated

    def _action(self, action: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """通用 Action API 调用"""
        body = params or {}
        self._maybe_precheck_permission(action, body)
        result = self._request("POST", f"/agentengine/api/v1/{action}", body)
        
        # 检查错误 (统一返回格式 {"Code": 0, ...})
        code = result.get("Code", 0)
        if code != 0:
            msg = result.get("Message", "Unknown API error")
            raise AgentEngineAPIError(code=code, message=msg)
            
        if result.get("Error"):
            raise Exception(result["Error"].get("Message", "Unknown error"))
        
        # 提取 Data 并统一转换为 snake_case
        data = result.get("Data") if result.get("Data") is not None else result
        data = self._to_snake_case(data)
        
        # 预发环境 endpoint 协议归一化: https -> http
        if self.logical_region and self.logical_region.strip().lower() == "pre-online":
            data = self._fix_endpoints_protocol(data)
        
        return data

    # ===== Agent Actions =====

    @staticmethod
    def _normalize_network_payload(network: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(network, dict):
            return None

        def _pick(*keys: str, default: Any = None) -> Any:
            for key in keys:
                if key in network and network[key] is not None:
                    return network[key]
            return default

        payload: Dict[str, Any] = {
            "EnablePublicAccess": bool(
                _pick("enable_public_access", "enablePublicAccess", "EnablePublicAccess", default=False)
            ),
            "EnableVpcAccess": bool(
                _pick("enable_vpc_access", "enableVpcAccess", "EnableVpcAccess", default=False)
            ),
        }

        field_map = {
            "VpcId": ("vpc_id", "vpcId", "VpcId"),
            "SubnetId": ("subnet_id", "subnetId", "SubnetId"),
            "SecurityGroupId": ("security_group_id", "securityGroupId", "SecurityGroupId"),
            "AvailabilityZone": ("availability_zone", "availabilityZone", "AvailabilityZone"),
        }
        for target_key, source_keys in field_map.items():
            value = str(_pick(*source_keys, default="") or "").strip()
            if value:
                payload[target_key] = value

        return payload

    @staticmethod
    def _normalize_ui_config_payload(ui_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(ui_config, dict):
            return None

        def _extract(*keys: str) -> tuple[bool, Any]:
            for key in keys:
                if key in ui_config:
                    return True, ui_config.get(key)
            return False, None

        profile_present, profile = _extract("profile", "Profile")
        path_present, path = _extract("path", "Path")
        url_present, url = _extract("url", "Url")
        if not (profile_present or path_present or url_present):
            return None

        payload: Dict[str, Any] = {}
        if profile_present:
            payload["Profile"] = str(profile).strip() if profile is not None else None
        if path_present:
            payload["Path"] = str(path).strip() if path is not None else None
        if url_present:
            payload["Url"] = str(url).strip() if url is not None else None
        return payload

    @staticmethod
    def _normalize_storage_payload(storage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(storage, dict):
            return None

        mount_path = str(
            storage.get("mount_path")
            or storage.get("mountPath")
            or storage.get("MountPath")
            or ""
        ).strip()
        size_gi = storage.get("size_gi", storage.get("sizeGi", storage.get("SizeGi")))
        if not mount_path and size_gi is None:
            return None

        payload: Dict[str, Any] = {}
        if mount_path:
            payload["MountPath"] = mount_path
        if size_gi is not None:
            payload["SizeGi"] = int(size_gi)
        return payload

    @staticmethod
    def _normalize_memory_config_payload(memory_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(memory_config, dict):
            return None

        memory_system = str(
            memory_config.get("memory_system")
            or memory_config.get("MemorySystem")
            or ""
        ).strip()
        if not memory_system:
            return None

        payload: Dict[str, Any] = {
            "MemorySystem": memory_system,
        }
        field_mapping = {
            "Mem0InstanceId": (
                memory_config.get("mem0_instance_id")
                or memory_config.get("Mem0InstanceId")
            ),
            "Mem0InstanceName": (
                memory_config.get("mem0_instance_name")
                or memory_config.get("Mem0InstanceName")
            ),
            "Mem0Region": (
                memory_config.get("mem0_region")
                or memory_config.get("Mem0Region")
            ),
        }
        for key, value in field_mapping.items():
            text = str(value or "").strip()
            if text:
                payload[key] = text
        return payload

    async def create_agent(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建 Agent (通过 CreateAgentProduct 走订单流程)"""
        framework = self._normalize_framework_name(data.get("framework"))
        params = {
            "Name": data.get("name"),
            "Description": data.get("description"),
            "Framework": framework,
            "DeploymentType": data.get("artifact_type", "Code"),
            "Region": self._normalize_payload_region(data.get("region", "cn-beijing-6")),
            "Resource": {
                "Cpu": int(float(data.get("resources", {}).get("cpu", 2))),
                "Memory": int(str(data.get("resources", {}).get("memory", "4Gi")).replace("Gi", ""))
            },
            "Scaling": {
                "MinReplicas": int(data.get("scaling", {}).get("min_replicas", 1)),
                "MaxReplicas": int(data.get("scaling", {}).get("max_replicas", 10)),
                "QpsPerInstance": int(data.get("scaling", {}).get("concurrency", 20)),
            },
            "AutoPay": True,
        }

        network_payload = self._normalize_network_payload(data.get("network"))
        if network_payload:
            params["Network"] = network_payload

        ui_config_payload = self._normalize_ui_config_payload(data.get("ui_config"))
        if ui_config_payload is not None:
            params["UiConfig"] = ui_config_payload

        storage_payload = self._normalize_storage_payload(data.get("storage"))
        if storage_payload is not None:
            params["Storage"] = storage_payload

        memory_config_payload = self._normalize_memory_config_payload(data.get("memory_config"))
        if memory_config_payload is not None:
            params["MemoryConfig"] = memory_config_payload

        # 访问控制 (默认 ApiKey；OpenClaw 可显式传入 None 关闭平台层鉴权)
        auth_type = data.get("auth_type")
        if auth_type:
            params["Access"] = {
                "AuthType": auth_type,
                "IamRole": data.get("iam_role", "KsyunAgentEngineDefaultRole"),
            }

        if params["DeploymentType"] == "Code":
            ks3 = data.get("ks3", {})
            params["CodeConfig"] = {
                "Path": data.get("artifact_path", ""),
                "AccessKey": ks3.get("access_key"),
                "SecretKey": ks3.get("secret_key"),
                "Region": self._normalize_payload_region(ks3.get("region", "cn-beijing-6")),
                "Bucket": ks3.get("bucket"),
            }
        else:
            ic = data.get("image_credential", {}) or {}
            image_username = (ic.get("username") or "").strip()
            image_password = (ic.get("password") or "").strip()
            artifact = (data.get("artifact_path", "") or "").strip()
            img_ns, img_repo, img_ver = self._parse_container_image_ref(artifact)

            container_config = {
                "ImageType": "Personal",
                "NameSpace": img_ns,
                "ImageRepo": img_repo,
                "ImageVersion": img_ver,
                "ImageAddr": artifact,
            }
            # 仅在用户名和密码同时存在时才传鉴权，避免对公共镜像误触发失败鉴权重试。
            if image_username and image_password:
                container_config["UserName"] = image_username
                container_config["Password"] = image_password
            params["ContainerConfig"] = container_config

        env_vars = []
        envs = data.get("env_vars") or data.get("environment_variables")
        if envs:
            if isinstance(envs, dict):
                for k, v in envs.items():
                    env_vars.append({"Key": k, "Value": str(v), "IsSensitive": False})
            elif isinstance(envs, list):
                env_vars = envs

        advanced = {
            "EnableObservability": True,
            "EnvironmentVariables": env_vars,
        }
        inbound_identity_auth = data.get("inbound_identity_auth")
        if inbound_identity_auth is not None:
            advanced["InboundIdentityAuth"] = inbound_identity_auth
        project_id = data.get("project_id")
        if project_id:
            advanced["ProjectId"] = project_id
        params["Advanced"] = advanced

        return self._action("CreateAgentProduct", params)

    async def get_agent(self, agent_id: str = None, name: str = None, include_api_key: bool = False) -> Dict[str, Any]:
        """获取 Agent 详情（支持 AgentId 或 Name 查询）"""
        if agent_id:
            params: Dict[str, Any] = {"AgentId": agent_id}
            if include_api_key:
                params["IncludeApiKey"] = True
            try:
                return self._action("GetAgent", params)
            except Exception as e:
                # 兼容旧控制面：极少数版本仍使用 Id 字段
                if self._should_fallback_get_agent_with_legacy_id(e):
                    fallback = {"Id": agent_id}
                    if include_api_key:
                        fallback["IncludeApiKey"] = True
                    return self._action("GetAgent", fallback)
                raise

        if name:
            params = {"Name": name}
            if include_api_key:
                params["IncludeApiKey"] = True
            return self._action("GetAgent", params)

        return self._action("GetAgent", {})

    async def get_agent_ui_bootstrap(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        session_id: str | None = None,
    ) -> Dict[str, Any]:
        """获取 Agent UI bootstrap 元数据。"""
        params: Dict[str, Any] = {}
        if agent_id:
            params["AgentId"] = agent_id
        if name:
            params["Name"] = name
        if session_id:
            params["SessionId"] = session_id
        return self._action("GetAgentUiBootstrap", params)

    async def create_dashboard_access_link(
        self,
        *,
        agent_id: Optional[str] = None,
        name: Optional[str] = None,
        link_type: str = "private",
        path: str = "/",
        expires_seconds: Optional[int] = None,
        force_new: bool = False,
    ) -> Dict[str, Any]:
        """创建 Dashboard 短链接。"""
        params: Dict[str, Any] = {
            "LinkType": link_type,
            "Path": path,
            "ForceNew": bool(force_new),
        }
        if expires_seconds is not None:
            params["ExpiresSeconds"] = int(expires_seconds)
        if agent_id:
            params["AgentId"] = agent_id
        if name:
            params["Name"] = name
        return self._action("CreateDashboardAccessLink", params)

    async def get_client_bootstrap_config(
        self,
        *,
        product: Optional[str] = None,
        framework: Optional[str] = None,
        region: Optional[str] = None,
        client_type: str = "cli",
        client_version: Optional[str] = None,
        locale: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取客户端启动配置（动态默认值/升级提示/公告）。"""
        params: Dict[str, Any] = {
            "ClientType": client_type or "cli",
        }
        if product:
            params["Product"] = product
        if framework:
            params["Framework"] = framework
        if region:
            params["Region"] = region
        if client_version:
            params["ClientVersion"] = client_version
        if locale:
            params["Locale"] = locale
        return self._action("GetClientBootstrapConfig", params)

    async def list_dashboard_access_links(
        self,
        *,
        agent_id: Optional[str] = None,
        name: Optional[str] = None,
        link_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """列出 Dashboard 短链接。"""
        params: Dict[str, Any] = {
            "Page": int(page),
            "Size": int(size),
        }
        if agent_id:
            params["AgentId"] = agent_id
        if name:
            params["Name"] = name
        if link_type:
            params["LinkType"] = link_type
        if status:
            params["Status"] = status
        return self._action("ListDashboardAccessLinks", params)

    async def delete_dashboard_access_link(self, *, link_id: str) -> Dict[str, Any]:
        """删除 Dashboard 短链接。"""
        return self._action("DeleteDashboardAccessLink", {"LinkId": link_id})
        
    async def list_agents(
        self,
        region: Optional[str] = None,
        framework: Optional[str | Sequence[str]] = None,
        status: Optional[str] = None,
        name: Optional[str] = None,
        agent_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """列出 Agents"""
        params = {
            "Page": int(page),
            "PageSize": int(page_size),
            "Region": self._normalize_payload_region(region),
        }
        normalized_framework = self._normalize_framework_filters(framework)
        if normalized_framework:
            params["Framework"] = normalized_framework
        if status:
            params["Status"] = status
        if name:
            params["Name"] = name
        if agent_id:
            params["Id"] = agent_id
        return self._action("ListAgents", params)

    async def get_agent_logs(
        self,
        *,
        agent_id: str,
        instance: Optional[str] = None,
        log_type: str = "Stdout",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """获取 Agent 日志。"""
        normalized_log_type = (log_type or "Stdout").strip()
        if normalized_log_type.lower() == "stdout":
            normalized_log_type = "Stdout"
        elif normalized_log_type.lower() == "log":
            normalized_log_type = "Log"

        params: Dict[str, Any] = {
            "AgentId": agent_id,
            "LogType": normalized_log_type,
            "Page": int(page),
            "PageSize": int(page_size),
        }
        if instance:
            params["Instance"] = instance
        if start_time is not None:
            params["StartTime"] = int(start_time)
        if end_time is not None:
            params["EndTime"] = int(end_time)
        if keyword:
            params["Keyword"] = keyword
        return self._action("GetAgentLogs", params)

    async def run_openclaw_repair(
        self,
        agent_id: str,
        *,
        repair_action: str = "doctor-fix",
    ) -> Dict[str, Any]:
        """在控制面触发 OpenClaw runtime 修复动作。"""
        return self._action(
            "RunOpenClawRepair",
            {
                "AgentId": agent_id,
                "RepairAction": repair_action,
            },
        )

    async def delete_agent(self, agent_id: str) -> bool:
        """删除 Agent"""
        self._action("DeleteAgent", {"AgentId": agent_id})
        return True



    async def update_agent(self, agent_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新 Agent (热更新)"""
        params = {"AgentId": agent_id}
        if data.get("description"):
            params["Description"] = data["description"]
            
        if data.get("artifact_path"):
            artifact = (data.get("artifact_path", "") or "").strip()
            if (data.get("artifact_type") or "").lower() == "container":
                ic = data.get("image_credential", {}) or {}
                image_username = (ic.get("username") or "").strip()
                image_password = (ic.get("password") or "").strip()
                img_ns, img_repo, img_ver = self._parse_container_image_ref(artifact)
                container_config = {
                    "ImageType": "Personal",
                    "NameSpace": img_ns,
                    "ImageRepo": img_repo,
                    "ImageVersion": img_ver,
                    "ImageAddr": artifact,
                }
                if image_username and image_password:
                    container_config["UserName"] = image_username
                    container_config["Password"] = image_password
                params["ContainerConfig"] = container_config
            else:
                ks3 = data.get("ks3", {})
                params["CodeConfig"] = {
                    "Path": artifact,
                    "AccessKey": ks3.get("access_key"),
                    "SecretKey": ks3.get("secret_key"),
                    "Region": self._normalize_payload_region(ks3.get("region", "cn-beijing-6")),
                    "Bucket": ks3.get("bucket"),
                }
            
        if data.get("resources"):
            params["Resource"] = {
                "Cpu": int(float(data["resources"].get("cpu", 2))),
                "Memory": int(str(data["resources"].get("memory", "4Gi")).replace("Gi", ""))
            }
            
        if data.get("scaling"):
            params["Scaling"] = {
                "MinReplicas": int(data["scaling"].get("min_replicas", 1)),
                "MaxReplicas": int(data["scaling"].get("max_replicas", 10)),
                "Concurrency": int(data["scaling"].get("concurrency", 20)),
            }
            
        envs = data.get("env_vars") or data.get("environment_variables")
        if envs:
            env_vars = []
            if isinstance(envs, dict):
                for k, v in envs.items():
                    env_vars.append({"Key": k, "Value": str(v), "IsSensitive": False})
            elif isinstance(envs, list):
                env_vars = envs
            params["EnvironmentVariables"] = env_vars

        network_payload = self._normalize_network_payload(data.get("network"))
        if network_payload:
            params["Network"] = network_payload

        ui_config_payload = self._normalize_ui_config_payload(data.get("ui_config"))
        if ui_config_payload is not None:
            params["UiConfig"] = ui_config_payload

        storage_payload = self._normalize_storage_payload(data.get("storage"))
        if storage_payload is not None:
            params["Storage"] = storage_payload

        memory_config_payload = self._normalize_memory_config_payload(data.get("memory_config"))
        if memory_config_payload is not None:
            params["MemoryConfig"] = memory_config_payload

        # 访问控制 (可选)
        auth_type = data.get("auth_type")
        if auth_type:
            params["Access"] = {
                "AuthType": auth_type,
                "IamRole": data.get("iam_role", "KsyunAgentEngineDefaultRole"),
            }

        # 高级配置 (可选)
        advanced = {}
        observability = data.get("observability")
        if isinstance(observability, dict) and "langfuse_enabled" in observability:
            advanced["EnableObservability"] = bool(observability.get("langfuse_enabled"))
        elif "enable_observability" in data:
            advanced["EnableObservability"] = bool(data.get("enable_observability"))
        inbound_identity_auth = data.get("inbound_identity_auth")
        if inbound_identity_auth is not None:
            advanced["InboundIdentityAuth"] = inbound_identity_auth
        project_id = data.get("project_id")
        if project_id:
            advanced["ProjectId"] = project_id
        if advanced:
            params["Advanced"] = advanced
            
        return self._action("UpdateAgent", params)

    # ===== Session Actions =====
    
    async def create_session(self, agent_id: str, user_id: Optional[str] = None, expires_hours: int = 24) -> Dict[str, Any]:
        """创建会话"""
        return self._action("CreateSession", {
            "AgentId": agent_id,
            "UserId": user_id,
            "ExpiresHours": expires_hours
        })
    
    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """获取会话详情"""
        return self._action("GetSession", {"Id": session_id})
    
    async def list_sessions(self, agent_id: str, page: int = 1, size: int = 20) -> Dict[str, Any]:
        """列出会话"""
        return self._action("ListSessions", {"AgentId": agent_id, "Page": page, "Size": size})
    
    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        try:
            self._action("DeleteSession", {"Id": session_id})
            return True
        except Exception:
            return False

    async def list_workspace_files(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        path: str = ".",
        recursive: bool = False,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        if transport["mode"] == "action":
            return self._annotate_workspace_payload(
                self._action(
                    "ListWorkspaceFiles",
                    self._compact_params(
                        {
                            "AgentId": transport.get("agent_id"),
                            "Name": transport.get("name"),
                            "Path": path,
                            "Recursive": recursive,
                        }
                    ),
                ),
                transport_mode="action_proxy",
            )
        access = transport["access"]
        response = self._workspace_runtime_request(
            access=access,
            method="GET",
            path="entries",
            params={"path": path, "recursive": str(bool(recursive)).lower()},
        )
        return self._annotate_workspace_payload(
            self._workspace_runtime_json(response),
            transport_mode="runtime_direct",
        )

    async def get_workspace_health(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        if transport["mode"] == "action":
            return {}
        access = transport["access"]
        response = self._workspace_runtime_request(
            access=access,
            method="GET",
            path="healthz",
        )
        return self._annotate_workspace_payload(
            self._workspace_runtime_json(response),
            transport_mode="runtime_direct",
        )

    async def upload_workspace_file(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        remote_path: str,
        local_path: str | Path,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        file_path = Path(local_path)
        file_bytes = file_path.read_bytes()
        guessed_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if transport["mode"] == "action":
            response = self._action_raw_request(
                "POST",
                "AddWorkspaceFile",
                data={
                    "AgentId": transport.get("agent_id"),
                    "Name": transport.get("name"),
                    "Path": remote_path,
                },
                files={
                    "file": (
                        file_path.name,
                        file_bytes,
                        guessed_type,
                    )
                },
            )
            return self._annotate_workspace_payload(
                self._workspace_runtime_json(response),
                transport_mode="action_proxy",
            )
        access = transport["access"]
        response = self._workspace_runtime_request(
            access=access,
            method="POST",
            path=f"files/{self._encode_workspace_runtime_path(remote_path)}",
            files={
                "file": (
                    file_path.name,
                    file_bytes,
                    guessed_type,
                )
            },
        )
        return self._annotate_workspace_payload(
            self._workspace_runtime_json(response),
            transport_mode="runtime_direct",
        )

    async def download_workspace_file(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        remote_path: str,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> bytes:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        if transport["mode"] == "action":
            response = self._action_raw_request(
                "GET",
                "GetWorkspaceFileContent",
                params={
                    "AgentId": transport.get("agent_id"),
                    "Name": transport.get("name"),
                    "FilePath": remote_path,
                },
                accept="application/octet-stream",
            )
            return response.content
        access = transport["access"]
        response = self._workspace_runtime_request(
            access=access,
            method="GET",
            path=f"files/{self._encode_workspace_runtime_path(remote_path)}",
        )
        return response.content

    async def delete_workspace_file(
        self,
        *,
        agent_id: str | None = None,
        name: str | None = None,
        remote_path: str,
        endpoint: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        transport = await self._resolve_workspace_transport(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint,
            api_key=api_key,
        )
        if transport["mode"] == "action":
            return self._annotate_workspace_payload(
                self._action(
                    "DeleteWorkspaceFile",
                    self._compact_params(
                        {
                            "AgentId": transport.get("agent_id"),
                            "Name": transport.get("name"),
                            "Path": remote_path,
                        }
                    ),
                ),
                transport_mode="action_proxy",
            )
        access = transport["access"]
        response = self._workspace_runtime_request(
            access=access,
            method="DELETE",
            path=f"files/{self._encode_workspace_runtime_path(remote_path)}",
        )
        return self._annotate_workspace_payload(
            self._workspace_runtime_json(response),
            transport_mode="runtime_direct",
        )

    # ===== MCP Actions =====

    @staticmethod
    def _looks_like_nested_mcp_payload(data: Dict[str, Any]) -> bool:
        return any(
            key in data
            for key in (
                "DeploymentType",
                "CodeConfig",
                "ContainerConfig",
                "Resource",
                "Scaling",
                "Access",
                "Advanced",
                "Network",
            )
        )

    @staticmethod
    def _normalize_mcp_memory(memory: Any, default: int = 2) -> int:
        raw = str(memory or "").strip()
        if not raw:
            return default
        lowered = raw.lower()
        if lowered.endswith("gi"):
            raw = raw[:-2]
        return int(float(raw))

    @staticmethod
    def _normalize_mcp_deployment_type(value: Optional[str], default: Optional[str] = None) -> Optional[str]:
        raw = str(value or default or "").strip()
        if not raw:
            return None
        return "Container" if raw.lower() == "container" else "Code"

    def _infer_mcp_deployment_type(self, data: Dict[str, Any], default: Optional[str] = None) -> Optional[str]:
        explicit = self._normalize_mcp_deployment_type(
            data.get("deployment_type") or data.get("artifact_type"),
            default=default,
        )
        if explicit:
            return explicit
        if data.get("container_config") or data.get("image_credential"):
            return "Container"
        artifact_path = str(data.get("artifact_path") or "").strip()
        if artifact_path.startswith("ks3://") or data.get("ks3"):
            return "Code"
        return default

    def _build_mcp_code_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ks3 = data.get("ks3") or {}
        code_config: Dict[str, Any] = {
            "Path": str(data.get("artifact_path") or ""),
        }
        if ks3.get("access_key") is not None:
            code_config["AccessKey"] = ks3.get("access_key")
        if ks3.get("secret_key") is not None:
            code_config["SecretKey"] = ks3.get("secret_key")
        if ks3.get("region") is not None or data.get("region") is not None:
            code_config["Region"] = self._normalize_payload_region(
                ks3.get("region") or data.get("region") or self.logical_region
            )
        if ks3.get("bucket") is not None:
            code_config["Bucket"] = ks3.get("bucket")
        return code_config

    def _build_mcp_container_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        container = data.get("container_config") or {}
        artifact = str(data.get("artifact_path") or container.get("image_addr") or "").strip()
        ic = data.get("image_credential", {}) or {}
        img_ns, img_repo, img_ver = self._parse_container_image_ref(artifact)

        image_type = container.get("image_type") or "Personal"
        params: Dict[str, Any] = {
            "ImageType": image_type,
            "NameSpace": container.get("name_space") or img_ns,
            "ImageRepo": container.get("image_repo") or img_repo,
            "ImageVersion": container.get("image_version") or img_ver,
            "ImageAddr": container.get("image_addr") or artifact,
        }
        if container.get("enterprise_instance"):
            params["EnterpriseInstance"] = container.get("enterprise_instance")
        if container.get("enterprise_instance_id"):
            params["EnterpriseInstanceId"] = container.get("enterprise_instance_id")

        username = (container.get("username") or ic.get("username") or "").strip()
        password = (container.get("password") or ic.get("password") or "").strip()
        if username and password:
            params["UserName"] = username
            params["Password"] = password
        return params

    def _build_mcp_resource(self, data: Dict[str, Any]) -> Dict[str, Any]:
        resources = data.get("resources", {}) or {}
        return {
            "Cpu": int(float(resources.get("cpu", 1))),
            "Memory": self._normalize_mcp_memory(resources.get("memory", "2Gi"), default=2),
        }

    @staticmethod
    def _build_mcp_scaling(data: Dict[str, Any]) -> Dict[str, Any]:
        scaling = data.get("scaling", {}) or {}
        return {
            "MinReplicas": int(scaling.get("min_replicas", 1)),
            "MaxReplicas": int(scaling.get("max_replicas", 5)),
            "QpsPerInstance": int(scaling.get("concurrency", 20)),
        }

    @staticmethod
    def _build_mcp_advanced(data: Dict[str, Any]) -> Dict[str, Any]:
        metadata = data.get("metadata", {}) or {}
        return {
            "McpVariable": metadata.get("mcp_variable", "mcp"),
            "Tools": metadata.get("tools", []) or [],
        }

    async def create_mcp(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建 MCP"""
        if self._looks_like_nested_mcp_payload(data):
            params = dict(data)
            params["Region"] = self._normalize_payload_region(
                params.get("Region") or data.get("region") or self.logical_region or "cn-beijing-6"
            )
            return self._action("CreateMCP", params)

        deployment_type = self._infer_mcp_deployment_type(data, default="Code") or "Code"
        params: Dict[str, Any] = {
            "Name": data.get("name"),
            "Description": data.get("description"),
            "Region": self._normalize_payload_region(data.get("region", "cn-beijing-6")),
            "DeploymentType": deployment_type,
            "Resource": self._build_mcp_resource(data),
            "Scaling": self._build_mcp_scaling(data),
            "Access": {"AuthType": "ApiKey" if data.get("enable_auth") else "None"},
            "Advanced": self._build_mcp_advanced(data),
        }
        if deployment_type == "Container":
            params["ContainerConfig"] = self._build_mcp_container_config(data)
        else:
            params["CodeConfig"] = self._build_mcp_code_config(data)
        network_payload = self._normalize_network_payload(data.get("network"))
        if network_payload:
            params["Network"] = network_payload
        return self._action("CreateMCP", params)
    
    async def get_mcp(self, mcp_id: str) -> Dict[str, Any]:
        """获取 MCP 详情"""
        return self._action("GetMCP", {"Id": mcp_id})
    
    async def list_mcps(
        self,
        region: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """列出 MCP (注册中心)"""
        params = {"Page": int(page), "PageSize": int(page_size)}
        if region:
            params["Region"] = self._normalize_payload_region(region)
        result = self._action("ListMCPs", params)
        # _action 已统一转 snake_case: "MCPs" -> "m_c_ps"... 
        # 实际上 MCPs -> mcps, Total -> total
        return {"mcps": result.get("mcps", []), "total": result.get("total", 0)}
    
    async def update_mcp(self, mcp_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新 MCP (热更新)"""
        if self._looks_like_nested_mcp_payload(data):
            params = dict(data)
            params.setdefault("Id", mcp_id)
            if "Region" in params or data.get("region") or self.logical_region:
                params["Region"] = self._normalize_payload_region(
                    params.get("Region") or data.get("region") or self.logical_region
                )
            return self._action("UpdateMCP", params)

        params: Dict[str, Any] = {"Id": mcp_id}
        deployment_type = self._infer_mcp_deployment_type(data)

        if data.get("description") is not None:
            params["Description"] = data["description"]
        if deployment_type:
            params["DeploymentType"] = deployment_type

        if data.get("artifact_path"):
            if deployment_type == "Container":
                params["ContainerConfig"] = self._build_mcp_container_config(data)
            elif deployment_type == "Code":
                params["CodeConfig"] = self._build_mcp_code_config(data)

        resources = data.get("resources", {}) or {}
        if any(resources.get(key) is not None for key in ("cpu", "memory")):
            params["Resource"] = {
                "Cpu": int(float(resources.get("cpu", 1))),
                "Memory": self._normalize_mcp_memory(resources.get("memory", "2Gi"), default=2),
            }

        scaling = data.get("scaling", {}) or {}
        if any(scaling.get(key) is not None for key in ("min_replicas", "max_replicas", "concurrency")):
            params["Scaling"] = {
                "MinReplicas": int(scaling.get("min_replicas", 1)),
                "MaxReplicas": int(scaling.get("max_replicas", 5)),
                "QpsPerInstance": int(scaling.get("concurrency", 20)),
            }

        if data.get("enable_auth") is not None:
            params["Access"] = {"AuthType": "ApiKey" if data.get("enable_auth") else "None"}

        metadata = data.get("metadata", {}) or {}
        if metadata.get("mcp_variable") is not None or metadata.get("tools") is not None:
            params["Advanced"] = {
                "McpVariable": metadata.get("mcp_variable"),
                "Tools": metadata.get("tools"),
            }

        network_payload = self._normalize_network_payload(data.get("network"))
        if network_payload:
            params["Network"] = network_payload
        if data.get("region") is not None:
            params["Region"] = self._normalize_payload_region(data.get("region"))
        return self._action("UpdateMCP", params)
    
    async def delete_mcp(self, mcp_id: str) -> bool:
        """删除 MCP"""
        try:
            self._action("DeleteMCP", {"Id": mcp_id})
            return True
        except Exception:
            return False
    
    async def get_mcp_by_name(self, name: str, region: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """按名称查询 MCP"""
        # 使用 ListMCPs 然后过滤 (Action API 暂不支持 by-name)
        try:
            page = 1
            page_size = 100
            while True:
                result = await self.list_mcps(region=region, page=page, page_size=page_size)
                mcps = result.get("mcps", [])
                for mcp in mcps:
                    if mcp.get("name") == name:
                        return mcp
                if len(mcps) < page_size:
                    break
                page += 1
            return None
        except Exception:
            return None

    # ===== Upload Actions =====
    
    async def get_presigned_url(self, filename: str) -> Dict[str, Any]:
        """获取 KS3 预签名上传 URL"""
        return self._action("GetPresignedUrl", {"Filename": filename})

    # ===== Chat Actions =====
    
    async def chat(self, agent_id: str, message: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """调用 Agent"""
        params = {
            "AgentId": agent_id,
            "Messages": [{"role": "user", "content": message}],
            "Stream": False
        }
        if session_id:
            params["SessionId"] = session_id
        return self._action("RunAgent", params)

    # ===== Version Actions =====
    
    async def release_version(
        self, 
        agent_id: str, 
        tag: Optional[str] = None, 
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """发布新版本
        
        Args:
            agent_id: Agent ID
            tag: 版本标签，不填则自动生成
            description: 版本描述
            
        Returns:
            版本信息
        """
        params = {"AgentId": agent_id}
        if tag:
            params["Tag"] = tag
        if description:
            params["Description"] = description
        return self._action("CreateVersion", params)
    
    async def list_versions(
        self, 
        agent_id: str, 
        page: int = 1, 
        size: int = 10
    ) -> Dict[str, Any]:
        """列出版本历史
        
        Args:
            agent_id: Agent ID
            page: 页码
            size: 每页数量
            
        Returns:
            版本列表和分页信息
        """
        return self._action("ListVersions", {
            "AgentId": agent_id,
            "Page": page,
            "PageSize": size
        })
    
    async def rollback_version(
        self, 
        agent_id: str, 
        target_version_id: Optional[str] = None,
        target_tag: Optional[str] = None,
        ks3_access_key: Optional[str] = None,
        ks3_secret_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """回滚到指定版本
        
        Args:
            agent_id: Agent ID
            target_version_id: 目标版本 ID（与 target_tag 二选一）
            target_tag: 目标版本标签（与 target_version_id 二选一）
            ks3_access_key: KS3 Access Key (可选)
            ks3_secret_key: KS3 Secret Key (可选)
            
        Returns:
            回滚结果
        """
        params = {"AgentId": agent_id}
        if target_version_id:
            params["TargetVersionId"] = target_version_id
        if target_tag:
            params["TargetTag"] = target_tag
        if ks3_access_key:
            params["KS3AccessKey"] = ks3_access_key
        if ks3_secret_key:
            params["KS3SecretKey"] = ks3_secret_key
            
        return self._action("RollbackVersion", params)
