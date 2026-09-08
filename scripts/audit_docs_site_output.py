#!/usr/bin/env python3
"""Audit the exported documentation site as it will be served by GitHub Pages."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit


PUBLIC_ORIGIN = "https://kingsoftcloud.github.io"


@dataclass
class HtmlDocument:
    ids: set[str] = field(default_factory=set)
    references: list[tuple[str, str]] = field(default_factory=list)
    html_lang: str | None = None
    alternate_languages: set[str] = field(default_factory=set)
    canonical: str | None = None


class _DocumentParser(HTMLParser):
    _URL_ATTRIBUTES = {
        "a": "href",
        "img": "src",
        "link": "href",
        "script": "src",
        "source": "src",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = HtmlDocument()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        if tag == "html":
            self.document.html_lang = values.get("lang")
        if identifier := values.get("id"):
            self.document.ids.add(identifier)
        if tag == "a" and (name := values.get("name")):
            self.document.ids.add(name)

        attribute = self._URL_ATTRIBUTES.get(tag)
        if attribute and (target := values.get(attribute)):
            self.document.references.append((f"{tag}[{attribute}]", target))

        if tag == "meta" and values.get("property") in {"og:image", "og:url"}:
            if target := values.get("content"):
                self.document.references.append((f"meta[{values['property']}]", target))
        if tag == "meta" and values.get("name") == "twitter:image":
            if target := values.get("content"):
                self.document.references.append(("meta[twitter:image]", target))

        if tag == "link" and values.get("rel") == "alternate":
            if language := values.get("hreflang"):
                self.document.alternate_languages.add(language)
        if tag == "link" and values.get("rel") == "canonical":
            self.document.canonical = values.get("href")


def _read_document(path: Path) -> HtmlDocument:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.document


def _public_page_url(relative: Path, base_path: str) -> str:
    route = relative.as_posix()
    if route == "index.html":
        route = ""
    elif route.endswith("/index.html"):
        route = route[: -len("index.html")]
    return f"{PUBLIC_ORIGIN}{base_path}/{route}"


def _output_candidates(out_dir: Path, route: str) -> tuple[Path, ...]:
    relative = route.lstrip("/")
    candidate = out_dir / relative
    if route.endswith("/"):
        return (candidate / "index.html",)
    return (candidate, candidate / "index.html", candidate.with_suffix(".html"))


def audit_export(out_dir: Path, base_path: str = "/ksadk-python") -> list[str]:
    """Return human-readable failures for one static export."""

    base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
    html_files = sorted(out_dir.rglob("*.html"))
    if not html_files:
        return [f"no HTML files found below {out_dir}"]

    documents = {path: _read_document(path) for path in html_files}
    failures: list[str] = []

    for source, document in documents.items():
        relative = source.relative_to(out_dir)
        route_parts = relative.parts
        expected_language = None
        if route_parts and route_parts[0] == "cn":
            expected_language = "zh-CN"
        elif route_parts and route_parts[0] == "en":
            expected_language = "en"

        if expected_language and document.html_lang != expected_language:
            failures.append(
                f"{relative}: html lang is {document.html_lang!r}, expected {expected_language!r}"
            )

        if len(route_parts) >= 2 and route_parts[0] in {"cn", "en"} and route_parts[1] == "docs":
            if document.canonical is None:
                failures.append(f"{relative}: missing canonical link")
            required_languages = {"zh-CN", "en", "x-default"}
            missing_languages = required_languages - document.alternate_languages
            if missing_languages:
                failures.append(
                    f"{relative}: missing alternate languages {sorted(missing_languages)}"
                )

        page_url = _public_page_url(relative, base_path)
        for kind, raw_target in document.references:
            if raw_target.startswith(("mailto:", "tel:", "javascript:", "data:")):
                continue
            target = urlsplit(urljoin(page_url, raw_target))
            if target.scheme not in {"http", "https"}:
                continue
            if target.hostname in {"localhost", "127.0.0.1"}:
                failures.append(f"{relative}: {kind} uses local URL {raw_target}")
                continue
            if f"{target.scheme}://{target.netloc}" != PUBLIC_ORIGIN:
                continue
            if base_path and not (
                target.path == base_path or target.path.startswith(f"{base_path}/")
            ):
                failures.append(f"{relative}: {kind} escapes deployment base path: {raw_target}")
                continue

            route = target.path[len(base_path) :] if base_path else target.path
            candidates = _output_candidates(out_dir, unquote(route))
            target_path = next((path for path in candidates if path.exists()), None)
            if target_path is None:
                failures.append(f"{relative}: {kind} target does not exist: {raw_target}")
                continue
            if target.fragment and target_path.suffix == ".html":
                target_document = documents.get(target_path)
                if target_document and unquote(target.fragment) not in target_document.ids:
                    failures.append(f"{relative}: fragment does not exist: {raw_target}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs-site/out"))
    parser.add_argument("--base-path", default="/ksadk-python")
    args = parser.parse_args()

    failures = audit_export(args.out, args.base_path)
    if failures:
        print("Documentation export audit failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Documentation export audit passed: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
