"""Security gates for a public pilot: webhook signature verification and
dashboard auth. Both are off by default (no env vars set) so the demo and
tests keep working untouched; both come from the environment at runtime,
never from the repo. Stdlib only.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def verify_webhook_signature(secret, body, header):
    """Check Meta's X-Hub-Signature-256 header: HMAC-SHA256 over the raw
    request body, keyed by the app secret, sent as ``sha256=<hex>``.

    Constant-time comparison, so a wrong signature leaks nothing about
    how close it was. Anything missing or malformed is a plain no.
    """
    if not secret or not header:
        return False
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def check_basic_auth(password, header):
    """Check an HTTP Basic Authorization header against one shared
    password. The username is ignored — the password is the whole gate.
    Anything missing or malformed is a plain no.
    """
    if not password or not header:
        return False
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    _, sep, given = decoded.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(password, given)
