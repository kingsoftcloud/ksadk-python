"""Fixed diagnostics shared by trusted resource service adapters."""


class ResourceUpstreamAuthorizationError(PermissionError):
    def __init__(self):
        super().__init__("RESOURCE_FORBIDDEN")
