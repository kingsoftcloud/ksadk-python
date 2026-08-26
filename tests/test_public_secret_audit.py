from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "public_secret_audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("public_secret_audit", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_secret_audit_blocks_private_details_in_docs_only(tmp_path):
    audit = _load_module()
    docs_path = tmp_path / "docs-site" / "guide.mdx"
    docs_path.parent.mkdir(parents=True)
    docs_path.write_text(
        "endpoint=aicp.inner.api.ksyun.com demo=0611agent-xiayu scm=ezone\n",
        encoding="utf-8",
    )
    code_path = tmp_path / "ksadk" / "client.py"
    code_path.parent.mkdir(parents=True)
    code_path.write_text(
        'SUPPORTED_ENDPOINT = "aicp.inner.api.ksyun.com"\n', encoding="utf-8"
    )

    hits = audit.audit_paths(
        tmp_path,
        ["docs-site/guide.mdx", "ksadk/client.py"],
    )

    assert len(hits) == 1
    assert hits[0].startswith("docs-site/guide.mdx:1:")


def test_public_secret_audit_keeps_existing_secret_detection(tmp_path):
    audit = _load_module()
    key_path = tmp_path / "config.txt"
    secret_line = "Secret" + "AccessKey=not-a-placeholder"
    key_path.write_text(secret_line + "\n", encoding="utf-8")

    hits = audit.audit_paths(tmp_path, ["config.txt"])

    assert hits == [f"config.txt:1:{secret_line}"]
