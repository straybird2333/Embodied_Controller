"""Remote policy URL validation and session authentication for the sim client.

The pinned XPolicyLab client accepts a URL but has no header option. Keep that
adaptation here, in RoboICL-owned code, so the vendored transport stays clean.
"""

from __future__ import annotations

import inspect
import ipaddress
import os
from urllib.parse import urlsplit


SESSION_TOKEN_ENV = "ROBOICL_POLICY_SESSION_TOKEN"


def validate_policy_url(url: str, *, allow_insecure_loopback: bool = False) -> str:
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise ValueError("policy URL must be a non-empty URL without whitespace")
    parsed = urlsplit(url)
    if parsed.scheme not in {"wss", "ws"} or not parsed.hostname:
        raise ValueError("policy URL must use wss:// (or explicitly allowed loopback ws://)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("policy URL must not contain credentials, query, or fragment")
    if parsed.scheme == "ws":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not allow_insecure_loopback or not loopback:
            raise ValueError("ws:// is allowed only for explicitly enabled loopback development")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("policy URL has an invalid port") from exc
    return url


def install_session_auth(url: str, token: str | None = None) -> None:
    """Add a bearer session ticket only to this policy URL, including reconnects."""
    token = token if token is not None else os.environ.get(SESSION_TOKEN_ENV)
    if not isinstance(token, str) or not 16 <= len(token) <= 4096 or "\r" in token or "\n" in token:
        raise ValueError(f"{SESSION_TOKEN_ENV} must contain a 16..4096 character session ticket")

    import websockets

    connect = websockets.connect
    if getattr(connect, "_roboicl_session_auth_url", None) == url:
        return
    if getattr(connect, "_roboicl_session_auth_url", None) is not None:
        raise RuntimeError("a different remote policy URL is already configured in this process")

    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError) as exc:
        raise RuntimeError("cannot determine websocket header API; refusing unauthenticated connection") from exc
    header_arg = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    if header_arg not in parameters:
        raise RuntimeError("websocket package has no supported handshake-header argument")

    def connect_with_session(uri, *args, **kwargs):
        if uri != url:
            raise RuntimeError("session ticket cannot be used for a different websocket URL")
        supplied = kwargs.get(header_arg)
        headers = dict(supplied or {})
        if any(str(name).lower() == "authorization" for name in headers):
            raise ValueError("Authorization header is reserved for the RoboICL session ticket")
        headers["Authorization"] = f"Bearer {token}"
        kwargs[header_arg] = headers
        return connect(uri, *args, **kwargs)

    connect_with_session._roboicl_session_auth_url = url
    websockets.connect = connect_with_session
