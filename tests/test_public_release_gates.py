from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _makefile() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def _target_dependencies(makefile: str, target: str) -> set[str]:
    match = re.search(rf"^{re.escape(target)}:\s*(?P<deps>[^\n]*)$", makefile, re.MULTILINE)
    assert match, f"missing Makefile target: {target}"
    return set(match.group("deps").split())


def test_external_publish_targets_require_review_gate_and_publish_state_check():
    makefile = _makefile()

    for target in ("publish", "publish-test"):
        deps = _target_dependencies(makefile, target)
        assert "open-source-approval-check" in deps
        assert "public-preflight" in deps
        assert "public-publish-check" in deps


def test_public_release_tag_requires_approval_check():
    makefile = _makefile()

    deps = _target_dependencies(makefile, "public-release-tag")

    assert "open-source-approval-check" in deps
    assert "public-preflight" in deps
    assert "public-publish-check" in deps
    assert "内部审核" not in makefile


def test_publication_state_make_target_uses_valid_phase():
    makefile = _makefile()

    match = re.search(
        r"^open-source-publication-state:\n(?P<body>(?:\t.*\n)+)",
        makefile,
        re.MULTILINE,
    )
    assert match
    body = match.group("body")

    assert "--phase placeholder" not in body
    assert "--phase pre-publish" in body or 'PUBLIC_PUBLISH_PHASE' in body


def test_public_test_and_ci_cover_release_gate_and_runtime_markdown_tests():
    makefile = _makefile()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for required in (
        "tests/test_check_approval_record.py",
        "tests/test_public_release_gates.py",
        "tests/test_markdown_repair.py",
        "tests/test_conversation_runtime.py",
        "tests/test_server_session_app.py",
    ):
        assert required in makefile
        assert required in ci
