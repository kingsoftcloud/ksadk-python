from pathlib import Path

from scripts.audit_docs_site_output import audit_export


def _write_page(root: Path, route: str, body: str, *, language: str = "zh-CN") -> None:
    path = root / route / "index.html" if route else root / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<html lang="{language}"><head>{body}</head></html>', encoding="utf-8")


def test_static_docs_audit_accepts_localized_links_and_metadata(tmp_path: Path):
    metadata = """
      <link rel="canonical" href="https://kingsoftcloud.github.io/ksadk-python/cn/docs/" />
      <link rel="alternate" hreflang="zh-CN" href="https://kingsoftcloud.github.io/ksadk-python/cn/docs/" />
      <link rel="alternate" hreflang="en" href="https://kingsoftcloud.github.io/ksadk-python/en/docs/" />
      <link rel="alternate" hreflang="x-default" href="https://kingsoftcloud.github.io/ksadk-python/cn/docs/" />
    """
    _write_page(tmp_path, "cn/docs", metadata + '<a href="/ksadk-python/en/docs/#start">EN</a>')
    _write_page(tmp_path, "en/docs", metadata + '<i id="start"></i>', language="en")

    assert audit_export(tmp_path) == []


def test_static_docs_audit_rejects_wrong_language_local_urls_and_404s(tmp_path: Path):
    _write_page(
        tmp_path,
        "en/docs",
        '<meta property="og:image" content="http://localhost:3000/og/image.png" />'
        '<a href="/ksadk-python/en/docs/missing/">missing</a>',
    )

    failures = audit_export(tmp_path)
    assert any("html lang" in failure for failure in failures)
    assert any("local URL" in failure for failure in failures)
    assert any("target does not exist" in failure for failure in failures)
