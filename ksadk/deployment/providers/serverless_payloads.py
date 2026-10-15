"""Build serverless artifact, network, storage, and credential payloads."""

from __future__ import annotations

from typing import Any, Dict, Optional

import click

from ksadk.builders.container_builder import registry_kind_label, resolve_registry_credentials
from ksadk.deployment.base import DeployTarget


class _ServerlessPayloadMixin:
    @staticmethod
    def _image_credential_from_env(image_ref: str) -> Optional[Dict[str, str]]:
        username, password, registry_kind = resolve_registry_credentials(image_ref)
        if username and password:
            return {
                "endpoint": image_ref.split("/", 1)[0],
                "username": username,
                "password": password,
            }

        if password and not username and registry_kind != "personal_kcr":
            click.secho(
                f"   ⚠️  检测到 KCR_PASSWORD 但缺少 KCR_USERNAME，"
                f"已忽略{registry_kind_label(registry_kind)}镜像凭证；"
                "企业版 KCR 和第三方镜像仓库必须配置 KCR_USERNAME + KCR_PASSWORD",
                fg="yellow",
            )
        elif registry_kind == "personal_kcr":
            click.secho(
                "   ⚠️  未配置个人版 KCR 镜像凭证 "
                "(KSYUN_ACCOUNT_ID/KCR_PASSWORD)，私有镜像可能无法拉取",
                fg="yellow",
            )
        else:
            click.secho(
                f"   ⚠️  未配置{registry_kind_label(registry_kind)}镜像凭证 "
                "(KCR_USERNAME/KCR_PASSWORD)，私有镜像可能无法拉取",
                fg="yellow",
            )
        return None

    @staticmethod
    def _parse_code_artifact_path(artifact_path: str) -> tuple[Optional[str], Optional[str]]:
        """解析代码包路径，返回 (bucket, object_key)。"""
        if not artifact_path:
            return None, None

        path = artifact_path.strip()
        if not path:
            return None, None

        if path.startswith("ks3://"):
            raw = path[6:]
            parts = raw.split("/", 1)
            bucket = parts[0].strip() if parts else ""
            object_key = parts[1].strip() if len(parts) > 1 else ""
            return bucket or None, object_key or None

        if path.startswith("http://") or path.startswith("https://"):
            from urllib.parse import urlparse

            parsed = urlparse(path)
            bucket = (parsed.netloc.split(".", 1)[0] if parsed.netloc else "").strip()
            object_key = parsed.path.lstrip("/").strip()
            return bucket or None, object_key or None

        if "/" in path:
            bucket, object_key = path.split("/", 1)
            return bucket.strip() or None, object_key.strip() or None

        return None, None

    @staticmethod
    def _serialize_network_config(
        target: DeployTarget, *, is_update: bool = False
    ) -> Optional[Dict[str, Any]]:
        network = getattr(target, "network", None)
        if network is None:
            return None

        public_access = getattr(network, "enable_public_access", None)
        # 三态：None=未指定。create 默认开公网；update 未指定则不发 network 字段。
        if public_access is None:
            public_value: Optional[bool] = None if is_update else True
        else:
            public_value = bool(public_access)

        payload: Dict[str, Any] = {}
        if public_value is not None:
            payload["enable_public_access"] = public_value
        if bool(getattr(network, "enable_vpc_access", False)):
            payload["enable_vpc_access"] = True

        for field in ("vpc_id", "subnet_id", "security_group_id", "availability_zone"):
            value = str(getattr(network, field, "") or "").strip()
            if value:
                payload[field] = value

        if not payload:
            return None
        return payload

    @staticmethod
    def _serialize_storage_config(target: DeployTarget) -> Optional[Dict[str, Any]]:
        storage = getattr(target, "storage", None)
        if storage is None:
            return None

        mount_path = str(getattr(storage, "mount_path", "") or "").strip()
        size_gi = getattr(storage, "size_gi", None)
        if not mount_path and size_gi is None:
            return None

        payload: Dict[str, Any] = {}
        if mount_path:
            payload["mount_path"] = mount_path
        if size_gi is not None:
            payload["size_gi"] = int(size_gi)
        return payload
