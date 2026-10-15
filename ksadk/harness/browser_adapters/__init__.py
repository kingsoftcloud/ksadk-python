"""Optional browser adapters; importing this package does not load Playwright."""

from .native_playwright import NativePlaywrightBrowser
from .native_vision import NativeVisionBrowser

__all__ = ["NativePlaywrightBrowser", "NativeVisionBrowser"]
