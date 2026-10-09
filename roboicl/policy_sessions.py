"""Signed short-lived WSS tickets shared by the control API and policy gateway."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time


def load_ticket_secret() -> bytes:
    raw = os.environ.get("ROBOICL_POLICY_TICKET_SECRET")
    if raw:
        try:
            secret = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("ROBOICL_POLICY_TICKET_SECRET must be base64url") from exc
    else:
        filename = os.environ.get("ROBOICL_POLICY_TICKET_SECRET_FILE")
        if not filename:
            raise RuntimeError("configure ROBOICL_POLICY_TICKET_SECRET_FILE")
        path = Path(filename).expanduser()
        if path.stat().st_mode & 0o077:
            raise RuntimeError("policy ticket secret file must be private (mode 0600)")
        secret = path.read_bytes().strip()
    if len(secret) < 32:
        raise RuntimeError("policy ticket signing secret must contain at least 32 bytes")
    return secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_ticket(secret: bytes, claims: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    signature = _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{head}.{body}.{signature}"


def verify_ticket(secret: bytes, token: str, *, now: int | None = None) -> dict:
    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("invalid ticket")
    try:
        head, body, signature = token.split(".")
        signed = f"{head}.{body}".encode("ascii")
        expected = _b64(hmac.new(secret, signed, hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid ticket")
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid ticket") from exc
    if header != {"alg": "HS256", "typ": "JWT"} or not isinstance(claims, dict):
        raise ValueError("invalid ticket")
    current = int(time.time()) if now is None else now
    if type(claims.get("exp")) is not int or claims["exp"] < current:
        raise ValueError("expired ticket")
    if type(claims.get("iat")) is not int or claims["iat"] > current + 30:
        raise ValueError("invalid ticket time")
    role = claims.get("role", "simulator")
    common = {"sid", "role", "task", "shots", "seed", "layout", "profile", "reasoning_effort", "iat", "exp"}
    required = common | ({"run_id"} if role == "simulator" else set())
    # Continue accepting the pre-role simulator ticket shape during a rolling
    # restart. New tickets always include an explicit role.
    legacy_simulator = common - {"role"} | {"run_id"}
    if set(claims) != required and not (role == "simulator" and set(claims) == legacy_simulator):
        raise ValueError("invalid ticket claims")
    if role not in {"simulator", "external_policy"}:
        raise ValueError("invalid ticket role")
    if not isinstance(claims["sid"], str) or len(claims["sid"]) != 32 or any(
        char not in "0123456789abcdef" for char in claims["sid"]
    ):
        raise ValueError("invalid ticket scope")
    if role == "simulator":
        run_id = claims["run_id"]
        if not isinstance(run_id, str) or len(run_id) != 32 or any(
            char not in "0123456789abcdef" for char in run_id
        ):
            raise ValueError("invalid ticket scope")
    if (not isinstance(claims.get("task"), str) or type(claims.get("shots")) is not int
            or type(claims.get("seed")) is not int or type(claims.get("layout")) is not int
            or not isinstance(claims.get("profile"), str)
            or claims.get("reasoning_effort") is not None
               and claims.get("reasoning_effort") not in {"low", "medium", "high", "xhigh", "max"}):
        raise ValueError("invalid ticket scope")
    return claims
