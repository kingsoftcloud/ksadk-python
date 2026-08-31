"""``agentengine plugin`` - DSH-native and Codex-compatible plugin management.

The stable CLI deliberately exposes only two ecosystems.  Top-level lifecycle
commands manage the active DeepSeek Harness Profile; ``plugin dsh`` remains a
compatibility alias for scripts that adopted the earlier explicit namespace.
Codex plugins stay under ``plugin codex`` because their lifecycle is owned by
Codex App Server.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import click

from ksadk.cli.error_utils import (
    EXIT_CODE_RESOLUTION,
    EXIT_CODE_VALIDATION,
    CLIError,
    abort_with_cli_error,
)
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.cli.ui import emit_json, is_json_output, print_info, print_kv, print_success

CODEX_HOME_ENV = "KSADK_CODEX_HOME"
DSH_HOME_ENV = "KSADK_DSH_HOME"
DSH_BIN_ENV = "KSADK_DSH_BIN"
DSH_PROFILE_ENV = "KSADK_DSH_PROFILE"


def _codex_home() -> Path:
    """Use the same workspace-isolated Codex home as the local Runtime."""

    configured = os.environ.get(CODEX_HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / ".agentkit" / "codex-home"


def _dsh_home() -> Path:
    configured = os.environ.get(DSH_HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / ".agentkit" / "dsh-home"


def _dsh_command() -> tuple[str, ...] | None:
    configured = os.environ.get(DSH_BIN_ENV, "").strip()
    from ksadk.plugins.dsh_toolchain import DshToolchainManager

    return DshToolchainManager().require_command(configured or None)


def _dsh_profile() -> str:
    return os.environ.get(DSH_PROFILE_ENV, "").strip() or "ksadk"


def _codex_inventory_payload(value: Any) -> dict[str, Any]:
    """Project Codex host inventory without leaking marketplace filesystem paths."""

    raw = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        "ecosystem": "codex",
        "integrationMode": "bridged",
        "pluginId": raw["pluginId"],
        "name": raw["name"],
        "marketplaceName": raw["marketplaceName"],
        "version": raw.get("version"),
        "installed": raw["installed"],
        "enabled": raw["enabled"],
        "availability": raw["availability"],
        "permissionsDeclared": False,
        "riskDisclosures": raw.get("riskDisclosures", []),
    }


def _dsh_inventory_payload(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _with_codex_bridge(operation):
    from ksadk.plugins.bridges.codex import CodexAppServerPluginBridge

    async with CodexAppServerPluginBridge(codex_home=_codex_home()) as bridge:
        return bridge.host, await operation(bridge)


def _call_codex(operation):
    """Run one bounded App Server lifecycle request from the synchronous CLI."""

    try:
        return asyncio.run(_with_codex_bridge(operation))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as err:
        code = (
            "codex_plugin_not_found"
            if type(err).__name__ == "CodexPluginNotFoundError"
            else (
                "codex_plugin_permission_confirmation_required"
                if type(err).__name__ == "CodexPluginApprovalRequired"
                else "codex_plugin_host_unavailable"
            )
        )
        abort_with_cli_error(
            CLIError(
                code=code,
                message=(
                    "Codex 插件不存在或来源不唯一"
                    if code == "codex_plugin_not_found"
                    else "安装前必须确认 Codex App Server 宿主权限风险"
                    if code == "codex_plugin_permission_confirmation_required"
                    else "Codex 插件宿主当前不可用"
                ),
                exit_code=(
                    EXIT_CODE_RESOLUTION
                    if code == "codex_plugin_not_found"
                    else EXIT_CODE_VALIDATION
                ),
            ),
            context="Plugin",
        )


def _call_dsh(operation):
    """Run one bounded DSH Profile lifecycle request from the synchronous CLI."""

    from ksadk.plugins.bridges.dsh import DshProfilePluginBridge

    try:
        with DshProfilePluginBridge(
            dsh_home=_dsh_home(),
            profile=_dsh_profile(),
            dsh_command=_dsh_command(),
        ) as bridge:
            return bridge.host, operation(bridge)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as err:
        name = type(err).__name__
        code = (
            "dsh_plugin_not_found"
            if name == "DshPluginNotFoundError"
            else "dsh_plugin_permission_confirmation_required"
            if name == "DshPluginApprovalRequired"
            else "dsh_plugin_operation_failed"
            if name in {"DshPluginMutationError", "ValueError"}
            else "dsh_plugin_host_unavailable"
        )
        messages = {
            "dsh_plugin_not_found": "DSH 插件未安装",
            "dsh_plugin_permission_confirmation_required": (
                "安装或升级前必须确认 DSH 宿主权限风险"
            ),
            "dsh_plugin_operation_failed": "DSH 插件操作失败，原 Profile 已保留",
            "dsh_plugin_host_unavailable": "DSH 插件宿主当前不可用",
        }
        abort_with_cli_error(
            CLIError(
                code=code,
                message=messages[code],
                exit_code=(
                    EXIT_CODE_RESOLUTION if code == "dsh_plugin_not_found" else EXIT_CODE_VALIDATION
                ),
            ),
            context="Plugin",
        )


def _call_dsh_developer(operation):
    """Run one bounded standard DSH bundle development operation."""

    from ksadk.plugins.dsh_toolchain import (
        DshPluginPackError,
        DshPluginSourceError,
        DshPluginValidationError,
        DshToolchainInstallError,
        DshToolchainUnavailableError,
        DshToolchainVersionMismatchError,
    )

    try:
        return operation()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as err:
        if isinstance(err, DshToolchainVersionMismatchError):
            code = "dsh_toolchain_version_mismatch"
            message = "DSH 或 pnpm 版本与受支持工具链不一致"
        elif isinstance(err, DshToolchainUnavailableError):
            code = "dsh_toolchain_unavailable"
            message = "DSH 插件开发工具链不可用"
        elif isinstance(err, DshToolchainInstallError):
            code = "dsh_toolchain_install_failed"
            message = "DSH 插件开发工具链安装失败"
        elif isinstance(err, DshPluginSourceError):
            code = "dsh_plugin_source_invalid"
            message = "DSH 插件源码不是有效的标准 Bundle"
        elif isinstance(err, DshPluginValidationError):
            code = "dsh_plugin_validation_failed"
            message = "DSH 插件生命周期校验失败"
        elif isinstance(err, DshPluginPackError):
            code = "dsh_plugin_pack_failed"
            message = "DSH 插件 npm 打包失败"
        else:
            raise
        details = {}
        stage = getattr(err, "stage", None)
        if isinstance(stage, str) and stage:
            details["stage"] = stage
        abort_with_cli_error(
            CLIError(
                code=code,
                message=message,
                exit_code=EXIT_CODE_VALIDATION,
                details=details,
            ),
            context="Plugin",
        )


@click.group("plugin", context_settings=CONTEXT_SETTINGS)
def plugin() -> None:
    """管理 DSH 插件，并兼容 Codex App Server 插件。"""


@plugin.group("toolchain", context_settings=CONTEXT_SETTINGS)
def plugin_toolchain() -> None:
    """管理固定版本、隔离安装的 DSH 插件开发工具链。"""


@plugin_toolchain.command("status", context_settings=CONTEXT_SETTINGS)
def plugin_toolchain_status() -> None:
    """检查受管理 DSH CLI、实际版本、pnpm 和锁文件。"""

    from ksadk.plugins.dsh_toolchain import DshToolchainManager

    state = DshToolchainManager().status()
    payload = state.model_dump(mode="json", by_alias=True, exclude_none=True)
    if is_json_output():
        emit_json(payload)
        return
    print_kv("DSH", state.actual_version or state.expected_version)
    print_kv("状态", "可用" if state.usable else "未安装或不完整")
    print_kv("目录", state.root)
    if state.problem:
        print_kv("问题", state.problem)


@plugin_toolchain.command("install", context_settings=CONTEXT_SETTINGS)
def install_plugin_toolchain() -> None:
    """从 npm 安装受支持的 DSH CLI；不需要 DSH 源码仓。"""

    from ksadk.plugins.dsh_toolchain import DshToolchainManager

    state = _call_dsh_developer(lambda: DshToolchainManager().install())
    payload = state.model_dump(mode="json", by_alias=True, exclude_none=True)
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件开发工具链已就绪: {state.actual_version}")
        print_kv("目录", state.root)


@plugin.command("create", context_settings=CONTEXT_SETTINGS)
@click.argument("target", type=click.Path(path_type=Path))
@click.option("--name", "package_name", default=None, help="标准 npm 包名。")
def create_dsh_plugin(target: Path, package_name: str | None) -> None:
    """创建标准 DSH Bundle；不会生成 KsADK 私有 manifest。"""

    from ksadk.plugins.dsh_toolchain import DshPluginDeveloper

    result = _call_dsh_developer(
        lambda: DshPluginDeveloper().create(target, package_name=package_name)
    )
    payload = result.model_dump(mode="json", by_alias=True)
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已创建: {result.package_name}")
        print_kv("目录", result.target)


@plugin.command("validate", context_settings=CONTEXT_SETTINGS)
@click.argument("source")
def validate_dsh_plugin(source: str) -> None:
    """在临时 Profile 验证安装、投射、启停与卸载生命周期。"""

    from ksadk.plugins.dsh_toolchain import DshPluginDeveloper

    configured = os.environ.get(DSH_BIN_ENV, "").strip() or None
    result = _call_dsh_developer(
        lambda: DshPluginDeveloper(explicit_dsh=configured).validate(source)
    )
    payload = result.model_dump(mode="json", by_alias=True)
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件生命周期已通过: {result.package_name}")
        print_kv("DSH", result.host_version)
        print_kv("Profile 摘要", result.profile_digest)


@plugin.command("test", context_settings=CONTEXT_SETTINGS)
@click.argument("source")
def test_dsh_plugin(source: str) -> None:
    """运行真实 DSH 生命周期 smoke；不冒充 Provider conformance。"""

    validate_dsh_plugin.callback(source)  # type: ignore[attr-defined]


@plugin.command("pack", context_settings=CONTEXT_SETTINGS)
@click.argument("source", type=click.Path(path_type=Path))
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="npm tarball 输出目录，默认 SOURCE/dist。",
)
def pack_dsh_plugin(source: Path, output_dir: Path | None) -> None:
    """委托固定 pnpm 将标准 DSH Bundle 打包成 npm tgz。"""

    from ksadk.plugins.dsh_toolchain import DshPluginDeveloper

    result = _call_dsh_developer(
        lambda: DshPluginDeveloper().pack(source, output_dir=output_dir)
    )
    payload = result.model_dump(mode="json", by_alias=True)
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已打包: {result.package_name}@{result.package_version}")
        print_kv("产物", result.artifact)


@plugin.group("codex", context_settings=CONTEXT_SETTINGS)
def codex_plugins() -> None:
    """通过 Codex App Server 管理 Codex 官方插件。"""


@codex_plugins.command("list", context_settings=CONTEXT_SETTINGS)
@click.option("--installed-only", is_flag=True, help="只显示已安装插件。")
@click.option("--force-refetch", is_flag=True, help="请求宿主刷新插件目录。")
def list_codex_plugins(installed_only: bool, force_refetch: bool) -> None:
    """列出 Codex 宿主可见的官方插件。"""

    async def operation(bridge):
        return await bridge.list_plugins(force_refetch=force_refetch)

    host, items = _call_codex(operation)
    visible = [item for item in items if item.installed or not installed_only]
    payload = {
        "ecosystem": "codex",
        "integrationMode": "bridged",
        "host": {"id": "codex-app-server", "version": host.version, "available": True},
        "items": [_codex_inventory_payload(item) for item in visible],
    }
    if is_json_output():
        emit_json(payload)
        return
    print_info(f"Codex App Server {host.version} · {len(visible)} 个插件")
    for item in payload["items"]:
        state = "已安装" if item["installed"] else "可安装"
        click.echo(f"{item['pluginId']}  {item.get('version') or '-'}  {state}")


@codex_plugins.command("info", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_id")
@click.option("--marketplace", "marketplace_name", default=None)
def codex_plugin_info(plugin_id: str, marketplace_name: str | None) -> None:
    """显示一个 Codex 插件的宿主 inventory 和能力。"""

    async def operation(bridge):
        return await bridge.read_plugin(plugin_id, marketplace_name=marketplace_name)

    host, detail = _call_codex(operation)
    payload = {
        "item": _codex_inventory_payload(detail.inventory),
        "host": {"id": "codex-app-server", "version": host.version, "available": True},
        "description": detail.description,
        "capabilities": {
            "skills": list(detail.skills),
            "mcpServers": list(detail.mcp_servers),
            "hooks": list(detail.hooks),
            "apps": list(detail.apps),
            "scheduledTasks": list(detail.scheduled_tasks),
        },
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"Codex 插件: {plugin_id}")
        print_kv("宿主", f"Codex App Server {host.version}")
        print_kv("状态", "已安装" if detail.inventory.installed else "可安装")


@codex_plugins.command("install", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_id")
@click.option("--marketplace", "marketplace_name", default=None)
@click.option(
    "--accept-host-permissions",
    is_flag=True,
    help="确认插件权限由 Codex 宿主管理并以当前系统用户权限运行。",
)
def install_codex_plugin(
    plugin_id: str,
    marketplace_name: str | None,
    accept_host_permissions: bool,
) -> None:
    """安装一个 Codex 官方插件；必须显式确认宿主权限风险。"""

    if not accept_host_permissions:
        abort_with_cli_error(
            CLIError(
                code="codex_plugin_permission_confirmation_required",
                message="安装前必须传入 --accept-host-permissions 确认宿主权限风险",
                exit_code=EXIT_CODE_VALIDATION,
            ),
            context="Plugin",
        )

    async def operation(bridge):
        return await bridge.install_plugin(
            plugin_id,
            marketplace_name=marketplace_name,
            accept_undeclared_permissions=True,
        )

    host, result = _call_codex(operation)
    payload = {
        "item": _codex_inventory_payload(result.inventory),
        "host": {"id": "codex-app-server", "version": host.version, "available": True},
        "authPolicy": result.auth_policy,
        "appsNeedingAuth": list(result.apps_needing_auth),
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"Codex 插件已安装: {result.inventory.plugin_id}")


@codex_plugins.command("uninstall", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_id")
def uninstall_codex_plugin(plugin_id: str) -> None:
    """请求 Codex App Server 卸载插件。"""

    async def operation(bridge):
        return await bridge.uninstall_plugin(plugin_id)

    host, result = _call_codex(operation)
    payload = {
        "ecosystem": "codex",
        "integrationMode": "bridged",
        "pluginId": result.plugin_id,
        "installed": False,
        "enabled": False,
        "host": {"id": "codex-app-server", "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"Codex 插件已卸载: {plugin_id}")


@plugin.group("dsh", context_settings=CONTEXT_SETTINGS)
def dsh_plugins() -> None:
    """通过原生 DeepSeek Harness Profile 管理 DSH 插件。"""


@dsh_plugins.command("list", context_settings=CONTEXT_SETTINGS)
def list_dsh_plugins() -> None:
    """列出当前隔离 Profile 中的 DSH bundle。"""

    host, items = _call_dsh(lambda bridge: bridge.list_plugins())
    payload = {
        "ecosystem": "dsh",
        "integrationMode": "bridged",
        "profile": _dsh_profile(),
        "host": {"id": host.host_id, "version": host.version, "available": True},
        "items": [_dsh_inventory_payload(item) for item in items],
    }
    if is_json_output():
        emit_json(payload)
        return
    print_info(f"DeepSeek Harness {host.version} · {len(items)} 个 Profile 插件")
    for item in payload["items"]:
        state = "已启用" if item["enabled"] else "已停用"
        click.echo(f"{item['name']}  {item.get('version') or '-'}  {state}")


@dsh_plugins.command("info", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_name")
def dsh_plugin_info(plugin_name: str) -> None:
    """显示一个 DSH 插件的原生 Profile inventory。"""

    host, item = _call_dsh(lambda bridge: bridge.get_plugin(plugin_name))
    payload = {
        "item": _dsh_inventory_payload(item),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件: {plugin_name}")
        print_kv("状态", "已启用" if item.enabled else "已停用")


@dsh_plugins.command("install", context_settings=CONTEXT_SETTINGS)
@click.argument("source")
@click.option(
    "--accept-host-permissions",
    is_flag=True,
    help="确认 DSH 包与安装脚本会以当前宿主用户权限运行。",
)
def install_dsh_plugin(source: str, accept_host_permissions: bool) -> None:
    """把一个 DSH bundle 安装到隔离 Profile；安装后默认停用。"""

    if not accept_host_permissions:
        abort_with_cli_error(
            CLIError(
                code="dsh_plugin_permission_confirmation_required",
                message="安装前必须传入 --accept-host-permissions 确认宿主权限风险",
                exit_code=EXIT_CODE_VALIDATION,
            ),
            context="Plugin",
        )
    host, item = _call_dsh(
        lambda bridge: bridge.install_plugin(source, accept_host_permissions=True)
    )
    payload = {
        "item": _dsh_inventory_payload(item),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已安装并保持停用: {item.name}；请显式执行 enable")


@dsh_plugins.command("enable", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_name")
def enable_dsh_plugin(plugin_name: str) -> None:
    """在 DSH Profile 中启用已安装的 bundle。"""

    host, item = _call_dsh(lambda bridge: bridge.set_enabled(plugin_name, enabled=True))
    payload = {
        "item": _dsh_inventory_payload(item),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已启用: {plugin_name}")


@dsh_plugins.command("disable", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_name")
def disable_dsh_plugin(plugin_name: str) -> None:
    """从 DSH Profile 组合中停用 bundle，但保留已安装包。"""

    host, item = _call_dsh(lambda bridge: bridge.set_enabled(plugin_name, enabled=False))
    payload = {
        "item": _dsh_inventory_payload(item),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已停用: {plugin_name}")


@dsh_plugins.command("update", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_name")
@click.option(
    "--accept-host-permissions",
    is_flag=True,
    help="确认升级后的包与安装脚本会以当前宿主用户权限运行。",
)
def update_dsh_plugin(plugin_name: str, accept_host_permissions: bool) -> None:
    """由 DSH/pnpm 更新一个插件；失败时恢复旧 Profile。"""

    if not accept_host_permissions:
        abort_with_cli_error(
            CLIError(
                code="dsh_plugin_permission_confirmation_required",
                message="升级前必须传入 --accept-host-permissions 确认宿主权限风险",
                exit_code=EXIT_CODE_VALIDATION,
            ),
            context="Plugin",
        )
    host, item = _call_dsh(
        lambda bridge: bridge.update_plugin(plugin_name, accept_host_permissions=True)
    )
    payload = {
        "item": _dsh_inventory_payload(item),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已更新: {plugin_name}")


@dsh_plugins.command("uninstall", context_settings=CONTEXT_SETTINGS)
@click.argument("plugin_name")
def uninstall_dsh_plugin(plugin_name: str) -> None:
    """从 DSH Profile 卸载一个 bundle。"""

    host, _ = _call_dsh(lambda bridge: bridge.uninstall_plugin(plugin_name))
    payload = {
        "ecosystem": "dsh",
        "integrationMode": "bridged",
        "profile": _dsh_profile(),
        "name": plugin_name,
        "installed": False,
        "enabled": False,
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH 插件已卸载: {plugin_name}")


@dsh_plugins.command("profile", context_settings=CONTEXT_SETTINGS)
def dsh_profile_info() -> None:
    """预检并显示当前 DSH Profile 的无 Secret 配置摘要。"""

    host, projection = _call_dsh(lambda bridge: bridge.project_profile())
    payload = {
        "profile": projection.model_dump(mode="json", by_alias=True),
        "host": {"id": host.host_id, "version": host.version, "available": True},
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"DSH Profile 已通过预检: {projection.profile}")
        print_kv("配置摘要", projection.config_digest)


# DSH is the canonical plugin ecosystem.  Reuse the exact same Click command
# objects at the top level so output, validation and typed errors cannot drift
# between ``plugin list`` and the compatibility alias ``plugin dsh list``.
for _dsh_command_alias in (
    list_dsh_plugins,
    dsh_plugin_info,
    install_dsh_plugin,
    enable_dsh_plugin,
    disable_dsh_plugin,
    update_dsh_plugin,
    uninstall_dsh_plugin,
    dsh_profile_info,
):
    plugin.add_command(_dsh_command_alias)
