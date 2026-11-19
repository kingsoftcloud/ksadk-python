"""Same-origin presentation proxy; the browser never receives service credentials."""

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from ksadk.harness.service_presentation import ENGINE_VIEW_PATH, VIEW_HASH
from ksadk.harness.service_runtime import harness_service_from_env

_VIEW_HEADERS = {
    "cache-control": "private, max-age=31536000, immutable",
    "content-security-policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; sandbox allow-scripts"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def register_harness_service_view(app: FastAPI) -> None:
    @app.api_route(
        ENGINE_VIEW_PATH + "/{sha256}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def get_view(request: Request, sha256: str) -> Response:
        if not VIEW_HASH.fullmatch(sha256):
            raise HTTPException(status_code=404)
        try:
            service = harness_service_from_env()
            html = await service.view_html(sha256) if service is not None else None
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=502, detail="Harness view unavailable") from error
        if html is None:
            raise HTTPException(status_code=404)
        return Response(
            content=b"" if request.method == "HEAD" else html,
            media_type="text/html",
            headers=_VIEW_HEADERS,
        )
