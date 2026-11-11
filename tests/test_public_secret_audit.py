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
        "endpoint=internal.example.invalid demo=fixture-agent scm=ezone\n",
        encoding="utf-8",
    )
    code_path = tmp_path / "ksadk" / "client.py"
    code_path.parent.mkdir(parents=True)
    code_path.write_text(
        'SUPPORTED_SCM = "ezone"\n', encoding="utf-8"
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


def test_git_inventory_scans_chinese_named_file_contents(tmp_path, monkeypatch, capsys):
    audit = _load_module()
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    relative = "docs/公开参考.md"
    path = tmp_path / relative
    path.parent.mkdir()
    secret = "AKIA" + "A" * 16
    path.write_text(secret + "\n", encoding="utf-8")
    audit.subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    audit.subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    audit.subprocess.run(["git", "add", "--", relative], cwd=tmp_path, check=True)

    assert audit.main() == 1
    assert capsys.readouterr().out.strip() == f"{relative}:1:{secret}"


def test_public_secret_audit_skips_private_markers_in_non_exported_docs_but_not_secrets(
    tmp_path,
):
    audit = _load_module()
    internal_path = tmp_path / "docs" / "superpowers" / "handoff.md"
    internal_path.parent.mkdir(parents=True)
    secret_line = "Secret" + "AccessKey=not-a-placeholder"
    internal_path.write_text(
        "scm=ezone endpoint=internal.example.invalid\n" + secret_line + "\n",
        encoding="utf-8",
    )

    hits = audit.audit_paths(tmp_path, ["docs/superpowers/handoff.md"])

    assert hits == [f"docs/superpowers/handoff.md:2:{secret_line}"]
