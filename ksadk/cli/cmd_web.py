"""ksadk web - 启动统一本地 Web UI。"""

import asyncio
import os
import webbrowser
from pathlib import Path

import click
import yaml

from ksadk.cli.error_utils import ensure_json_output_supported, print_exception
from ksadk.cli.local_runtime import reexec_with_project_venv_if_needed
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)
from ksadk.configs import setup_environment
from ksadk.detection import FrameworkDetector
from ksadk.runners.factory import create_runner

_PERSISTENT_STM_FRAMEWORKS = {"adk", "langgraph", "langchain", "deepagents"}
_STM_ENV_NAMES = (
    "KSADK_STM_BACKEND",
    "KSADK_STM_PATH",
    "KSADK_STM_URL",
    "KSADK_STM_DB_PATH",
    "KSADK_STM_DB_URL",
)
_SESSION_ENV_NAMES = (
    "KSADK_SESSION_BACKEND",
    "AGENTENGINE_SESSION_BACKEND",
    "KSADK_SESSION_PATH",
    "KSADK_SESSION_DSN",
)
_CHECKPOINT_ENV_NAMES = (
    "KSADK_CHECKPOINT_BACKEND",
    "KSADK_CHECKPOINT_PATH",
    "KSADK_LANGGRAPH_CHECKPOINT_DSN",
)
_LOCAL_UI_ENV_NAMES = ("AGENTENGINE_UI_DIR",)


def _normalize_ui_path(path: str | None) -> str:
    value = str(path or "/").strip() or "/"
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/"


def _load_agentengine_config(agent_path: Path) -> dict:
    for file_name in ("agentengine.yaml", "ksadk.yaml", "ksadk.yml"):
        config_path = agent_path / file_name
        if not config_path.exists():
            continue
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
    return {}


def _project_dotenv_values(agent_path: Path) -> dict[str, str]:
    env_path = agent_path / ".env"
    if not env_path.exists():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    values: dict[str, str] = {}
    for key, value in dotenv_values(env_path, encoding="utf-8-sig").items():
        if key and value is not None:
            values[str(key)] = str(value)
    return values


def _explicit_env_names_excluding_project_dotenv(
    names: tuple[str, ...],
    project_dotenv: dict[str, str],
) -> set[str]:
    explicit: set[str] = set()
    for name in names:
        current = os.environ.get(name)
        if current is None:
            continue
        if project_dotenv.get(name) == current:
            continue
        explicit.add(name)
    return explicit


def _configure_custom_ui_env(agent_path: Path) -> str:
    config = _load_agentengine_config(agent_path)
    if str(config.get("ui_profile") or "").strip().lower() != "custom":
        return "/"

    bundle_path = str(config.get("ui_bundle_path") or "").strip()
    if not bundle_path:
        return "/"
    bundle_dir = agent_path / bundle_path
    if not (bundle_dir / "index.html").is_file():
        return "/"

    ui_path = _normalize_ui_path(str(config.get("ui_path") or "/"))
    os.environ["KSADK_UI_PROFILE"] = "custom"
    os.environ["KSADK_UI_PATH"] = ui_path
    os.environ["KSADK_UI_BUNDLE_PATH"] = bundle_path
    return ui_path


def _ensure_langgraph_sqlite_checkpoint_available() -> None:
    try:
        import langgraph.checkpoint.sqlite.aio  # noqa: F401
    except ImportError:
        print_error(
            "本地 LangGraph checkpoint 需要安装 langgraph-checkpoint-sqlite：\n"
            "  pip install langgraph-checkpoint-sqlite\n"
            "或显式设置 KSADK_CHECKPOINT_BACKEND=memory/postgres。"
        )
        raise SystemExit(1)


def _configure_langgraph_checkpoint_env(
    agent_path: Path,
    *,
    explicit_checkpoint_env_names: set[str],
) -> None:
    checkpoint_backend = str(os.environ.get("KSADK_CHECKPOINT_BACKEND") or "").strip().lower()
    if checkpoint_backend == "local":
        checkpoint_backend = "sqlite"
        os.environ["KSADK_CHECKPOINT_BACKEND"] = "sqlite"

    if checkpoint_backend == "sqlite":
        _ensure_langgraph_sqlite_checkpoint_available()
        os.environ.setdefault(
            "KSADK_CHECKPOINT_PATH",
            str(agent_path / ".agentengine" / "ui" / "checkpoints.sqlite"),
        )
        os.environ.pop("KSADK_LANGGRAPH_CHECKPOINT_DSN", None)
        return

    if explicit_checkpoint_env_names.intersection(_CHECKPOINT_ENV_NAMES):
        return

    _ensure_langgraph_sqlite_checkpoint_available()
    os.environ["KSADK_CHECKPOINT_BACKEND"] = "sqlite"
    os.environ["KSADK_CHECKPOINT_PATH"] = str(
        agent_path / ".agentengine" / "ui" / "checkpoints.sqlite"
    )
    os.environ.pop("KSADK_LANGGRAPH_CHECKPOINT_DSN", None)


def _default_project_stm_if_unset(
    framework: str,
    agent_path: Path,
    *,
    explicit_session_env_names: set[str] | None = None,
    explicit_checkpoint_env_names: set[str] | None = None,
) -> None:
    if framework not in _PERSISTENT_STM_FRAMEWORKS:
        return
    explicit_session_env_names = explicit_session_env_names or set()
    explicit_checkpoint_env_names = explicit_checkpoint_env_names or set()
    session_db_path = str(agent_path / ".agentengine" / "ui" / "sessions.sqlite")
    if not any(name in os.environ for name in _STM_ENV_NAMES):
        os.environ["KSADK_STM_BACKEND"] = "sqlite"
        os.environ["KSADK_STM_PATH"] = session_db_path
    if not explicit_session_env_names.intersection(_SESSION_ENV_NAMES):
        os.environ["KSADK_SESSION_BACKEND"] = "local"
        os.environ["KSADK_SESSION_PATH"] = session_db_path
        os.environ.pop("KSADK_SESSION_DSN", None)
        os.environ.pop("AGENTENGINE_SESSION_BACKEND", None)
    if framework == "langgraph":
        _configure_langgraph_checkpoint_env(
            agent_path,
            explicit_checkpoint_env_names=explicit_checkpoint_env_names,
        )


def configure_local_runtime_persistence(
    agent_path: Path,
    framework: str,
    *,
    explicit_session_env_names: set[str] | None = None,
    explicit_checkpoint_env_names: set[str] | None = None,
    explicit_local_ui_env_names: set[str] | None = None,
) -> None:
    """Bind locally-run agent state to its project unless the shell overrides it."""

    project_dotenv = _project_dotenv_values(agent_path)
    if explicit_session_env_names is None:
        explicit_session_env_names = _explicit_env_names_excluding_project_dotenv(
            (*_STM_ENV_NAMES, *_SESSION_ENV_NAMES),
            project_dotenv,
        )
    if explicit_checkpoint_env_names is None:
        explicit_checkpoint_env_names = _explicit_env_names_excluding_project_dotenv(
            _CHECKPOINT_ENV_NAMES,
            project_dotenv,
        )
    if explicit_local_ui_env_names is None:
        explicit_local_ui_env_names = _explicit_env_names_excluding_project_dotenv(
            _LOCAL_UI_ENV_NAMES,
            project_dotenv,
        )

    os.environ["KSADK_PROJECT_DIR"] = str(agent_path)
    local_ui_dir = str(agent_path / ".agentengine" / "ui")
    if "AGENTENGINE_UI_DIR" not in explicit_local_ui_env_names:
        os.environ["AGENTENGINE_UI_DIR"] = local_ui_dir
    else:
        os.environ.setdefault("AGENTENGINE_UI_DIR", local_ui_dir)
    _default_project_stm_if_unset(
        framework,
        agent_path,
        explicit_session_env_names=explicit_session_env_names,
        explicit_checkpoint_env_names=explicit_checkpoint_env_names,
    )


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option("--port", "-p", default=8080, help="Web UI 端口")
@click.option("--model", help="指定模型名称 (覆盖 .env 配置)")
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
def web(agent_dir: str, port: int, model: str, no_open: bool):
    """启动本地统一 Web UI（Invoke UI）

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    \b
    用途说明:
      本地调试 Agent Invoke UI（非云端 Dashboard）
      所有受支持框架统一使用 ksadk 内建 Web UI
    """
    ensure_json_output_supported(
        "agentengine web",
        suggestion=(
            "请改用 `agentengine dashboard open` 或 " "`agentengine agent status --output json`。"
        ),
    )

    agent_path = Path(agent_dir).resolve()
    command_args = ["web", str(agent_path), "--port", str(port)]
    if model:
        command_args.extend(["--model", model])
    if no_open:
        # re-exec 进项目 venv 时透传 --no-open,否则子进程仍会打开浏览器
        command_args.append("--no-open")
    reexec_with_project_venv_if_needed(agent_path, command_args)
    project_dotenv = _project_dotenv_values(agent_path)
    explicit_session_env_names = _explicit_env_names_excluding_project_dotenv(
        (*_STM_ENV_NAMES, *_SESSION_ENV_NAMES),
        project_dotenv,
    )
    explicit_checkpoint_env_names = _explicit_env_names_excluding_project_dotenv(
        _CHECKPOINT_ENV_NAMES,
        project_dotenv,
    )
    explicit_local_ui_env_names = _explicit_env_names_excluding_project_dotenv(
        _LOCAL_UI_ENV_NAMES,
        project_dotenv,
    )

    print_title("启动本地调试 Web UI")
    print_kv("项目目录", str(agent_path))

    # 设置模型名称 (CLI 参数优先级最高)
    if model:
        os.environ["MODEL_NAME"] = model
        os.environ["OPENAI_MODEL_NAME"] = model
        print_kv("指定模型", model, value_style="#58a6ff")

    setup_environment(agent_path)

    # 检测框架
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()

    if result.type.value == "unknown":
        print_error("未检测到支持的框架")
        raise SystemExit(1)

    if result.type.value == "codex":
        raw_config = getattr(result, "raw_config", None) or {}
        if str(raw_config.get("artifact_type") or "").strip().lower() == "managedruntime":
            from ksadk.managed_runtime import (
                ManagedRuntimeError,
                resolve_local_managed_runtime,
            )

            try:
                resolved_runtime = asyncio.run(
                    resolve_local_managed_runtime(
                        raw_config,
                        region=os.getenv("KSYUN_REGION", "cn-beijing-6"),
                    )
                )
            except ManagedRuntimeError as exc:
                print_error(str(exc))
                raise SystemExit(1) from exc
            runtime_config = raw_config.setdefault("runtime", {})
            if isinstance(runtime_config, dict):
                runtime_config["version"] = resolved_runtime.version
            print_kv(
                "Runtime",
                f"{resolved_runtime.name}@{resolved_runtime.version}",
                value_style="#58a6ff",
            )
            if resolved_runtime.source == "installed-unlocked":
                print_warn(
                    "离线使用本机已安装 Runtime；云端构建前请显式锁定版本"
                    "或连接 AgentEngine 获取默认版本"
                )

    # Map framework types to display names
    framework_map = {
        "adk": "ADK",
        "langchain": "LangChain",
        "langgraph": "LangGraph",
        "deepagents": "DeepAgents",
        "codex": "Codex",
    }
    display_name = framework_map.get(result.type.value, result.name)
    print_kv("框架", display_name, value_style="#2da44e")

    configure_local_runtime_persistence(
        agent_path,
        result.type.value,
        explicit_session_env_names=explicit_session_env_names,
        explicit_checkpoint_env_names=explicit_checkpoint_env_names,
        explicit_local_ui_env_names=explicit_local_ui_env_names,
    )
    launch_path = _configure_custom_ui_env(agent_path)

    try:
        print_info("初始化 Runner...")
        runner = create_runner(result, str(agent_path))
    except Exception as e:
        print_exception("Runner 初始化失败", e)
        raise SystemExit(1)

    print_success("启动统一 Web UI")
    launch_url = f"http://localhost:{port}{launch_path if launch_path != '/' else ''}"
    print_kv("Web UI", launch_url, value_style="#58a6ff")
    print_kv("Agent", result.name)
    print_info("按 Ctrl+C 停止")

    if not no_open:
        webbrowser.open(launch_url)

    try:
        runner.run_server(port=port)
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as e:
        print_exception("统一 Web UI 启动失败", e)
        raise SystemExit(1)
