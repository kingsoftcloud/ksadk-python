"""Compatibility imports for the canonical A2A workload-token providers.

Keep the historical module path bound to the same classes exported by
``ksadk.a2a``; duplicate ABCs break isinstance checks and old pickle globals.
"""

from ksadk.a2a.control_plane import (
    DEFAULT_A2A_TOKEN_DIR,
    ENV_A2A_TOKEN_DIR,
    MAX_WORKLOAD_TOKEN_BYTES,
    FileWorkloadTokenProvider,
    WorkloadTokenProvider,
)

__all__ = [
    "DEFAULT_A2A_TOKEN_DIR",
    "ENV_A2A_TOKEN_DIR",
    "MAX_WORKLOAD_TOKEN_BYTES",
    "FileWorkloadTokenProvider",
    "WorkloadTokenProvider",
]
