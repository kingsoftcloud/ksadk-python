from __future__ import annotations

import json
import os
import re
from pathlib import Path

import click

_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_pairs(items: tuple[str, ...] | list[str] | None) -> dict[str, str]:
    """Parse repeated KEY=VALUE CLI env options."""
    parsed: dict[str, str] = {}
    for raw_item in items or ():
        item = str(raw_item or "").strip()
        if not item or "=" not in item:
            raise ValueError(f"自定义环境变量格式错误: {raw_item!r}，应为 KEY=VALUE")
        key, value = item.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"自定义环境变量名不合法: {key!r}，请使用合法的环境变量名")
        parsed[key] = value
    return parsed


def load_env_file(env_file: str | None, *, base_dir: Path | None = None) -> dict[str, str]:
    """Load explicit runtime env variables from a dotenv or JSON object file."""
    if not env_file:
        return {}

    path = Path(env_file)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if not path.exists():
        raise ValueError(f"环境变量文件不存在: {path}")

    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(f"环境变量 JSON 文件必须是对象: {path}")
        items = raw.items()
    else:
        from dotenv import dotenv_values

        items = dotenv_values(path, encoding="utf-8-sig").items()

    parsed: dict[str, str] = {}
    for raw_key, raw_value in items:
        if not raw_key or raw_value is None:
            continue
        key = str(raw_key).lstrip("\ufeff").strip()
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"环境变量名不合法: {key!r}，文件: {path}")
        parsed[key] = str(raw_value)
    return parsed


def resolve_explicit_env_vars(
    *,
    env_file: str | None,
    env_pairs: tuple[str, ...] | list[str] | None,
    base_dir: Path,
) -> dict[str, str]:
    """Resolve explicit env vars from ``--env-file``/``--env`` (no auto-``./.env``).

    Used by the generic ``agentengine deploy``/``launch`` which read model
    config from ``agentengine.yaml``, not from ``./.env``. Framework-specific
    deploys (hermes/openclaw) use ``resolve_runtime_env_overrides`` instead,
    which auto-discovers ``./.env`` and returns shell_keys for os.environ merge.
    """
    env_vars = load_env_file(env_file, base_dir=base_dir)
    env_vars.update(parse_env_pairs(env_pairs))
    return env_vars


def apply_explicit_env_with_shell_priority(
    base_env: dict[str, str],
    cli_env: dict[str, str],
    auto_dotenv: dict[str, str],
    shell_keys: set[str],
) -> dict[str, str]:
    """Merge env into base with precedence: cli_env > shell > auto_dotenv.

    ``shell_keys`` is the snapshot of ``os.environ`` keys captured BEFORE any
    env load. ``cli_env`` (``--env``/``--env-file``) is the highest explicit
    CLI intent and overwrites everything. ``auto_dotenv`` (auto-discovered
    ``./.env``) only fills keys not already set by shell.
    """
    for key, value in auto_dotenv.items():
        if key in shell_keys:
            continue
        base_env.setdefault(key, value)
    for key, value in cli_env.items():
        base_env[key] = value
    return base_env


def inject_env_to_environ(
    cli_env: dict[str, str],
    auto_dotenv: dict[str, str],
    shell_keys: set[str],
) -> int:
    """Apply env to ``os.environ`` with precedence: cli_env > shell > auto_dotenv.

    ``auto_dotenv`` only fills keys absent from shell (shell wins over auto
    ``./.env``). ``cli_env`` overwrites shell (explicit CLI intent wins).
    Returns the number of keys injected from ``auto_dotenv`` (for user-facing
    messages); ``cli_env`` overwrites are not counted.
    """
    loaded = 0
    for key, value in auto_dotenv.items():
        if key in shell_keys:
            continue
        os.environ.setdefault(key, value)
        loaded += 1
    for key, value in cli_env.items():
        os.environ[key] = value
    return loaded


def resolve_runtime_env_overrides(
    *,
    env_file: str | None,
    extra_env: tuple[str, ...] | list[str] | None,
    base_dir: Path,
) -> tuple[dict[str, str], dict[str, str], set[str], str | None]:
    """Resolve runtime env overrides with precedence ``--env > --env-file > shell > auto ./.env``.

    Returns ``(cli_env, auto_dotenv, shell_keys, source)``:
    - ``cli_env``: merged ``--env-file`` + ``--env`` (``--env`` wins); explicit
      CLI intent, overwrites shell at merge time.
    - ``auto_dotenv``: auto-discovered ``./.env`` (only when ``env_file`` is
      None); does NOT overwrite shell.
    - ``shell_keys``: snapshot of ``os.environ`` captured before any load.
    - ``source``: the file path used (for user-facing messages) or ``None``.
    """
    shell_keys = set(os.environ)
    explicit_file_env = load_env_file(env_file, base_dir=base_dir) if env_file else {}
    auto_dotenv: dict[str, str] = {}
    auto_source: str | None = None
    if not env_file:
        auto_path = base_dir / ".env"
        if auto_path.exists():
            auto_dotenv = load_env_file(str(auto_path), base_dir=base_dir)
            auto_source = str(auto_path)
    pair_env = parse_env_pairs(extra_env)
    cli_env = {**explicit_file_env, **pair_env}
    return cli_env, auto_dotenv, shell_keys, env_file or auto_source


def env_options(func):
    func = click.option(
        "--env-file",
        type=click.Path(exists=False, dir_okay=False),
        help="额外运行时环境变量文件，支持 .env 或 JSON 对象",
    )(func)
    func = click.option(
        "--env",
        "extra_env",
        multiple=True,
        help="额外透传运行时环境变量，格式 KEY=VALUE，可重复传入",
    )(func)
    return func
