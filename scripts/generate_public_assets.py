#!/usr/bin/env python3
"""Validate recorded Studio assets and render public architecture diagrams."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "docs-site" / "public" / "assets"
ARCH_SVG = ASSETS_DIR / "ksadk-runtime-architecture.svg"
ARCH_PNG = ASSETS_DIR / "ksadk-runtime-architecture.png"
ARCH_EN_SVG = ASSETS_DIR / "ksadk-runtime-architecture.en.svg"
ARCH_EN_PNG = ASSETS_DIR / "ksadk-runtime-architecture.en.png"
ARCHITECTURE_ASSETS = (
    (ARCH_SVG, ARCH_PNG),
    (ARCH_EN_SVG, ARCH_EN_PNG),
)
STUDIO_ASSETS = (
    (ASSETS_DIR / "agentkit-studio-overview.png", "PNG", 1),
    (ASSETS_DIR / "agentkit-studio-platform-resources.png", "PNG", 1),
    (ASSETS_DIR / "agentkit-studio-demo.gif", "GIF", 4),
)


def validate_architecture_svg() -> None:
    """Require the checked-in bilingual architecture SVG sources."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    missing = [path for path, _ in ARCHITECTURE_ASSETS if not path.exists()]
    if missing:
        names = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        raise RuntimeError(f"Missing architecture SVG assets: {names}")


def render_architecture_png() -> None:
    """Render checked-in architecture SVGs when requested or absent."""
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to render architecture PNG")
    regenerate = os.environ.get("KSADK_REGENERATE_ARCHITECTURE_PNG") == "1"
    for svg_path, png_path in ARCHITECTURE_ASSETS:
        if png_path.exists() and not regenerate:
            continue
        subprocess.run(
            [
                converter,
                str(svg_path),
                "--width",
                "2880",
                "--height",
                "1800",
                "--output",
                str(png_path),
            ],
            check=True,
        )


def validate_recorded_studio_assets() -> None:
    """Keep README media tied to real Studio captures checked into the repo."""
    for path, expected_format, minimum_frames in STUDIO_ASSETS:
        if not path.is_file():
            raise RuntimeError(f"Missing Studio asset: {path.relative_to(ROOT)}")
        with Image.open(path) as image:
            if image.format != expected_format:
                raise RuntimeError(
                    f"Unexpected format for {path.relative_to(ROOT)}: {image.format}"
                )
            if image.width < 1000 or image.height < 600:
                raise RuntimeError(
                    f"Studio asset is too small: {path.relative_to(ROOT)} {image.size}"
                )
            frame_count = getattr(image, "n_frames", 1)
            if frame_count < minimum_frames:
                raise RuntimeError(
                    f"Studio demo has too few frames: {path.relative_to(ROOT)} {frame_count}"
                )


def main() -> int:
    validate_architecture_svg()
    render_architecture_png()
    validate_recorded_studio_assets()
    for svg_path, png_path in ARCHITECTURE_ASSETS:
        print(f"validated {svg_path.relative_to(ROOT)}")
        print(f"validated {png_path.relative_to(ROOT)}")
    for path, _, _ in STUDIO_ASSETS:
        print(f"validated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
