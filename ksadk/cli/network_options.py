"""Shared CLI helpers for AgentEngine network configuration."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Mapping, Optional

import click

from ksadk.cli.error_utils import validation_error
from ksadk.cli.ui import print_info, print_warn

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


def resolve_deploy_target_network(
    deploy_target: "DeployTarget",
    *,
    region: str | None = None,
    dry_run: bool = False,
) -> None:
    """Fill optional network fields on a DeployTarget when they can be inferred."""
    network = getattr(deploy_target, "network", None)
    if network is None:
        return
    if str(getattr(network, "availability_zone", "") or "").strip():
        return
    if not bool(getattr(network, "enable_vpc_access", False)):
        return
    subnet_id = str(getattr(network, "subnet_id", "") or "").strip()
    if not subnet_id or dry_run:
        return
    resolved_region = str(region or getattr(deploy_target, "region", "") or "").strip()
    if not resolved_region:
        return
    availability_zone = _resolve_subnet_availability_zone(
        subnet_id=subnet_id,
        region=resolved_region,
    )
    if availability_zone:
        network.availability_zone = availability_zone
        print_info(f"已根据子网 {subnet_id} 自动推断可用区: {availability_zone}")
    else:
        print_warn(
            "未能根据子网自动推断可用区；如私网 ENI 调度失败，请显式传入 "
            "`--availability-zone`。"
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
    _fill_network_availability_zone(payload, network_kwargs)
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


def _fill_network_availability_zone(payload: dict[str, Any], network_kwargs: Mapping[str, Any]) -> None:
    if str(payload.get("availability_zone") or "").strip():
        return
    if not bool(payload.get("enable_vpc_access")):
        return
    subnet_id = str(payload.get("subnet_id") or "").strip()
    if not subnet_id:
        return
    if bool(network_kwargs.get("dry_run")):
        return
    region = str(network_kwargs.get("region") or "").strip()
    if not region:
        return

    availability_zone = _resolve_subnet_availability_zone(subnet_id=subnet_id, region=region)
    if availability_zone:
        payload["availability_zone"] = availability_zone
        print_info(f"已根据子网 {subnet_id} 自动推断可用区: {availability_zone}")
    else:
        print_warn(
            "未能根据子网自动推断可用区；如私网 ENI 调度失败，请显式传入 "
            "`--availability-zone`。"
        )


def _resolve_subnet_availability_zone(*, subnet_id: str, region: str) -> str | None:
    subnet_id = str(subnet_id or "").strip()
    region = str(region or "").strip()
    if not subnet_id or not region:
        return None

    access_key, secret_key = _resolve_ksyun_credentials()
    if not access_key or not secret_key:
        return None

    try:
        VpcClient, DescribeSubnetsRequest, Credential, ClientProfile, HttpProfile = _import_vpc_sdk()
    except Exception as exc:
        print_warn(f"缺少 VPC 子网查询 SDK，跳过可用区自动推断: {exc}")
        return None

    response = None
    last_error: Exception | None = None
    for endpoint, protocol in ((None, None), ("vpc.inner.api.ksyun.com", "http")):
        if endpoint and not _should_retry_inner_vpc_endpoint(last_error):
            break
        try:
            profile = ClientProfile(
                httpProfile=HttpProfile(reqTimeout=10, endpoint=endpoint, protocol=protocol)
            )
            client = VpcClient(Credential(access_key, secret_key), region, profile)
            request = DescribeSubnetsRequest()
            request.SubnetId = {"1": subnet_id}
            response = client.DescribeSubnets(request)
            break
        except Exception as exc:
            last_error = exc

    if response is None:
        print_warn(f"查询子网 {subnet_id} 可用区失败，跳过自动推断: {last_error}")
        return None

    return _extract_subnet_availability_zone(response, subnet_id)


def _import_vpc_sdk():
    from ksyun.client.vpc.v20160304.client import VpcClient
    from ksyun.client.vpc.v20160304.models import DescribeSubnetsRequest
    from ksyun.common.credential import Credential
    from ksyun.common.profile.client_profile import ClientProfile
    from ksyun.common.profile.http_profile import HttpProfile

    return VpcClient, DescribeSubnetsRequest, Credential, ClientProfile, HttpProfile


def _should_retry_inner_vpc_endpoint(error: Exception | None) -> bool:
    if error is None:
        return False
    return "InnerAccountCanOnlyAccessThroughIntranet" in str(error)


def _resolve_ksyun_credentials() -> tuple[str, str]:
    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY") or ""
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY") or ""
    if access_key and secret_key:
        return access_key.strip(), secret_key.strip()

    try:
        from ksadk.configs.global_config import get_env_from_global_config

        global_env = get_env_from_global_config()
    except Exception:
        global_env = {}

    access_key = access_key or global_env.get("KSYUN_ACCESS_KEY") or global_env.get("KS3_ACCESS_KEY") or ""
    secret_key = secret_key or global_env.get("KSYUN_SECRET_KEY") or global_env.get("KS3_SECRET_KEY") or ""
    return str(access_key or "").strip(), str(secret_key or "").strip()


def _extract_subnet_availability_zone(response: Any, subnet_id: str) -> str | None:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return None
    if not isinstance(response, Mapping):
        return None

    wanted_subnet_id = str(subnet_id or "").strip()
    subnets = list(_iter_subnet_dicts(response))
    for subnet in subnets:
        current_subnet_id = _pick_text(
            subnet,
            "SubnetId",
            "subnetId",
            "subnet_id",
            "id",
            "Id",
        )
        if current_subnet_id and current_subnet_id != wanted_subnet_id:
            continue
        availability_zone = _pick_text(
            subnet,
            "AvailabilityZone",
            "AvailabilityZoneName",
            "availabilityZone",
            "availabilityZoneName",
            "availability_zone",
            "availability_zone_name",
            "Zone",
            "zone",
        )
        if availability_zone:
            return availability_zone

    if len(subnets) == 1:
        return _pick_text(
            subnets[0],
            "AvailabilityZone",
            "AvailabilityZoneName",
            "availabilityZone",
            "availabilityZoneName",
            "availability_zone",
            "availability_zone_name",
            "Zone",
            "zone",
        )
    return None


def _iter_subnet_dicts(value: Any):
    if isinstance(value, Mapping):
        subnet_keys = ("SubnetId", "subnetId", "subnet_id")
        zone_keys = (
            "AvailabilityZone",
            "AvailabilityZoneName",
            "availabilityZone",
            "availabilityZoneName",
            "availability_zone",
            "availability_zone_name",
        )
        if any(key in value for key in subnet_keys) or any(key in value for key in zone_keys):
            yield value
        for child in value.values():
            yield from _iter_subnet_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_subnet_dicts(item)


def _pick_text(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""
