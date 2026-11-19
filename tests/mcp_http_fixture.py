"""Build the same local HTTP MCP fixture under both supported SDK families."""

import inspect


def streamable_app(server, **settings):
    if "json_response" in inspect.signature(server.streamable_http_app).parameters:
        return server.streamable_http_app(**settings)
    for key, value in settings.items():
        setattr(server.settings, key, value)
    return server.streamable_http_app()
