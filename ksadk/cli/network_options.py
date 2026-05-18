"""Shared CLI helpers for AgentEngine network configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Optional

import click

from ksadk.cli.error_utils import validation_error

if TYPE_CHECKING:
    from ksadk.deployment.base import DeployTarget


NETWORK_OPTION_PARAMS = (
    "enable_public_access",
    "enable_vpc_access",
    "vpc_id",
    "subnet_id",
    "security_group_id",
    "availability_zone",
)

_VPC_ID_FIELDS = ("vpc_id", "subnet_id", "security_group_id")
_VPC_ID_LABELS = {
    "vpc_id": "VpcId",
    "subnet_id": "SubnetId",
    "security_group_id": "SecurityGroupId",
}


def network_options(func):
    """Attach common network options to a Click command."""
    options = [
        click.option(
            "--enable-public-access/--disable-public-access",
            default=None,
            help="是否开启公网访问；未指定时使用配置文件或平台默认值",
        ),
        click.option("--enable-vpc-access", is_flag=True, default=False, help="开启 VPC 私网访问"),
        click.option("--vpc-id", default=None, help="VPC ID（开启 VPC 访问时必填）"),
        click.option("--subnet-id", default=None, help="子网 ID（开启 VPC 访问时必填）"),
        click.option("--security-group-id", default=None, help="安全组 ID（开启 VPC 访问时必填）"),
        click.option("--availability-zone", default=None, help="可用区（可选）"),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def network_cli_kwargs(
    *,
    enable_public_access: Optional[bool] = None,
    enable_vpc_access: bool = False,
    vpc_id: Optional[str] = None,
    subnet_id: Optional[str] = None,
    security_group_id: Optional[str] = None,
    availability_zone: Optional[str] = None,
) -> dict[str, Any]:
    """Return normalized kwargs for passing between Click entrypoints and async functions."""
    return {
        "enable_public_access": enable_public_access,
        "enable_vpc_access": bool(enable_vpc_access),
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": security_group_id,
        "availability_zone": availability_zone,
    }


def extract_network_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read network from top-level `network` or `deploy.network`."""
    if not isinstance(config, Mapping):
        return {}
    deploy_config = config.get("deploy") if isinstance(config.get("deploy"), Mapping) else {}
    raw_network = config.get("network") or deploy_config.get("network") or {}
    return dict(raw_network) if isinstance(raw_network, Mapping) else {}


def apply_network_config(config: Mapping[str, Any] | None, deploy_target: "DeployTarget") -> None:
    """Apply file-based network config to a DeployTarget."""
    raw_network = extract_network_config(config)
    if not raw_network:
        return

    def _pick(*keys: str, default=None):
        for key in keys:
            if key in raw_network and raw_network[key] is not None:
                return raw_network[key]
        return default

    deploy_target.network.enable_public_access = bool(
        _pick("enable_public_access", "enablePublicAccess", "EnablePublicAccess", default=deploy_target.network.enable_public_access)
    )
    deploy_target.network.enable_vpc_access = bool(
        _pick("enable_vpc_access", "enableVpcAccess", "EnableVpcAccess", default=deploy_target.network.enable_vpc_access)
    )
    deploy_target.network.vpc_id = str(
        _pick("vpc_id", "vpcId", "VpcId", default=deploy_target.network.vpc_id) or ""
    ).strip()
    deploy_target.network.subnet_id = str(
        _pick("subnet_id", "subnetId", "SubnetId", default=deploy_target.network.subnet_id) or ""
    ).strip()
    deploy_target.network.security_group_id = str(
        _pick(
            "security_group_id",
            "securityGroupId",
            "SecurityGroupId",
            default=deploy_target.network.security_group_id,
        )
        or ""
    ).strip()
    deploy_target.network.availability_zone = str(
        _pick(
            "availability_zone",
            "availabilityZone",
            "AvailabilityZone",
            default=deploy_target.network.availability_zone,
        )
        or ""
    ).strip()


def apply_network_cli_overrides(deploy_target: "DeployTarget", **network_kwargs: Any) -> None:
    """Apply explicit CLI network overrides to a DeployTarget."""
    if network_kwargs.get("enable_public_access") is not None:
        deploy_target.network.enable_public_access = bool(network_kwargs["enable_public_access"])
    if network_kwargs.get("enable_vpc_access"):
        deploy_target.network.enable_vpc_access = True
    for field in ("vpc_id", "subnet_id", "security_group_id", "availability_zone"):
        value = network_kwargs.get(field)
        if value is not None:
            setattr(deploy_target.network, field, str(value or "").strip())
    if any(str(getattr(deploy_target.network, field, "") or "").strip() for field in _VPC_ID_FIELDS):
        deploy_target.network.enable_vpc_access = True


def validate_deploy_target_network(deploy_target: "DeployTarget") -> None:
    """Require VPC identifiers as an atomic group when VPC access is requested."""
    values = {
        "vpc_id": str(getattr(deploy_target.network, "vpc_id", "") or "").strip(),
        "subnet_id": str(getattr(deploy_target.network, "subnet_id", "") or "").strip(),
        "security_group_id": str(getattr(deploy_target.network, "security_group_id", "") or "").strip(),
    }
    has_any_vpc_id = any(values.values())
    if not (bool(getattr(deploy_target.network, "enable_vpc_access", False)) or has_any_vpc_id):
        return
    missing = [_VPC_ID_LABELS[field] for field, value in values.items() if not value]
    if missing:
        raise validation_error(
            "开启 VPC 访问时必须同时提供 VpcId、SubnetId、SecurityGroupId。",
            details={"missing": missing},
            hints=[
                "请同时传入 `--vpc-id`、`--subnet-id`、`--security-group-id`，或在配置文件 network/deploy.network 中同时设置这三个字段。",
                "`--availability-zone` 是可选字段，不替代子网或安全组。",
            ],
        )


def build_network_payload(**network_kwargs: Any) -> dict[str, Any] | None:
    """Build lower-case network payload for AgentEngineClient create/update calls."""
    payload: dict[str, Any] = {}
    if network_kwargs.get("enable_public_access") is not None:
        payload["enable_public_access"] = bool(network_kwargs["enable_public_access"])
    if network_kwargs.get("enable_vpc_access"):
        payload["enable_vpc_access"] = True
    for field in ("vpc_id", "subnet_id", "security_group_id", "availability_zone"):
        value = network_kwargs.get(field)
        if value is not None and str(value or "").strip():
            payload[field] = str(value or "").strip()
    if not payload:
        return None
    if any(str(payload.get(field) or "").strip() for field in _VPC_ID_FIELDS):
        payload["enable_vpc_access"] = True

    _validate_network_payload(payload)
    return payload


def _validate_network_payload(payload: Mapping[str, Any]) -> None:
    values = {field: str(payload.get(field) or "").strip() for field in _VPC_ID_FIELDS}
    has_any_vpc_id = any(values.values())
    if not (bool(payload.get("enable_vpc_access")) or has_any_vpc_id):
        return
    missing = [_VPC_ID_LABELS[field] for field, value in values.items() if not value]
    if missing:
        raise validation_error(
            "开启 VPC 访问时必须同时提供 VpcId、SubnetId、SecurityGroupId。",
            details={"missing": missing},
            hints=[
                "请同时传入 `--vpc-id`、`--subnet-id`、`--security-group-id`。",
                "`--availability-zone` 是可选字段，不替代子网或安全组。",
            ],
        )
