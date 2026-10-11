"""Ticket-authenticated WebSocket gateway for one remote RoboICL policy run.

Put a TLS reverse proxy in front of this loopback-only process. The gateway
validates one-time HMAC tickets, starts a policy worker for their fixed task,
and relays the pinned RoboDojo RPC bytes without decoding or changing them.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from roboicl.paths import CODE
from roboicl.transport import transport_options
from roboicl.policy_sessions import load_ticket_secret, verify_ticket


_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


@dataclass
class _RelaySession:
    simulator: object | None = None
    external_policy: object | None = None
    transport: dict | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


def _http_error(status: int, reason: str, body: bytes):
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    return Response(status, reason, Headers([
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]), body)


class PolicyGateway:
    def __init__(self, state_dir: Path, *, worker_timeout: int = 7200, max_workers: int = 8):
        self.secret = load_ticket_secret()
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self._lock_file = (self.state_dir / "gateway.lock").open("a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another RoboICL policy gateway already owns this state directory") from exc
        self.db_path = self.state_dir / "sessions.sqlite3"
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS consumed_sessions (
                sid TEXT PRIMARY KEY, run_id TEXT NOT NULL, task TEXT NOT NULL, seed INTEGER NOT NULL,
                consumed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(consumed_sessions)")}
            if "run_id" not in columns:
                db.execute("ALTER TABLE consumed_sessions ADD COLUMN run_id TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS active_worker (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), sid TEXT NOT NULL,
                pid INTEGER NOT NULL, proc_start TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS active_workers (
                sid TEXT PRIMARY KEY, pid INTEGER NOT NULL, proc_start TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS consumed_external_sessions (
                sid TEXT PRIMARY KEY, consumed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
            )""")
        self._recover_worker()
        self._purge_old_sessions()
        self.worker_timeout = worker_timeout
        self.max_workers = max_workers
        self.active_sids: set[str] = set()
        self.active_external_sids: set[str] = set()
        self.relay_sessions: dict[str, _RelaySession] = {}
        self.guard = threading.Lock()
        self.relay_guard = threading.Lock()
        self.connection_claims = {}
        self.closed = False

    def _db(self):
        gateway = self

        class DB:
            def __enter__(self):
                self.conn = sqlite3.connect(gateway.db_path, timeout=10)
                self.conn.row_factory = sqlite3.Row
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
                self.conn.close()
        return DB()

    def _purge_old_sessions(self):
        cutoff = int(time.time()) - 7 * 24 * 60 * 60
        with self._db() as db:
            old = db.execute("SELECT sid FROM consumed_sessions WHERE consumed_at < ?", (cutoff,)).fetchall()
            db.execute("DELETE FROM consumed_sessions WHERE consumed_at < ?", (cutoff,))
            db.execute("DELETE FROM consumed_external_sessions WHERE consumed_at < ?", (cutoff,))
        for row in old:
            (self.state_dir / f"{row['sid']}.log").unlink(missing_ok=True)
            try:
                shutil.rmtree(self.state_dir / row["sid"])
            except OSError:
                pass

    @staticmethod
    def _proc_start(pid: int) -> str | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            return raw[raw.rfind(")") + 2:].split()[19]
        except (OSError, IndexError):
            return None

    def _recover_worker(self):
        with self._db() as db:
            rows = db.execute("SELECT pid,proc_start FROM active_worker WHERE singleton=1").fetchall()
            rows += db.execute("SELECT pid,proc_start FROM active_workers").fetchall()
            db.execute("DELETE FROM active_worker WHERE singleton=1")
            db.execute("DELETE FROM active_workers")
        for row in rows:
            if self._proc_start(row["pid"]) != row["proc_start"]:
                continue
            try:
                os.killpg(row["pid"], signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._proc_start(row["pid"]) == row["proc_start"]:
                time.sleep(0.1)
            if self._proc_start(row["pid"]) == row["proc_start"]:
                try:
                    os.killpg(row["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _claims(self, request):
        path = urlsplit(request.path)
        segments = path.path.strip("/").split("/")
        sid = segments[-1] if segments else ""
        route = segments[0] if len(segments) == 2 else ""
        if path.query or route not in {"session", "external"} or not _SESSION_ID.fullmatch(sid):
            raise ValueError("invalid session route")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("missing ticket")
        claims = verify_ticket(self.secret, authorization[7:])
        claims.setdefault("role", "simulator")
        if claims.get("sid") != sid:
            raise ValueError("ticket route mismatch")
        expected_role = "simulator" if route == "session" else "external_policy"
        if claims.get("role") != expected_role:
            raise ValueError("ticket role mismatch")
        if (not isinstance(claims.get("task"), str)
                or not (CODE / "configs/tasks" / f"{claims['task']}.json").is_file()
                or type(claims.get("seed")) is not int
                or not 0 <= claims["seed"] <= 1_000_000
                or type(claims.get("shots")) is not int
                or claims["shots"] not in (0, 1)
                or type(claims.get("layout")) is not int
                or not 0 <= claims["layout"] <= 1_000_000
                or not isinstance(claims.get("profile"), str)
                or not claims["profile"]
                or claims.get("reasoning_effort") is not None
                   and claims.get("reasoning_effort") not in _EFFORTS):
            raise ValueError("invalid ticket scope")
        if expected_role == "simulator" and (
                not isinstance(claims.get("run_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", claims["run_id"])):
            raise ValueError("invalid simulator ticket scope")
        return claims

    def process_request(self, connection, request):
        try:
            claims = self._claims(request)
        except ValueError:
            return _http_error(401, "Unauthorized", b"unauthorized\n")
        with self.guard:
            sid = claims["sid"]
            if claims["role"] == "simulator":
                if sid in self.active_sids:
                    return _http_error(409, "Conflict", b"simulator session already connected\n")
                if len(self.active_sids) >= self.max_workers:
                    return _http_error(503, "Service Unavailable", b"policy relays busy\n")
                try:
                    with self._db() as db:
                        db.execute("""INSERT INTO consumed_sessions
                            (sid,run_id,task,seed,consumed_at,expires_at) VALUES (?,?,?,?,?,?)""",
                                   (sid, claims["run_id"], claims["task"], claims["seed"],
                                    int(time.time()), claims["exp"]))
                except sqlite3.IntegrityError:
                    return _http_error(401, "Unauthorized", b"simulator ticket already used\n")
                self.active_sids.add(sid)
            else:
                if sid in self.active_external_sids:
                    return _http_error(409, "Conflict", b"external policy session already connected\n")
                if len(self.active_external_sids) >= self.max_workers:
                    return _http_error(503, "Service Unavailable", b"policy relays busy\n")
                try:
                    with self._db() as db:
                        db.execute("""INSERT INTO consumed_external_sessions
                            (sid,consumed_at,expires_at) VALUES (?,?,?)""",
                                   (sid, int(time.time()), claims["exp"]))
                except sqlite3.IntegrityError:
                    return _http_error(401, "Unauthorized", b"external policy ticket already used\n")
                self.active_external_sids.add(sid)
        self.connection_claims[id(connection)] = claims
        return None

    async def handler(self, client):
        claims = self.connection_claims.pop(id(client), None)
        if claims is None:
            await client.close(code=1008, reason="missing policy session")
            return
        sid = claims["sid"]
        role = claims["role"]
        slot = "simulator" if role == "simulator" else "external_policy"
        duplicate = False
        with self.relay_guard:
            pair = self.relay_sessions.setdefault(sid, _RelaySession())
            selected = transport_options(claims)
            if getattr(pair, slot) is not None or (pair.transport is not None and pair.transport != selected):
                duplicate = True
            else:
                pair.transport = selected
                setattr(pair, slot, client)
                if pair.simulator is not None and pair.external_policy is not None:
                    pair.ready.set()
        if duplicate:
            # A mismatched second role never enters the normal handler finally
            # block. Release its admission slot without removing a live peer.
            if getattr(pair, slot) is None:
                with self.guard:
                    (self.active_sids if role == "simulator" else self.active_external_sids).discard(sid)
            await client.close(code=1008, reason="duplicate or mismatched session peer")
            return
        try:
            wait_seconds = max(1, min(self.worker_timeout, claims["exp"] - int(time.time())))
            ready_task = asyncio.create_task(pair.ready.wait())
            closed_task = asyncio.create_task(client.wait_closed())
            done, pending = await asyncio.wait(
                (ready_task, closed_task), timeout=wait_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                raise asyncio.TimeoutError
            if self.closed or pair.done.is_set():
                return
            if pair.simulator is None or pair.external_policy is None:
                return
            if role == "simulator":
                await self._relay_pair(sid, pair)
            else:
                await asyncio.wait_for(pair.done.wait(), timeout=self.worker_timeout)
        except asyncio.TimeoutError:
            print(f"policy session {sid} timed out waiting for its peer", file=sys.stderr, flush=True)
            try:
                await client.close(code=1013, reason="external policy peer unavailable")
            except Exception:
                pass
        except Exception:
            print(f"policy session {sid} failed ({type(sys.exc_info()[1]).__name__})",
                  file=sys.stderr, flush=True)
            try:
                await client.close(code=1011, reason="policy relay failed")
            except Exception:
                pass
        finally:
            peer = None
            with self.relay_guard:
                if getattr(pair, slot) is client:
                    setattr(pair, slot, None)
                peer = pair.external_policy if role == "simulator" else pair.simulator
                pair.done.set()
                pair.ready.set()
                if pair.simulator is None and pair.external_policy is None:
                    self.relay_sessions.pop(sid, None)
            if peer is not None:
                try:
                    await peer.close(code=1011, reason="policy peer disconnected")
                except Exception:
                    pass
            with self.guard:
                if role == "simulator":
                    self.active_sids.discard(sid)
                else:
                    self.active_external_sids.discard(sid)

    async def _relay_pair(self, sid: str, pair: _RelaySession):
        simulator, external = pair.simulator, pair.external_policy
        to_external = asyncio.create_task(self._copy(simulator, external))
        to_simulator = asyncio.create_task(self._copy(external, simulator))
        try:
            done, pending = await asyncio.wait(
                (to_external, to_simulator), timeout=self.worker_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                print(f"policy session {sid} exceeded its time limit", file=sys.stderr, flush=True)
                await simulator.close(code=1001, reason="policy session time limit reached")
                await external.close(code=1001, reason="policy session time limit reached")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        finally:
            pair.done.set()
            for peer in (simulator, external):
                try:
                    await peer.close()
                except Exception:
                    pass

    @staticmethod
    async def _copy(source, target):
        async for message in source:
            if not isinstance(message, (bytes, bytearray)):
                raise ValueError("policy RPC messages must be binary")
            await target.send(message)

    async def serve(self, host: str, port: int):
        import websockets
        async with websockets.serve(
            self.handler, host, port,
            process_request=self.process_request,
            max_size=32 * 1024 * 1024, compression=None,
            ping_interval=30, ping_timeout=600,
        ) as server:
            try:
                await self.stop_event.wait()
            finally:
                self.closed = True
                for pair in list(self.relay_sessions.values()):
                    pair.done.set()
                    pair.ready.set()
                    for peer in (pair.simulator, pair.external_policy):
                        if peer is not None:
                            try:
                                await peer.close(code=1001, reason="policy gateway shutting down")
                            except Exception:
                                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ROBOICL_POLICY_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ROBOICL_POLICY_GATEWAY_PORT", "8765")))
    parser.add_argument("--state-dir", default=os.environ.get("ROBOICL_POLICY_GATEWAY_STATE", str(CODE / ".roboicl-policy-gateway")))
    parser.add_argument("--worker-timeout", type=int, default=7200)
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("ROBOICL_POLICY_GATEWAY_MAX_WORKERS", "8")))
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or not 60 <= args.worker_timeout <= 4 * 60 * 60
            or not 1 <= args.max_workers <= 64):
        parser.error("port, worker timeout, or max workers is outside allowed range")
    gateway = PolicyGateway(Path(args.state_dir).expanduser().resolve(),
                            worker_timeout=args.worker_timeout, max_workers=args.max_workers)

    async def run_gateway():
        gateway.stop_event = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, gateway.stop_event.set)
        await gateway.serve(args.host, args.port)

    try:
        asyncio.run(run_gateway())
    except KeyboardInterrupt:
        pass
    finally:
        fcntl.flock(gateway._lock_file, fcntl.LOCK_UN)
        gateway._lock_file.close()


if __name__ == "__main__":
    main()
