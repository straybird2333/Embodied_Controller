"""Low overhead, cross-process diagnostic event stream for one run."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4


def _clock_id() -> str:
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = socket.gethostname()
    return boot


def emit(event: str, name: str | None = None, *, source: str | None = None,
         span_id: str | None = None, parent_span_id: str | None = None,
         attempt: int = 1, step: int | None = None, request_id: str | None = None,
         attributes: dict | None = None, path: str | Path | None = None) -> dict | None:
    """Append one bounded-size JSON event. Missing path disables instrumentation."""
    raw_path = path or os.environ.get("ROBOICL_DIAGNOSTICS_PATH")
    if not raw_path:
        return None
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payload = dict(attributes or {})
    # Guard against accidental log/token or oversized object propagation.
    encoded_attributes = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded_attributes.encode("utf-8")) > 16_384:
        payload = {"truncated": True, "preview": encoded_attributes[:4000]}
    event_path = path
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seq = 1
        try:
            with event_path.open("rb") as existing:
                existing.seek(max(0, event_path.stat().st_size - 16_384))
                lines = existing.read().splitlines()
                if lines:
                    seq = int(json.loads(lines[-1]).get("seq", 0)) + 1
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if seq > 20_000:
            fcntl.flock(lock, fcntl.LOCK_UN)
            return None
        row = {
            "seq": seq,
            "event_id": uuid4().hex,
            "source": source or os.environ.get("ROBOICL_DIAGNOSTIC_SOURCE", "unknown"),
            "clock_id": _clock_id(),
            "at_utc": now,
            "mono_ns": time.monotonic_ns(),
            "event": event,
            "name": name,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "attempt": attempt,
            "step": step,
            "request_id": request_id,
            "attributes": payload,
        }
        with event_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        fcntl.flock(lock, fcntl.LOCK_UN)
    return row


@contextmanager
def span(name: str, *, source: str | None = None, parent_span_id: str | None = None,
         attempt: int = 1, step: int | None = None, attributes: dict | None = None):
    span_id = uuid4().hex
    start_ns = time.monotonic_ns()
    emit("span_begin", name, source=source, span_id=span_id,
         parent_span_id=parent_span_id, attempt=attempt, step=step,
         attributes=attributes)
    try:
        yield span_id
    except BaseException as exc:
        emit("span_end", name, source=source, span_id=span_id,
             parent_span_id=parent_span_id, attempt=attempt, step=step,
             attributes={"duration_seconds": (time.monotonic_ns() - start_ns) / 1e9,
                         "outcome": "error", "exception_type": type(exc).__name__})
        raise
    else:
        emit("span_end", name, source=source, span_id=span_id,
             parent_span_id=parent_span_id, attempt=attempt, step=step,
             attributes={"duration_seconds": (time.monotonic_ns() - start_ns) / 1e9,
                         "outcome": "ok"})
