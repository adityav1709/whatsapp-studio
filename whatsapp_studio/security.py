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
import os


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


def basic_auth_password(header):
    """The password from an HTTP Basic Authorization header, or None
    when the header is missing or malformed. The username is ignored —
    the password is the whole gate."""
    if not header:
        return None
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[len("Basic "):], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    _, sep, given = decoded.partition(":")
    if not sep:
        return None
    return given


def check_basic_auth(password, header):
    """Check an HTTP Basic Authorization header against one shared
    password. Anything missing or malformed is a plain no.
    """
    if not password:
        return False
    given = basic_auth_password(header)
    if given is None:
        return False
    return hmac.compare_digest(password, given)


def dashboard_password_env_name(shop_id):
    """The environment variable holding one shop dashboard password:
    DASHBOARD_PASSWORD_<SHOP>, with the shop id uppercased and any
    character that cannot appear in an env var turned into an
    underscore. Passwords live in the environment, never in the repo
    or the registry file."""
    safe = "".join(c if c.isalnum() else "_" for c in shop_id.upper())
    return "DASHBOARD_PASSWORD_" + safe


def load_dashboard_passwords(shop_ids, environ=None, master_password=None):
    """One dashboard password per shop, from the environment.

    Returns {shop_id: password} for the shops that have one set; a
    shop with no password set is absent, so it has no owner login.
    Two shops sharing one password string is a ValueError, and so is
    a shop password equal to the master: the password picks the shop,
    so an ambiguous password fails boot loudly instead of silently
    opening the wrong shop.
    """
    env = environ if environ is not None else os.environ
    seen = {}
    if master_password:
        seen[master_password] = "the master password"
    passwords = {}
    for shop_id in shop_ids:
        password = env.get(dashboard_password_env_name(shop_id))
        if not password:
            continue
        if password in seen:
            raise ValueError(
                "Dashboard password for shop {!r} matches {}; "
                "each shop needs its own password.".format(shop_id, seen[password])
            )
        seen[password] = "shop {!r}".format(shop_id)
        passwords[shop_id] = password
    return passwords


def resolve_dashboard_scope(shop_ids, passwords, master_password, header, shop=None):
    """Which shop a dashboard request may see, from its credential.

    Returns ("shop", shop_id) for a shop password, ("master", shop_id)
    when the master password names a shop, ("master", None) when the
    master password names no shop (the picker), or None when the
    credential opens nothing. The password is the only shop selector:
    a shop password never takes a shop, so a shop password with a
    shop in the request is a plain no, and the master only opens a
    shop that exists. Every shop password is compared, never just
    the first match, so timing says nothing about which shop matched.
    """
    given = basic_auth_password(header)
    if given is None:
        return None
    if master_password and hmac.compare_digest(master_password, given):
        if shop is None:
            return ("master", None)
        if shop in shop_ids:
            return ("master", shop)
        return None
    matched = None
    for shop_id, password in passwords.items():
        if hmac.compare_digest(password, given):
            matched = shop_id
    if matched is None:
        return None
    if shop is not None:
        return None
    return ("shop", matched)
