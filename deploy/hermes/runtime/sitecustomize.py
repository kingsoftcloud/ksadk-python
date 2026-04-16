from __future__ import annotations

import os


if str(os.getenv("HERMES_HOSTED_RUNTIME", "")).strip().lower() not in {"", "0", "false", "no", "off"}:
    from hosted_gateway import apply_hosted_patches

    apply_hosted_patches()
