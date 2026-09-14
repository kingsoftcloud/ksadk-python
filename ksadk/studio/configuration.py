"""One workspace configuration store for Studio settings and secret references."""

from __future__ import annotations

import os
from collections.abc import Mapping
from threading import RLock
from typing import Any

import yaml
from dotenv import dotenv_values

from ksadk.configs.global_config import get_env_from_global_config
from ksadk.studio.errors import StudioError

SETTINGS_ENV = {
    "cloudAccessKey": "KSYUN_ACCESS_KEY",
    "cloudSecretKey": "KSYUN_SECRET_KEY",
    "cloudAccountId": "KSYUN_ACCOUNT_ID",
    "cloudServerUrl": "AGENTENGINE_SERVER_URL",
    "cloudRegion": "AGENTENGINE_REGION",
    "cloudBucket": "KS3_BUCKET",
    "sandbox": "KSADK_CODEX_SANDBOX",
}
SECRET_SETTINGS = {"cloudAccessKey", "cloudSecretKey"}
_LOCK = RLock()


class WorkspaceConfiguration:
    """Explicit env-file > workspace > inherited environment > workspace .env.

    The canonical file is written atomically with mode 0600. Old files are
    imported once, without modifying them or the user's .env. Read fresh on
    each operation so settings and credential API writes share one store.
    """

    def __init__(self, workspace: Any, *, overrides: Mapping[str, str] | None = None):
        self.workspace = workspace
        self.path = workspace.resolve(".agentkit/config.yaml")
        self.overrides = dict(overrides or {})
        self.inherited = dict(os.environ)
        self.global_defaults = get_env_from_global_config()
        self.dotenv = {
            key: value
            for key, value in dotenv_values(workspace.resolve(".env")).items()
            if value is not None
        }

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.is_file():
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("schema")
                if not isinstance(data.get("settings"), dict) or not isinstance(
                    data.get("secrets"), dict
                ):
                    raise ValueError("sections")
                if any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in data["secrets"].items()
                ):
                    raise ValueError("secrets")
                return data
            legacy_settings = self.workspace.resolve(".agentkit/settings.yaml")
            legacy_secrets = self.workspace.resolve(".agentkit/secrets.env")
            settings = (
                yaml.safe_load(legacy_settings.read_text(encoding="utf-8")) or {}
                if legacy_settings.is_file()
                else {}
            )
            if not isinstance(settings, dict):
                raise ValueError("legacy settings")
            # Legacy writer stored literal values, without dotenv interpolation.
            secrets = {}
            if legacy_secrets.is_file():
                for line in legacy_secrets.read_text(encoding="utf-8").splitlines():
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        secrets[key.strip()] = value
            for key in SECRET_SETTINGS:
                if settings.get(key):
                    secrets.setdefault(SETTINGS_ENV[key], str(settings.pop(key)))
            data = {"version": 1, "settings": settings, "secrets": secrets}
            if legacy_settings.is_file() or legacy_secrets.is_file():
                self._write(data)
            return data
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise StudioError(
                "CONFIGURATION_INVALID",
                "工作区配置无法读取，请检查 .agentkit/config.yaml 或旧配置文件",
                status_code=422,
            ) from error

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.workspace.ignore_private_configuration()
            self.workspace.atomic_write_yaml(self.path, data)
        except OSError as error:
            raise StudioError(
                "CONFIGURATION_SAVE_FAILED", "工作区配置保存失败，请检查目录权限", status_code=500
            ) from error

    def settings(self) -> dict[str, Any]:
        with _LOCK:
            return dict(self._read()["settings"])

    def secrets(self) -> dict[str, str]:
        with _LOCK:
            return dict(self._read()["secrets"])

    def update_settings(self, patch: Mapping[str, Any]) -> None:
        with _LOCK:
            data = self._read()
            for key, value in patch.items():
                if key in SECRET_SETTINGS:
                    data["secrets"][SETTINGS_ENV[key]] = value
                else:
                    data["settings"][key] = value
            self._write(data)

    def put_secret(self, name: str, value: str | None) -> None:
        with _LOCK:
            data = self._read()
            if value is None:
                data["secrets"].pop(name, None)
            else:
                data["secrets"][name] = value
            self._write(data)

    def resolve(self, name: str) -> tuple[str | None, str]:
        if name in self.overrides:
            return self.overrides[name], "env-file"
        with _LOCK:
            data = self._read()
        if name in data["secrets"]:
            return data["secrets"][name], "workspace"
        for key, env in SETTINGS_ENV.items():
            if env == name and key in data["settings"]:
                return str(data["settings"][key]), "workspace"
        if self.global_defaults.get(name):
            return self.global_defaults[name], "global"
        if self.inherited.get(name):
            return self.inherited[name], "environment"
        if self.dotenv.get(name):
            return self.dotenv[name], "dotenv"
        return None, "missing"

    def resolve_candidates(self, names: list[str]) -> tuple[str | None, str]:
        ranked = {
            "env-file": 0,
            "workspace": 1,
            "global": 2,
            "environment": 3,
            "dotenv": 4,
            "missing": 5,
        }
        candidates = [(self.resolve(name), i) for i, name in enumerate(names)]
        (value, source), index = min(candidates, key=lambda item: (ranked[item[0][1]], item[1]))
        return value, source + ("-alias" if value and index else "")

    def environment(self) -> dict[str, str]:
        with _LOCK:
            data = self._read()
        values = {**self.dotenv, **self.global_defaults, **self.inherited}
        for key, name in SETTINGS_ENV.items():
            if key in data["settings"]:
                values[name] = str(data["settings"][key])
        if "codexProxy" in data["settings"]:
            from ksadk.studio.codex_builder import proxy_mode_env_value

            proxy = proxy_mode_env_value(data["settings"]["codexProxy"])
            if proxy is None:
                values.pop("KSADK_CODEX_USE_PROXY", None)
            else:
                values["KSADK_CODEX_USE_PROXY"] = proxy
        if "traceContent" in data["settings"]:
            values["KSADK_STUDIO_TRACE_CONTENT"] = "1" if data["settings"]["traceContent"] else "0"
        values.update(data["secrets"])
        values.update(self.overrides)
        return values
