"""Small Web Push boundary; subscription capability URLs are never logged."""
from __future__ import annotations

import json
import os
from typing import Any


def public_key() -> str:
    return os.environ.get("WP_VAPID_PUBLIC_KEY", "").strip()


def configured() -> bool:
    subject = os.environ.get("WP_VAPID_SUBJECT", "").strip()
    return bool(
        public_key()
        and os.environ.get("WP_VAPID_PRIVATE_KEY", "").strip()
        and (subject.startswith("mailto:") or subject.startswith("https://"))
    )


def send_web_push(subscription: dict[str, Any], *, title: str, body: str, url: str, tag: str) -> None:
    if not configured():
        raise RuntimeError("Web Push is not configured on this server")
    try:
        from pywebpush import webpush
    except ImportError as error:  # pragma: no cover - deployment configuration failure
        raise RuntimeError("The Web Push package is not installed") from error
    payload = json.dumps({
        "title": str(title)[:120], "body": str(body)[:300],
        "url": url if str(url).startswith("/") else "/dashboard",
        "tag": str(tag)[:120],
    }, separators=(",", ":"))
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=os.environ["WP_VAPID_PRIVATE_KEY"].strip(),
            vapid_claims={"sub": os.environ["WP_VAPID_SUBJECT"].strip()},
            timeout=10,
        )
    except Exception as error:
        # pywebpush exceptions can contain the capability-bearing endpoint and
        # provider response body. Preserve only type/status for diagnostics.
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        suffix = f" status {status}" if isinstance(status, int) else ""
        raise RuntimeError(f"Web Push delivery failed ({type(error).__name__}{suffix})") from None
