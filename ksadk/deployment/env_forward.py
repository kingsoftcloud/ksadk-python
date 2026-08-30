"""部署时的 shell 进程环境变量转发规则。

通用 deploy (serverless/kcf/kce) 与 hermes/openclaw deploy 共用同一套规则：
按前缀 (KSADK_/OPENAI_/KSYUN_/E2B_) + 显式 allowlist 转发 shell 环境变量，
denylist 中的 CLI/builders/configs/web 模块本地键不转发。
"""

import os
from typing import Mapping, MutableMapping, Optional

from ksadk.configs.env_registry import ENV_VAR_REGISTRY

DEPLOY_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        spec.name
        for spec in ENV_VAR_REGISTRY
        if spec.module
        not in {
            "builders",
            "cli",
            "configs",
            "web",
        }
    }
) | frozenset(
    {
        "E2B_API_KEY",
        "E2B_API_URL",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL_NAME",
        "SKILL_SPACE_ID",
        "KSYUN_ACCESS_KEY",
        "KSYUN_ACCOUNT_ID",
        "KSYUN_REGION",
        "KSYUN_SECRET_KEY",
    }
)
DEPLOY_PROCESS_ENV_PREFIXES = ("KSADK_", "OPENAI_", "KSYUN_", "E2B_")
DEPLOY_PROCESS_ENV_DENYLIST = frozenset(
    {spec.name for spec in ENV_VAR_REGISTRY if spec.module in {"builders", "cli", "configs", "web"}}
) | frozenset(
    {
        "KSADK_GLOBAL_CONFIG_ENV_KEYS",
        "KSADK_UPDATED_AT",
        "KSADK_VERSION",
    }
)


def should_forward_process_env(name: str) -> bool:
    if name in DEPLOY_PROCESS_ENV_DENYLIST:
        return False
    return name in DEPLOY_PROCESS_ENV_ALLOWLIST or name.startswith(DEPLOY_PROCESS_ENV_PREFIXES)


def forward_shell_process_env(
    base_env: MutableMapping[str, str],
    environ: Optional[Mapping[str, str]] = None,
) -> MutableMapping[str, str]:
    """把 shell 进程环境中符合转发规则的键补进 ``base_env`` (setdefault 语义)。

    不覆盖 ``base_env`` 已有的键 —— 调用方已 resolve 的值 (如 OPENAI_BASE_URL)
    优先；本函数只负责把白名单/前缀内、但调用方未显式处理的键 (如 KSYUN_*)
    带进 deploy payload。显式 --env/--env-file 由调用方在之后覆盖。
    """
    source = os.environ if environ is None else environ
    for key, value in sorted(source.items()):
        if value and should_forward_process_env(key):
            base_env.setdefault(key, value)
    return base_env
