from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import requests

from ksadk.common.auth import AWSV4Auth
from ksadk.skills.models import SkillListResponse, SkillRef

_KOP_HOSTS = frozenset(
    {
        "aicp.inner.api.ksyun.com",
        "aicp.internal.api.ksyun.com",
        "aicp.api.ksyun.com",
    }
)
_PRIVATE_KOP_HOSTS = frozenset(
    {
        "aicp.inner.api.ksyun.com",
        "aicp.internal.api.ksyun.com",
    }
)


class SkillServiceClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        access_key: str = "",
        secret_key: str = "",
        account_id: str = "",
        region: str = "",
        api_version: str = "",
        sign_service: str = "",
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        allow_env_fallback: bool = True,
    ):
        self.allow_env_fallback = allow_env_fallback
        resolve_env = _env if allow_env_fallback else lambda *names: ""
        self.base_url = _normalize_base_url(base_url)
        self.token = token
        self.access_key = access_key or resolve_env(
            "KSADK_SKILL_SERVICE_ACCESS_KEY", "KSYUN_ACCESS_KEY", "KS3_ACCESS_KEY"
        )
        self.secret_key = secret_key or resolve_env(
            "KSADK_SKILL_SERVICE_SECRET_KEY", "KSYUN_SECRET_KEY", "KS3_SECRET_KEY"
        )
        self.account_id = account_id or resolve_env(
            "KSADK_SKILL_SERVICE_ACCOUNT_ID", "KSYUN_ACCOUNT_ID"
        )
        self.logical_region = (
            region or resolve_env("KSADK_SKILL_SERVICE_REGION", "KSYUN_REGION") or "cn-beijing-6"
        )
        if not allow_env_fallback:
            if not region or region.strip().lower() == "pre-online":
                raise ValueError("Explicit Skill clients require a concrete region")
            if token and (access_key or secret_key):
                raise ValueError("Explicit Skill clients cannot mix token and signing credentials")
            if bool(access_key) != bool(secret_key):
                raise ValueError("Both signing credentials are required")
        self.region = (
            _normalize_control_region(self.logical_region)
            if allow_env_fallback
            else self.logical_region.strip()
        )
        self.custom_source = (
            _resolve_custom_source(self.logical_region) if allow_env_fallback else ""
        )
        self.api_version = (
            api_version or resolve_env("KSADK_SKILL_SERVICE_API_VERSION") or "2024-06-12"
        )
        self.sign_service = (
            sign_service or resolve_env("KSADK_SKILL_SERVICE_SIGN_SERVICE") or "aicp"
        )
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout
        self.transport = transport
        self._requests_session: requests.Session | None = None
        self._auth = AWSV4Auth(
            access_key_id=self.access_key,
            secret_access_key=self.secret_key,
            region=self.region,
            service=self.sign_service,
            allow_env_fallback=allow_env_fallback,
        )

    def action_url(self, action: str) -> str:
        if self._is_kop_mode():
            return (
                f"{self._kop_base_url()}/?Action={quote(action.lstrip('/'))}"
                f"&Version={quote(self.api_version)}"
            )
        return f"{self.base_url}/{action.lstrip('/')}"

    def list_skill_spaces(self, *, page_number: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self._get_json(
            "ListSkillSpaces",
            {"PageNumber": page_number, "PageSize": page_size},
        )

    def list_skills_by_space_id(self, space_id: str, *, max_pages: int = 20) -> SkillListResponse:
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise ValueError("Skill directory page budget must be 1..1000")
        if not self._is_kop_mode():
            # Legacy REST endpoints do not advertise a paging contract. Preserve
            # their request shape and report known incompleteness explicitly.
            payload = self._get_json("ListSkillsBySpaceId", {"SpaceId": space_id})
            result = SkillListResponse.from_payload(payload, space_id=space_id)
            if result.space_id != space_id:
                raise ValueError("Skill directory does not match the requested space")
            if result.total_count is not None and result.total_count > len(result.skills):
                return replace(result, truncated=True, pagination_warning="pagination_unavailable")
            return result
        skills = []
        seen = set()
        total = None
        for page_number in range(1, max_pages + 1):
            result = self.list_skills_page(space_id, page_number=page_number)
            if page_number > 1 and result.total_count != total:
                return replace(
                    result,
                    skills=skills,
                    truncated=True,
                    next_page=None,
                    pagination_warning="directory_changed",
                )
            total = result.total_count
            identities = [(skill.skill_id, skill.version_id) for skill in result.skills]
            if (
                any(not identity[0] for identity in identities)
                or len(set(identities)) != len(identities)
                or any(identity in seen for identity in identities)
            ):
                return replace(
                    result,
                    skills=skills,
                    truncated=True,
                    next_page=None,
                    pagination_warning="pagination_repeated",
                )
            seen.update(identities)
            skills.extend(result.skills)
            if total is not None and len(skills) > total:
                return replace(
                    result,
                    skills=skills,
                    truncated=True,
                    next_page=None,
                    pagination_warning="directory_changed",
                )
            if total is not None and len(skills) == total:
                return replace(result, skills=skills, truncated=False, next_page=None)
            if len(result.skills) < 100:
                incomplete = total is not None and len(skills) < total
                return replace(
                    result,
                    skills=skills,
                    truncated=incomplete,
                    next_page=None,
                    pagination_warning="directory_incomplete" if incomplete else "",
                )
        return replace(
            result,
            skills=skills,
            truncated=True,
            next_page=max_pages + 1,
            pagination_warning="page_budget_exhausted",
        )

    def list_skills_page(
        self, space_id: str, *, page_number: int = 1, page_size: int = 100
    ) -> SkillListResponse:
        if not self._is_kop_mode():
            raise ValueError("This Skill endpoint does not advertise pagination")
        if (
            type(page_number) is not int
            or page_number < 1
            or type(page_size) is not int
            or not 1 <= page_size <= 100
        ):
            raise ValueError("Invalid Skill directory page parameters")
        payload = self._get_json(
            "ListSkillsBySpaceId",
            {
                "SpaceId": space_id,
                "PageNumber": page_number,
                "PageSize": page_size,
            },
        )
        result = SkillListResponse.from_payload(payload, space_id=space_id)
        if result.space_id != space_id or len(result.skills) > page_size:
            raise ValueError("Skill directory page does not match the requested space or limit")
        more = (
            page_number * page_size < result.total_count
            if result.total_count is not None
            else len(result.skills) == page_size
        )
        return replace(result, truncated=more, next_page=page_number + 1 if more else None)

    def list_available_premade_skills(self) -> SkillListResponse:
        payload = self._get_json("ListAvailablePremadeSkills", {})
        result = SkillListResponse.from_payload(
            payload, space_id="public", space_name="Public Skills"
        )
        if result.total_count is not None and result.total_count > len(result.skills):
            return replace(result, truncated=True, pagination_warning="pagination_unavailable")
        return result

    def get_skill_download_url(self, skill: SkillRef) -> str:
        action = "GetSkillDownloadUrl" if skill.version_id else "GetPremadeSkillDownloadUrl"
        payload = self._get_json(
            action,
            {
                "SkillId": skill.skill_id,
                "VersionId": skill.version_id,
            },
        )
        data = payload.get("Data") or payload.get("data") or {}
        return str(data.get("DownloadUrl") or data.get("download_url") or "")

    def download_skill_archive(
        self, skill: SkillRef, *, max_bytes: int = 20 * 1024 * 1024
    ) -> bytes:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Skill archive size limit must be a positive integer")
        download_url = self.get_skill_download_url(skill)
        if not download_url:
            raise ValueError(f"Skill Service did not return DownloadUrl for {skill.skill_id}")
        if self.allow_env_fallback:
            download_url = _rewrite_ks3_to_internal(download_url)
        try:
            with httpx.Client(**self._client_kwargs()) as client:
                with client.stream("GET", download_url) as response:
                    response.raise_for_status()
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise ValueError("Skill archive exceeds download limit")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if len(content) + len(chunk) > max_bytes:
                            raise ValueError("Skill archive exceeds download limit")
                        content.extend(chunk)
                    return bytes(content)
        except httpx.HTTPError:
            # Download URLs can carry temporary credentials; do not expose the
            # HTTP exception's URL through logs or the consumer's tool result.
            raise ValueError("Skill archive download failed") from None

    def _get_json(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._is_kop_mode():
            return self._get_json_kop(action, params)
        with httpx.Client(**self._client_kwargs()) as client:
            response = client.get(self.action_url(action), params=params, headers=self._headers())
            response.raise_for_status()
            return self._decode_response(action, response)

    def _post_json(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(**self._client_kwargs()) as client:
            response = client.post(self.action_url(action), json=payload, headers=self._headers())
            response.raise_for_status()
            return self._decode_response(action, response)

    def _decode_response(self, action: str, response: httpx.Response) -> dict[str, Any]:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Skill Service returned non-object response for {action}")
        code = data.get("Code", data.get("code"))
        if code not in (None, 0, 200, "0", "200"):
            request_id = str(data.get("RequestId") or data.get("request_id") or "")
            message = str(data.get("Message") or data.get("message") or "")
            raise ValueError(
                f"Skill Service {action} failed: code={code}, "
                f"request_id={request_id}, message={message}"
            )
        return data

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if not self.allow_env_fallback:
            kwargs["trust_env"] = False
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return kwargs

    def _headers(self, *, action: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.account_id:
            headers["X-Ksc-Account-Id"] = self.account_id
        if self._is_kop_mode():
            headers.update(
                {
                    "Host": urlsplit(self._kop_base_url()).netloc,
                    "X-Ksc-Region": self.region,
                    "X-Ksc-Source": "ksadk-skill-runtime",
                }
            )
            if self.custom_source:
                headers["X-KSC-CUSTOM-SOURCE"] = self.custom_source
            if action:
                headers["X-Action"] = action
                headers["X-Version"] = self.api_version
        headers.update(self.extra_headers)
        return headers

    def _get_json_kop(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {"Action": action, "Version": self.api_version, **params}
        headers = self._headers(action=action)
        if self.transport is not None:
            with httpx.Client(**self._client_kwargs()) as client:
                response = client.get(self._kop_base_url() + "/", params=query, headers=headers)
                response.raise_for_status()
                return self._decode_response(action, response)
        if not self._auth.is_enabled:
            raise ValueError(
                "AICP Skill Service endpoint requires signing credentials. "
                "Set KSADK_SKILL_SERVICE_ACCESS_KEY/KSADK_SKILL_SERVICE_SECRET_KEY "
                "or KSYUN_ACCESS_KEY/KSYUN_SECRET_KEY."
            )
        requests_response = self._requests().get(
            self._kop_base_url() + "/",
            params=query,
            headers=headers,
            auth=self._auth.get_auth(),
            timeout=self.timeout,
            allow_redirects=False,
        )
        requests_response.raise_for_status()
        data = requests_response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Skill Service returned non-object response for {action}")
        code = data.get("Code", data.get("code"))
        if code not in (None, 0, 200, "0", "200"):
            request_id = str(data.get("RequestId") or data.get("request_id") or "")
            message = str(data.get("Message") or data.get("message") or data.get("Error") or "")
            raise ValueError(
                f"Skill Service {action} failed: code={code}, "
                f"request_id={request_id}, message={message}"
            )
        return data

    def _requests(self) -> requests.Session:
        if self._requests_session is None:
            self._requests_session = requests.Session()
            if not self.allow_env_fallback:
                self._requests_session.trust_env = False
        return self._requests_session

    def close(self) -> None:
        """Release the signing session when its owning activation ends."""
        if self._requests_session is not None:
            self._requests_session.close()
            self._requests_session = None

    def _is_kop_mode(self) -> bool:
        """Match only exact AICP control-plane endpoints.

        A user-controlled URL such as ``aicp.api.ksyun.com.attacker.example``
        must never receive KOP signing headers. Public AICP requires TLS; the
        private ``inner`` and ``internal`` endpoints also support their existing
        in-cluster HTTP transport.
        """
        parsed = urlsplit(self.base_url)
        host = (parsed.hostname or "").lower()
        return host in _KOP_HOSTS and (
            parsed.scheme == "https" or (parsed.scheme == "http" and host in _PRIVATE_KOP_HOSTS)
        )

    def _kop_base_url(self) -> str:
        parsed = urlsplit(self.base_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _normalize_control_region(region: str) -> str:
    region = (region or "cn-beijing-6").strip()
    if region.lower() == "pre-online":
        return os.environ.get("AGENTENGINE_PRE_CONTROL_REGION", "cn-beijing-6")
    return region


def _resolve_custom_source(region: str) -> str:
    if (region or "").strip().lower() == "pre-online":
        return os.environ.get("AGENTENGINE_PRE_CUSTOM_SOURCE", "pre")
    return ""


def _normalize_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/openapi.json"):
        path = path[: -len("/openapi.json")]
    elif path.endswith("/docs"):
        path = path[: -len("/docs")] + "/api/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _rewrite_ks3_to_internal(url: str) -> str:
    """Rewrite a KS3 public endpoint to internal only when public is unreachable.

    AICP Skill Service returns pre-signed download URLs with public KS3 domains
    (e.g. skill.ks3-cn-beijing.ksyuncs.com -> 60.x public IP). On private_only
    compute pods those public IPs are unreachable. This probes the public domain
    first; if reachable keeps it, otherwise rewrites to the internal domain
    (ks3-cn-beijing-internal.ksyuncs.com -> 198.18.96.x).
    """
    if not url:
        return url
    try:
        from urllib.parse import urlsplit

        from ksadk.common.constants import get_ks3_endpoints

        region = os.environ.get("KSADK_SKILL_SERVICE_REGION", "KSYUN_REGION") or "cn-beijing-6"
        public_ep, internal_ep = get_ks3_endpoints(region)
        if not public_ep or not internal_ep:
            return url
        if public_ep not in url:
            return url

        # Public reachable => keep it.
        import socket

        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            s = socket.socket()
            s.settimeout(1.5)
            s.connect((host, port))
            s.close()
            return url
        except OSError:
            pass

        # Public unreachable => rewrite to internal.
        return url.replace(public_ep, internal_ep)
    except Exception:
        pass
    return url
