"""HTML preview helpers shared by local server and deployed runtime routes."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import quote


def _source_list(*sources: str | None) -> str:
    seen: list[str] = []
    for source in sources:
        if source and source not in seen:
            seen.append(source)
    return " ".join(seen)


def _quote_workspace_path(path: str) -> str:
    return "/".join(quote(segment, safe="") for segment in path.split("/") if segment)


def build_workspace_file_base_href(file_path: str) -> str:
    normalized = str(file_path or "").replace("\\", "/").strip("/")
    if "/" not in normalized:
        return "/_ksadk/workspace/v1/files/"
    dir_path = normalized.rsplit("/", 1)[0]
    encoded_dir_path = _quote_workspace_path(dir_path)
    return f"/_ksadk/workspace/v1/files/{encoded_dir_path}/"


def build_workspace_preview_csp(asset_source: str | None = None) -> str:
    script_sources = _source_list(
        "'unsafe-inline'",
        "'unsafe-eval'",
        "'self'",
        "https:",
        asset_source,
    )
    render_asset_sources = _source_list("'self'", "https:", asset_source)
    return "; ".join(
        [
            "sandbox allow-scripts allow-downloads",
            "default-src 'none'",
            f"script-src {script_sources}",
            f"style-src 'unsafe-inline' data: {render_asset_sources}",
            f"img-src data: blob: {render_asset_sources}",
            f"font-src data: {render_asset_sources}",
            f"media-src data: blob: {render_asset_sources}",
            "worker-src blob:",
            "connect-src 'none'",
            "form-action 'none'",
            "base-uri 'self'",
        ]
    )


HASH_ANCHOR_HANDLER = """<script data-ksadk-preview-anchor-handler>
(function() {
  function scrollToHash(rawHref) {
    if (!rawHref || rawHref === '#') return;
    var rawId = rawHref.slice(1);
    var id = rawId;
    try {
      id = decodeURIComponent(rawId);
    } catch (_) {}
    var target = document.getElementById(id) || document.getElementsByName(id)[0];
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
  document.addEventListener('click', function(event) {
    var target = event.target && event.target.closest ? event.target.closest('a') : null;
    if (!target) return;
    var rawHref = target.getAttribute('href') || '';
    if (rawHref.charAt(0) === '#') {
      event.preventDefault();
      scrollToHash(rawHref);
    }
  }, true);
})();
</script>"""


def inject_workspace_html_preview(html_doc: str, file_path: str) -> str:
    base_href = build_workspace_file_base_href(file_path)
    base_tag = f'<base href="{html_lib.escape(base_href, quote=True)}">'
    injection = f"{base_tag}{HASH_ANCHOR_HANDLER}"
    if re.search(r"<head[^>]*>", html_doc, re.IGNORECASE):
        return re.sub(
            r"<head[^>]*>",
            lambda match: match.group() + injection,
            html_doc,
            count=1,
            flags=re.IGNORECASE,
        )
    return injection + html_doc
