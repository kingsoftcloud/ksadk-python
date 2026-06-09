from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import requests

from ksadk.common.auth import AWSV4Auth

from ksadk.skills.models import SkillListResponse, SkillRef


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
    ):
        self.base_url = _normalize_base_url(base_url)
        self.token = token
        self.access_key = access_key or _env("KSADK_SKILL_SERVICE_ACCESS_KEY", "KSYUN_ACCESS_KEY", "KS3_ACCESS_KEY")
        self.secret_key = secret_key or _env("KSADK_SKILL_SERVICE_SECRET_KEY", "KSYUN_SECRET_KEY", "KS3_SECRET_KEY")
        self.account_id = account_id or _env("KSADK_SKILL_SERVICE_ACCOUNT_ID", "KSYUN_ACCOUNT_ID")
        self.region = region or _env("KSADK_SKILL_SERVICE_REGION", "KSYUN_REGION") or "cn-beijing-6"
        self.api_version = api_version or os.environ.get("KSADK_SKILL_SERVICE_API_VERSION", "2024-06-12")
        self.sign_service = sign_service or os.environ.get("KSADK_SKILL_SERVICE_SIGN_SERVICE", "aicp")
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout
        self.transport = transport
        self._requests_session: requests.Session | None = None
        self._auth = AWSV4Auth(
            access_key_id=self.access_key,
            secret_access_key=self.secret_key,
            region=self.region,
            service=self.sign_service,
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

    def list_skills_by_space_id(self, space_id: str) -> SkillListResponse:
        if self._is_kop_mode():
            payload = self._get_json(
                "ListSkillsBySpaceId",
                {"SpaceId": space_id, "PageNumber": 1, "PageSize": 100},
            )
        else:
            payload = self._get_json("ListSkillsBySpaceId", {"SpaceId": space_id})
        return SkillListResponse.from_payload(payload, space_id=space_id)

    def list_available_premade_skills(self) -> SkillListResponse:
        payload = self._get_json("ListAvailablePremadeSkills", {})
        return SkillListResponse.from_payload(payload, space_id="public", space_name="Public Skills")

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

    def download_skill_archive(self, skill: SkillRef) -> bytes:
        download_url = self.get_skill_download_url(skill)
        if not download_url:
            raise ValueError(f"Skill Service did not return DownloadUrl for {skill.skill_id}")
        with httpx.Client(**self._client_kwargs()) as client:
            response = client.get(download_url)
            response.raise_for_status()
            return response.content

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
        response = self._requests().get(
            self._kop_base_url() + "/",
            params=query,
            headers=headers,
            auth=self._auth.get_auth(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
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
        return self._requests_session

    def _is_kop_mode(self) -> bool:
        host = urlsplit(self.base_url).netloc.lower()
        return (
            host.endswith("aicp.inner.api.ksyun.com")
            or host.endswith("aicp.internal.api.ksyun.com")
            or host.endswith("aicp.api.ksyun.com")
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


def _normalize_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/openapi.json"):
        path = path[: -len("/openapi.json")]
    elif path.endswith("/docs"):
        path = path[: -len("/docs")] + "/api/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
