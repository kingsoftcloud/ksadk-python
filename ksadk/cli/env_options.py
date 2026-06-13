from __future__ import annotations

import json
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
    env_vars = load_env_file(env_file, base_dir=base_dir)
    env_vars.update(parse_env_pairs(env_pairs))
    return env_vars


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
