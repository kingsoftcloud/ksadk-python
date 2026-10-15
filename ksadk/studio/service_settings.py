"""Workspace settings and credential-safe Studio configuration updates."""

from __future__ import annotations

import os
from typing import Any

from ksadk.studio.codex_builder import (
    current_proxy_mode,
    normalize_proxy_mode,
    proxy_mode_env_value,
)
from ksadk.studio.configuration import SETTINGS_ENV
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_authority import (
    SignedKnowledgeResourceAuthority,
    resource_authority_policy_from_environment,
)


class StudioSettingsMixin:
    def get_settings(self) -> dict[str, Any]:
        data = self.configuration.settings()
        environment = self.configuration.environment()
        defaults = {
            "sandbox": environment.get("KSADK_CODEX_SANDBOX", "workspace-write-auto"),
            "buildAfterCreate": True,
            "codexProxy": current_proxy_mode(),
            "cloudRegion": environment.get(
                "AGENTENGINE_REGION", environment.get("KSYUN_REGION", "cn-beijing-6")
            ),
            "cloudServerUrl": environment.get("AGENTENGINE_SERVER_URL", ""),
            "cloudBucket": environment.get("KS3_BUCKET", ""),
            "cloudAccountId": (environment.get("KSYUN_ACCOUNT_ID", "").strip()),
            # AK/SK 不回显(避免本地 UI 回传 secret);只暴露配置状态。
            # 已配置 = 启动环境或已持久化 settings 里 AK+SK 齐全。
            "cloudAccountConfigured": bool(
                (
                    environment.get("KSYUN_ACCESS_KEY") or environment.get("KS3_ACCESS_KEY", "")
                ).strip()
                and (
                    environment.get("KSYUN_SECRET_KEY") or environment.get("KS3_SECRET_KEY", "")
                ).strip()
            ),
            "cloudSignedAccountConfigured": bool(
                (
                    environment.get("KSYUN_ACCESS_KEY") or environment.get("KS3_ACCESS_KEY", "")
                ).strip()
                and (
                    environment.get("KSYUN_SECRET_KEY") or environment.get("KS3_SECRET_KEY", "")
                ).strip()
            ),
            "traceContent": environment.get("KSADK_STUDIO_TRACE_CONTENT", "1") != "0",
        }
        defaults.update({k: v for k, v in data.items() if k in defaults and v is not None})
        for key, name in SETTINGS_ENV.items():
            if key in defaults:
                value, _source = self.configuration.resolve(name)
                if value is not None:
                    defaults[key] = value
        defaults["configurationSources"] = {
            key: self.configuration.resolve(name)[1] for key, name in SETTINGS_ENV.items()
        }
        defaults["platformResourceMissingFields"] = [
            key
            for key in ("cloudAccessKey", "cloudSecretKey", "cloudServerUrl")
            if not self.configuration.resolve(SETTINGS_ENV[key])[0]
        ]
        defaults["platformResourcesConfigured"] = self.resource_authority is not None
        defaults["codexProxy"] = normalize_proxy_mode(environment.get("KSADK_CODEX_USE_PROXY"))
        return defaults

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "sandbox",
            "buildAfterCreate",
            "codexProxy",
            "cloudRegion",
            "cloudBucket",
            "cloudServerUrl",
            "traceContent",
            # 云账号:AK/SK 留空 = 不修改(保留已存值);AccountID 可单独更新。
            "cloudAccessKey",
            "cloudSecretKey",
            "cloudAccountId",
        }
        data = {k: payload[k] for k in allowed if k in payload}
        if "cloudAccessKey" in data and not str(data["cloudAccessKey"]).strip():
            data.pop("cloudAccessKey")
        if "cloudSecretKey" in data and not str(data["cloudSecretKey"]).strip():
            data.pop("cloudSecretKey")
        if "cloudAccountId" in data:
            data["cloudAccountId"] = str(data["cloudAccountId"]).strip()
            if not data["cloudAccountId"]:
                data.pop("cloudAccountId")
        if data.get("sandbox") and data["sandbox"] not in {
            "read-only",
            "workspace-write",
            "workspace-write-auto",
            "full-access",
            "read_only",
            "workspace_write",
            "workspace_write_auto",
            "full_access",
        }:
            raise StudioError("SETTINGS_INVALID", "sandbox 取值非法", status_code=422)
        if "codexProxy" in data and data["codexProxy"] not in {"auto", "forced", "direct"}:
            raise StudioError("SETTINGS_INVALID", "codexProxy 取值非法", status_code=422)
        for key in (
            "cloudAccessKey",
            "cloudSecretKey",
            "cloudAccountId",
            "cloudRegion",
            "cloudBucket",
            "cloudServerUrl",
        ):
            if key in data:
                if (
                    not isinstance(data[key], str)
                    or len(data[key]) > 2048
                    or any(ord(c) < 32 for c in data[key])
                ):
                    raise StudioError(
                        "SETTINGS_INVALID", "配置值格式无效", status_code=422, field=key
                    )
                data[key] = data[key].strip()
        if data.get("cloudServerUrl"):
            from ksadk.studio.resource_authority import _canonical_endpoint

            try:
                data["cloudServerUrl"] = _canonical_endpoint(
                    data["cloudServerUrl"],
                    allow_loopback_http=False,
                    allow_ksyun_internal_http=True,
                )
            except ValueError as error:
                raise StudioError(
                    "SETTINGS_INVALID",
                    "控制面地址必须是有效的 HTTPS 服务地址",
                    status_code=422,
                    field="cloudServerUrl",
                ) from error
        if (
            "cloudAccessKey" in data
            and data["cloudAccessKey"] != self.configuration.resolve("KSYUN_ACCESS_KEY")[0]
        ):
            if not data.get("cloudSecretKey"):
                raise StudioError(
                    "SETTINGS_INVALID",
                    "切换 Access Key 时请同时填写对应的 Secret Key",
                    status_code=422,
                    field="cloudSecretKey",
                )
            data.setdefault("cloudAccountId", "")
        candidate_environment = self.configuration.environment()
        for key, name in SETTINGS_ENV.items():
            if key in data and name not in self.configuration.overrides:
                candidate_environment[name] = str(data[key])
        try:
            candidate_policy = (
                self._resource_authority_policy_override
                or resource_authority_policy_from_environment(candidate_environment)
            )
        except ValueError as error:
            raise StudioError(
                "SETTINGS_INVALID", "云端连接配置无效，请检查控制面地址和区域", status_code=422
            ) from error
        self.plugin_runs.check_configuration_mutable()
        self.configuration.update_settings(data)
        self._apply_persisted_settings()
        self.resource_authority = (
            SignedKnowledgeResourceAuthority(self.resource_connections, candidate_policy)
            if candidate_policy is not None
            else None
        )
        self.plugin_runs.replace_resource_authority(self.resource_authority)
        if self._cloud_gateway_override is None:
            self.cloud.gateway = self._configured_cloud_gateway()
        return self.get_settings()

    async def apply_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Drain old credentials/leases before making a new account visible."""
        async with self._start_lock:
            if self.run_service.has_active_runs():
                raise StudioError(
                    "SETTINGS_RUNTIME_BUSY", "请等待当前运行结束后再保存设置", status_code=409
                )
            try:
                await self.plugin_runs.suspend_admission()
                return self.update_settings(payload)
            finally:
                await self.plugin_runs.resume_admission()

    def _apply_persisted_settings(self) -> None:
        data = self.configuration.settings()
        environment = self.configuration.environment()
        self._apply_settings_to_env(data)
        for name in (
            *SETTINGS_ENV.values(),
            *self.configuration.dotenv,
            *self.configuration.overrides,
        ):
            if name in environment:
                os.environ[name] = environment[name]

    @staticmethod
    def _apply_settings_to_env(data: dict[str, Any]) -> None:
        if data.get("sandbox"):
            os.environ["KSADK_CODEX_SANDBOX"] = data["sandbox"]
        if "codexProxy" in data:
            proxy_env = proxy_mode_env_value(data["codexProxy"])
            if proxy_env is None:
                os.environ.pop("KSADK_CODEX_USE_PROXY", None)
            else:
                os.environ["KSADK_CODEX_USE_PROXY"] = proxy_env
        if data.get("cloudRegion"):
            os.environ["AGENTENGINE_REGION"] = data["cloudRegion"]
        if data.get("cloudBucket"):
            os.environ["KS3_BUCKET"] = data["cloudBucket"]
        if "traceContent" in data:
            os.environ["KSADK_STUDIO_TRACE_CONTENT"] = "1" if data["traceContent"] else "0"
        # 云账号凭证:桥接到 KSYUN_* env(AgentEngineClient 的 V4 签名与
        # X-Ksc-Account-Id 注入都从这里取值)。
        if data.get("cloudAccessKey"):
            os.environ["KSYUN_ACCESS_KEY"] = str(data["cloudAccessKey"])
        if data.get("cloudSecretKey"):
            os.environ["KSYUN_SECRET_KEY"] = str(data["cloudSecretKey"])
        if data.get("cloudAccountId"):
            os.environ["KSYUN_ACCOUNT_ID"] = str(data["cloudAccountId"])
