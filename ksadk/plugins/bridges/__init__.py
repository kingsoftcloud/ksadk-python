"""Native-host ecosystem bridges.

Bridges keep each ecosystem's executable ABI inside its native host.  They do
not add a generic plugin execution API to KsADK.
"""

from ksadk.plugins.bridges.codex import (
    CodexAppServerPluginBridge,
    CodexBridgeError,
    CodexPluginApprovalRequired,
    CodexPluginDetail,
    CodexPluginInventory,
    CodexPluginNotFoundError,
)
from ksadk.plugins.bridges.dsh import (
    DshBridgeError,
    DshBridgeHost,
    DshClientBundle,
    DshHostUnavailableError,
    DshPluginApprovalRequired,
    DshPluginInventory,
    DshPluginMutationError,
    DshPluginNotFoundError,
    DshProfilePluginBridge,
    DshProfileProjection,
)

__all__ = [
    "CodexAppServerPluginBridge",
    "CodexBridgeError",
    "CodexPluginApprovalRequired",
    "CodexPluginDetail",
    "CodexPluginInventory",
    "CodexPluginNotFoundError",
    "DshBridgeError",
    "DshBridgeHost",
    "DshClientBundle",
    "DshHostUnavailableError",
    "DshPluginApprovalRequired",
    "DshPluginInventory",
    "DshPluginMutationError",
    "DshPluginNotFoundError",
    "DshProfilePluginBridge",
    "DshProfileProjection",
]
