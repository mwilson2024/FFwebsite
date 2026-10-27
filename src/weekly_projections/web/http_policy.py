"""Small, testable HTTP response policies shared by the web application."""

from __future__ import annotations


def cache_control_for(path: str, *, versioned: bool = False) -> str:
    """Return the browser cache policy for one public request path.

    HTML and authenticated API responses remain private. Versioned static
    assets are immutable so repeat page visits do not redownload site chrome.
    """
    if path.startswith("/static/"):
        return (
            "public, max-age=31536000, immutable"
            if versioned else "public, max-age=3600"
        )
    if path == "/manifest.webmanifest":
        return "public, max-age=3600"
    if path == "/service-worker.js":
        return "no-cache"
    return "no-store"
