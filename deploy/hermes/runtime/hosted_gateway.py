from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path


HOSTED_RUNTIME_ENV = "HERMES_HOSTED_RUNTIME"
GATEWAY_PID_ENV = "HERMES_GATEWAY_PID_FILE"
_ORIGINAL_GATEWAY_COMMAND = None


def is_hosted_runtime() -> bool:
    raw = str(os.getenv(HOSTED_RUNTIME_ENV, "")).strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _gateway_pid_file() -> Path:
    explicit = str(os.getenv(GATEWAY_PID_ENV, "")).strip()
    if explicit:
        return Path(explicit)
    run_dir = str(os.getenv("HERMES_RUN_DIR", "")).strip()
    if run_dir:
        return Path(run_dir) / "gateway.pid"
    return Path.home() / ".hermes" / "run" / "gateway.pid"


def _read_gateway_pid() -> int | None:
    pid_path = _gateway_pid_file()
    if not pid_path.exists():
        return None
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def hosted_gateway_status() -> tuple[bool, int | None]:
    pid = _read_gateway_pid()
    if _pid_alive(pid):
        return True, pid
    return False, None


def restart_hosted_gateway(*, timeout_seconds: float = 15.0, poll_seconds: float = 0.25) -> tuple[bool, int | None]:
    old_pid = _read_gateway_pid()
    if old_pid and _pid_alive(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.time() + max(timeout_seconds, poll_seconds)
    while time.time() < deadline:
        current_pid = _read_gateway_pid()
        if _pid_alive(current_pid) and current_pid != old_pid:
            return True, current_pid
        time.sleep(poll_seconds)

    current_pid = _read_gateway_pid()
    if _pid_alive(current_pid):
        return True, current_pid
    return False, None


def _print_hosted_gateway_banner() -> None:
    from hermes_cli.colors import Colors, color
    from hermes_cli.setup import print_info, print_success, print_warning

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.MAGENTA))
    print(color("│             ⚕ Gateway Setup                            │", Colors.MAGENTA))
    print(color("├─────────────────────────────────────────────────────────┤", Colors.MAGENTA))
    print(color("│  Configure messaging platforms for the hosted gateway. │", Colors.MAGENTA))
    print(color("│  Press Ctrl+C at any time to exit.                     │", Colors.MAGENTA))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.MAGENTA))
    print()

    is_running, pid = hosted_gateway_status()
    if is_running:
        print_success(f"Hosted gateway is running under container supervision (pid {pid}).")
    else:
        print_warning("Hosted gateway is managed by the container but is not currently running.")
        print_info("The local supervisor should restart it automatically.")
    print_info("Service installation is skipped in hosted Hermes.")


def hosted_gateway_setup() -> None:
    import hermes_cli.gateway as gateway

    _print_hosted_gateway_banner()

    while True:
        print()
        gateway.print_header("Messaging Platforms")

        menu_items = []
        for platform in gateway._PLATFORMS:
            status = gateway._platform_status(platform)
            menu_items.append(f"{platform['label']}  ({status})")
        menu_items.append("Done")

        choice = gateway.prompt_choice("Select a platform to configure:", menu_items, len(menu_items) - 1)
        if choice == len(gateway._PLATFORMS):
            break

        platform = gateway._PLATFORMS[choice]
        if platform["key"] == "whatsapp":
            gateway._setup_whatsapp()
        elif platform["key"] == "signal":
            gateway._setup_signal()
        elif platform["key"] == "weixin":
            gateway._setup_weixin()
        elif platform["key"] == "feishu":
            gateway._setup_feishu()
        else:
            gateway._setup_standard_platform(platform)

    any_configured = any(
        bool(gateway.get_env_value(p["token_var"]))
        for p in gateway._PLATFORMS
        if p["key"] != "whatsapp"
    ) or (gateway.get_env_value("WHATSAPP_ENABLED") or "").lower() == "true"

    print()
    if not any_configured:
        gateway.print_info("No platforms configured. Run 'hermes gateway setup' when ready.")
        print()
        return

    print(gateway.color("─" * 58, gateway.Colors.DIM))
    is_running, pid = hosted_gateway_status()
    if is_running:
        if gateway.prompt_yes_no("  Restart the container-managed gateway to pick up changes?", True):
            restarted, new_pid = restart_hosted_gateway()
            if restarted:
                rendered_pid = f" (pid {new_pid})" if new_pid else ""
                gateway.print_success(f"  Hosted gateway restarted{rendered_pid}.")
            else:
                gateway.print_warning("  Hosted gateway restart is still pending.")
                gateway.print_info("  The container supervisor should relaunch it automatically.")
        else:
            gateway.print_info("  Config saved. Restart the pod or rerun setup when you want the gateway to reload.")
    else:
        gateway.print_info("  Config saved. Hosted Hermes starts the gateway automatically inside the container.")
        gateway.print_info("  If it stays down, restart the pod to force a clean container boot.")
    print()


def hosted_gateway_command(args) -> None:
    global _ORIGINAL_GATEWAY_COMMAND
    subcmd = getattr(args, "gateway_command", None)
    if subcmd == "setup":
        try:
            hosted_gateway_setup()
        except KeyboardInterrupt:
            print()
            raise SystemExit(130) from None
        return

    if subcmd == "install":
        print("Hosted Hermes already manages the gateway inside this container.")
        print("Nothing needs to be installed.")
        raise SystemExit(0)

    if subcmd == "uninstall":
        print("Hosted Hermes manages the gateway lifecycle inside the container.")
        print("Uninstall is not applicable here.")
        raise SystemExit(0)

    if subcmd == "start":
        running, pid = hosted_gateway_status()
        if running:
            print(f"Hosted gateway is already running (pid {pid}).")
            raise SystemExit(0)
        restarted, new_pid = restart_hosted_gateway()
        if restarted:
            print(f"Hosted gateway is running (pid {new_pid}).")
            raise SystemExit(0)
        print("Hosted gateway is not running yet. The container supervisor should relaunch it automatically.")
        raise SystemExit(1)

    if subcmd == "restart":
        restarted, new_pid = restart_hosted_gateway()
        if restarted:
            print(f"Hosted gateway restarted (pid {new_pid}).")
            raise SystemExit(0)
        print("Hosted gateway restart timed out. The container supervisor should relaunch it automatically.")
        raise SystemExit(1)

    if subcmd == "stop":
        print("Hosted Hermes keeps the gateway supervised inside the container.")
        print("Stop or restart the pod if you need the gateway to stay down.")
        raise SystemExit(0)

    if _ORIGINAL_GATEWAY_COMMAND is None:
        raise RuntimeError("original gateway command handler is unavailable")
    _ORIGINAL_GATEWAY_COMMAND(args)


def apply_hosted_patches() -> None:
    if not is_hosted_runtime():
        return

    import hermes_constants

    hermes_constants._container_detected = True
    hermes_constants.is_container = lambda: True

    import hermes_cli.gateway as gateway

    gateway.is_container = lambda: True
    gateway.gateway_setup = hosted_gateway_setup
    global _ORIGINAL_GATEWAY_COMMAND
    if _ORIGINAL_GATEWAY_COMMAND is None:
        _ORIGINAL_GATEWAY_COMMAND = gateway.gateway_command
    gateway.gateway_command = hosted_gateway_command
