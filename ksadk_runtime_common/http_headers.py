"""HTTP response headers for downloads with Unicode filenames."""

from typing import Literal
from urllib.parse import quote


def build_content_disposition(
    filename: str, *, disposition: Literal["inline", "attachment"] = "attachment"
) -> str:
    """Keep headers ASCII and preserve filenames via the UTF-8 filename* parameter."""
    # Controls are not valid filename content in a response header. In
    # particular, never carry CR/LF into the header or its decoded filename.
    safe_name = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
    safe_name = safe_name or "download"
    encoded_name = quote(safe_name, safe="")
    if encoded_name != safe_name:
        return f"{disposition}; filename*=UTF-8''{encoded_name}"
    return f'{disposition}; filename="{safe_name}"'
