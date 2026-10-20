"""Bounded screenshot evidence used by workspace browser inspection."""

from __future__ import annotations

import math
from io import BytesIO
from typing import Any


def _canvas_visual_metrics(page: Any) -> list[dict[str, Any]]:
    """Return bounded pixel evidence for visible canvases.

    DOM presence and an error-free console do not prove that a WebGL scene
    rendered: a completely blank canvas satisfies both.  Sampling the actual
    Playwright screenshot keeps this diagnostic independent of canvas APIs and
    works for 2D and WebGL content alike.  It is evidence, not a universal pass
    criterion, because a deliberately solid-colour canvas can be valid.
    """

    viewport = page.viewport_size or {"width": 1280, "height": 720}
    viewport_area = max(1, int(viewport["width"]) * int(viewport["height"]))
    metrics: list[dict[str, Any]] = []
    canvases = page.locator("canvas")
    for index in range(min(canvases.count(), 3)):
        canvas = canvases.nth(index)
        box = canvas.bounding_box()
        if not box or box["width"] < 8 or box["height"] < 8:
            continue
        try:
            screenshot = _isolated_canvas_screenshot(page, canvas)
            variation = _image_variation_metrics(screenshot)
        except Exception as exc:  # noqa: BLE001 - diagnostic must not hide page result
            metrics.append({"index": index, "error": str(exc)[:500]})
            continue
        metrics.append(
            {
                "index": index,
                "width": round(float(box["width"]), 1),
                "height": round(float(box["height"]), 1),
                "viewport_coverage": round(
                    float(box["width"] * box["height"]) / viewport_area,
                    4,
                ),
                **variation,
            }
        )
    return metrics

def _isolated_canvas_screenshot(page: Any, canvas: Any) -> bytes:
    """Capture canvas pixels without HUD/DOM overlays inside its bounding box."""

    token = page.evaluate(
        """canvas => {
          const token = `ksadk-${Date.now()}-${Math.random()}`;
          for (const element of document.body.querySelectorAll('*')) {
            if (element === canvas || element.contains(canvas)) continue;
            element.dataset.ksadkVisualToken = token;
            element.dataset.ksadkVisualVisibility = element.style.visibility || '';
            element.style.visibility = 'hidden';
          }
          return token;
        }""",
        canvas.element_handle(),
    )
    try:
        return canvas.screenshot(timeout=5000)
    finally:
        page.evaluate(
            """token => {
              for (const element of document.querySelectorAll(
                `[data-ksadk-visual-token="${token}"]`
              )) {
                element.style.visibility = element.dataset.ksadkVisualVisibility || '';
                delete element.dataset.ksadkVisualToken;
                delete element.dataset.ksadkVisualVisibility;
              }
            }""",
            token,
        )

def _image_variation_metrics(image_bytes: bytes) -> dict[str, Any]:
    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as image:
        sample = image.convert("RGB")
        sample.thumbnail((64, 64))
        quantized = sample.quantize(colors=16)
        histogram = [count for count, _colour in quantized.getcolors() or []]
    total = max(1, sum(histogram))
    dominant_ratio = max(histogram, default=total) / total
    entropy_bits = -sum((count / total) * math.log2(count / total) for count in histogram if count)
    colour_buckets = len(histogram)
    return {
        "colour_buckets": colour_buckets,
        "dominant_ratio": round(dominant_ratio, 4),
        "entropy_bits": round(entropy_bits, 4),
        "visually_uniform": colour_buckets <= 2 or dominant_ratio >= 0.995,
    }

def _browser_inspection_succeeded(
    *,
    response_ok: bool,
    rendered: bool,
    errors: list[str],
    action_results: list[dict[str, Any]],
) -> bool:
    return bool(
        response_ok
        and rendered
        and not errors
        and all(result.get("ok") is True for result in action_results)
    )
