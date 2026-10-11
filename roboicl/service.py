"""Loopback HTTP control API and isolated run supervisor.

This module uses only the standard library. It never imports Isaac Sim,
RoboDojo, XPolicyLab, or the model policy into the API process.
"""

from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import signal
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from roboicl.config import registered_profile_paths
from roboicl.diagnostics import emit
from roboicl.transport import TRANSPORT_DEFAULTS, transport_options, transport_capabilities
from roboicl.paths import CODE, data_root, results_root
from roboicl.policy_sessions import load_ticket_secret, mint_ticket
from roboicl.remote_policy import validate_policy_url


_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_MAX_BODY = 16 * 1024
_MAX_TRANSFER_UPLOAD = 64 * 1024 * 1024
_TRANSFER_RATE_LIMIT = 20
_TRANSFER_CHUNK_BYTES = 1024 * 1024
_MAX_RUN_SECONDS = 4 * 60 * 60
_STOP_GRACE_SECONDS = 20
_TERMINAL = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _safe_int(value, name: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _profile_by_id(profile_id: object) -> tuple[str, dict]:
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("profile must be a configured protocol id")
    matches = []
    for path in registered_profile_paths():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("id") == profile_id:
            matches.append((str(path), value))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("profile id is ambiguous")
    raise ValueError("profile is not registered")


def _task_registered(task: object) -> bool:
    return (isinstance(task, str) and bool(_TASK_ID.fullmatch(task))
            and (CODE / "configs/tasks" / f"{task}.json").is_file())


def _clean_result(value: dict) -> dict:
    """Expose only the evaluator's public scoring fields, never paths/tracebacks."""
    details = {}
    for key, row in (value.get("details") or {}).items():
        if not isinstance(row, dict):
            continue
        item = {}
        for field in ("layout_id", "success", "score", "interrupted", "score_kind", "executed_steps"):
            if field in row and isinstance(row[field], (str, int, float, bool, type(None))):
                item[field] = row[field]
        error = row.get("policy_error")
        if isinstance(error, dict):
            item["policy_error"] = {
                field: error[field] for field in ("method", "classification", "official_score")
                if field in error and isinstance(error[field], (str, int, float, bool, type(None)))
            }
        details[str(key)] = item
    result = {
        field: value[field] for field in ("success_rate", "eval_time", "score")
        if field in value and isinstance(value[field], (str, int, float, bool, type(None)))
    }
    result["details"] = details
    if value.get("policy_error") and isinstance(value["policy_error"], dict):
        error = value["policy_error"]
        result["policy_error"] = {
            field: error[field] for field in ("method", "classification", "official_score")
            if field in error and isinstance(error[field], (str, int, float, bool, type(None)))
        }
    return result


class RunManager:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self._instance_lock = (self.state_dir / "service.lock").open("a+")
        try:
            fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another RoboICL API service already owns this state directory") from exc
        self.db_path = state_dir / "runs.sqlite3"
        self.logs_dir = state_dir / "logs"
        self.logs_dir.mkdir(mode=0o700, exist_ok=True)
        self.diagnostics_dir = state_dir / "diagnostics"
        self.diagnostics_dir.mkdir(mode=0o700, exist_ok=True)
        self.transfer_test_dir = state_dir / "transfer-test"
        self.transfer_test_file = self.transfer_test_dir / "benchmark.tar"
        self.transfer_test_metadata = self.transfer_test_dir / "benchmark.json"
        self._db_lock = threading.RLock()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._active: dict[str, subprocess.Popen] = {}
        self._active_lock = threading.RLock()
        self._leased_gpus: set[int] = set()
        self._stopping = threading.Event()
        self.results = results_root().resolve()
        self.sim_python = self._executable("ROBOICL_SIM_PYTHON", sys.executable)
        self.policy_python = self._executable("ROBOICL_POLICY_PYTHON", sys.executable)
        raw_gpus = os.environ.get("ROBOICL_API_GPUS", os.environ.get("ROBOICL_API_GPU", "0"))
        try:
            self.gpus = tuple(dict.fromkeys(
                _safe_int(int(item.strip()), "gpu", maximum=255)
                for item in raw_gpus.split(",") if item.strip()
            ))
        except ValueError as exc:
            raise RuntimeError("ROBOICL_API_GPUS must be a comma-separated list of GPU indices") from exc
        if not self.gpus:
            raise RuntimeError("ROBOICL_API_GPUS must list at least one GPU")
        self.max_parallel = _safe_int(
            int(os.environ.get("ROBOICL_API_MAX_PARALLEL", str(len(self.gpus)))),
            "max parallel", minimum=1, maximum=len(self.gpus),
        )
        self.max_queue = _safe_int(int(os.environ.get("ROBOICL_API_MAX_QUEUE", "8")), "max queue", maximum=1024)
        self.create_rate_per_min = _safe_int(
            int(os.environ.get("ROBOICL_API_CREATE_RATE_PER_MIN", "40")),
            "create rate per minute", minimum=1, maximum=1000,
        )
        self.max_seconds = _safe_int(
            int(os.environ.get("ROBOICL_API_MAX_RUN_SECONDS", "7200")),
            "max run seconds", minimum=60, maximum=_MAX_RUN_SECONDS,
        )
        self.retention_days = _safe_int(int(os.environ.get("ROBOICL_API_RETENTION_DAYS", "7")),
                                        "retention days", minimum=1, maximum=365)
        self.max_log_bytes = _safe_int(int(os.environ.get("ROBOICL_API_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
                                        "log max bytes", minimum=65536, maximum=100 * 1024 * 1024)
        configured_policy_url = os.environ.get("ROBOICL_POLICY_WSS_URL")
        self.policy_url = None
        if configured_policy_url:
            self.policy_url = validate_policy_url(configured_policy_url, allow_insecure_loopback=False)
            base_path = urlsplit(self.policy_url).path.rstrip("/")
            if not base_path.endswith("/session"):
                raise RuntimeError("ROBOICL_POLICY_WSS_URL must end with /session")
        self.ticket_secret = None
        if os.environ.get("ROBOICL_POLICY_TICKET_SECRET") or os.environ.get("ROBOICL_POLICY_TICKET_SECRET_FILE"):
            self.ticket_secret = load_ticket_secret()
        self._init_db()
        self._recover()
        self._free_gpus: queue.Queue[int] = queue.Queue()
        for gpu in self.gpus:
            self._free_gpus.put(gpu)
        self._threads = [threading.Thread(target=self._worker, name=f"roboicl-run-manager-{i}", daemon=True)
                         for i in range(self.max_parallel)]
        self.diagnostics_capabilities = _diagnostics_capabilities(self)

    @staticmethod
    def _executable(env_name: str, fallback: str) -> str:
        value = os.environ.get(env_name, fallback)
        path = Path(value).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"configured interpreter is unavailable: {env_name}")
        return str(path)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                shots INTEGER NOT NULL,
                seed INTEGER NOT NULL,
                layout INTEGER NOT NULL,
                profile TEXT NOT NULL,
                reasoning_effort TEXT,
                policy_session_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                returncode INTEGER,
                gpu INTEGER,
                pid INTEGER,
                proc_start TEXT,
                error_code TEXT,
                error_summary TEXT,
                actor_id TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS policy_sessions (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                shots INTEGER NOT NULL,
                seed INTEGER NOT NULL,
                layout INTEGER NOT NULL,
                profile TEXT NOT NULL,
                reasoning_effort TEXT,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                run_id TEXT UNIQUE,
                actor_id TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS transfer_tests (
                id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                sha256 TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS transfer_tests_actor ON transfer_tests(actor_id, created_at)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            if "gpu" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN gpu INTEGER")
            for name, declaration in (("diagnostic_mode", "TEXT"),
                                      ("max_steps", "INTEGER"),
                                      ("timeout_seconds", "INTEGER"),
                                      ("actor_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")

            for table in ("runs", "policy_sessions"):
                present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                declarations = {"action_format": "TEXT NOT NULL DEFAULT 'legacy'",
                                "feedback_mode": "TEXT NOT NULL DEFAULT 'per_step'",
                                "observation_encoding": "TEXT NOT NULL DEFAULT 'raw_rgb'",
                                "jpeg_quality": "INTEGER"}
                for name, declaration in declarations.items():
                    if name not in present:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @staticmethod
    def _proc_start(pid: int) -> str | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            tail = raw[raw.rfind(")") + 2:].split()
            return tail[19]  # Linux proc field 22 (starttime), relative to field 3.
        except (OSError, IndexError):
            return None

    @classmethod
    def _same_process(cls, pid: int, start: str | None) -> bool:
        return bool(start and cls._proc_start(pid) == start)

    def _recover(self):
        with self._db() as db:
            rows = db.execute("SELECT id,status,pid,proc_start FROM runs WHERE status IN ('queued','starting','running','cancelling')").fetchall()
            for row in rows:
                if row["pid"] and self._same_process(row["pid"], row["proc_start"]):
                    try:
                        os.killpg(row["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and self._same_process(row["pid"], row["proc_start"]):
                        time.sleep(0.1)
                    if self._same_process(row["pid"], row["proc_start"]):
                        try:
                            os.killpg(row["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                db.execute("UPDATE runs SET status='failed',stage='service_restarted',error_code='service_restarted',error_summary='Run interrupted by service restart',finished_at=?,updated_at=? WHERE id=?",
                           (_now(), _now(), row["id"]))
                db.execute("UPDATE policy_sessions SET status='invalidated' WHERE run_id=?", (row["id"],))

    def start(self):
        for thread in self._threads:
            thread.start()

    def close(self):
        self._stopping.set()
        queued_rows = []
        with self._db_lock, self._db() as db:
            queued_rows = db.execute("SELECT id FROM runs WHERE status='queued'").fetchall()
            db.execute("UPDATE runs SET status='failed',stage='service_shutdown',error_code='service_shutdown',error_summary='Run stopped during service shutdown',finished_at=?,updated_at=? WHERE status='queued'",
                       (_now(), _now()))
            db.execute("UPDATE policy_sessions SET status='cancelled' WHERE status IN ('issued','reserved')")
            db.execute("UPDATE runs SET status='cancelling',stage='cancelling',updated_at=? WHERE status IN ('starting','running')",
                       (_now(),))
        for row in queued_rows:
            run_id = row["id"]
            path = self.diagnostics_dir / f"{run_id}.jsonl"
            opened = next((event for event in self._events(run_id, 0, 1000)
                           if event.get("name") == "queue_wait" and event.get("event") == "span_begin"), None)
            if opened:
                emit("span_end", "queue_wait", source="service", span_id=opened.get("span_id"),
                     attributes={"duration_seconds": max(0.0, (time.monotonic_ns() - opened["mono_ns"]) / 1e9),
                                 "outcome": "service_shutdown"}, path=path)
            emit("instant", "cleanup_complete", source="service",
                 attributes={"gpu_slot_acquired": False, "queued_run_stopped": True}, path=path)
        with self._active_lock:
            active = list(self._active.items())
        for run_id, proc in active:
            self._signal(run_id, proc, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_GRACE_SECONDS + 10
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            with self._active_lock:
                active = list(self._active.items())
            for run_id, proc in active:
                self._signal(run_id, proc, signal.SIGKILL)
            deadline = time.monotonic() + 5
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
        if not any(thread.is_alive() for thread in self._threads):
            fcntl.flock(self._instance_lock, fcntl.LOCK_UN)
            self._instance_lock.close()

    def _require_policy_broker(self):
        if not self.policy_url or not self.ticket_secret:
            raise RuntimeError("policy_unreachable")

    def _session_url(self, session_id: str, *, route: str = "session") -> str:
        parsed = urlsplit(self.policy_url)
        base = parsed.path.rstrip("/")
        if not base.endswith("/session"):
            raise RuntimeError("configured policy WSS URL must end with /session")
        path = base[:-len("/session")] + f"/{route}/{session_id}"
        return parsed._replace(path=path).geturl()

    @staticmethod
    def _external_session_url(session_id: str, public_host: str | None) -> str:
        if not public_host:
            raise RuntimeError("public host is required to issue the external policy URL")
        parsed = urlsplit("//" + public_host)
        if (not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("invalid forwarded public host")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("invalid forwarded public host") from exc
        return f"wss://{parsed.netloc}/external/{session_id}"

    @staticmethod
    def _validated_settings(body: dict, *, allow_ttl: bool = False) -> dict:
        allowed = {"task", "shots", "seed", "layout", "profile", "reasoning_effort"} | set(TRANSPORT_DEFAULTS)
        if allow_ttl:
            allowed.add("ttl_seconds")
        if set(body) - allowed:
            raise ValueError("request contains unsupported fields")
        task = body.get("task")
        if not _task_registered(task):
            raise ValueError("task is not registered")
        shots = _safe_int(body.get("shots"), "shots", maximum=1)
        seed = _safe_int(body.get("seed", 0), "seed")
        layout = _safe_int(body.get("layout", 0), "layout")
        _profile_path, profile = _profile_by_id(body.get("profile"))
        if profile.get("shot_count") not in (None, shots):
            raise ValueError("profile does not support requested shots")
        effort = body.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS):
            raise ValueError("reasoning_effort must be one of low, medium, high, xhigh, max")
        return {"task": task, "shots": shots, "seed": seed, "layout": layout,
                "profile": profile["id"], "reasoning_effort": effort} | transport_options(body)

    def create_session(self, body: dict, actor_id: str | None = None,
                       public_host: str | None = None) -> dict:
        self._require_policy_broker()
        settings = self._validated_settings(body, allow_ttl=True)
        ttl = _safe_int(body.get("ttl_seconds", 900), "ttl_seconds", minimum=60, maximum=1800)
        layout_path = data_root() / "Assets/Eval_Layout/RoboDojo/arx_x5" / str(settings["seed"]) / f"{settings['task']}_{settings['layout']}.json"
        if not (data_root() / ".roboicl-assets.json").is_file() or not layout_path.is_file():
            raise FileNotFoundError("assets_not_ready")
        session_id = uuid4().hex
        issued = int(time.time())
        expires = issued + ttl
        with self._db_lock, self._db() as db:
            db.execute("""INSERT INTO policy_sessions
                (id,task,shots,seed,layout,profile,reasoning_effort,issued_at,expires_at,status,actor_id,action_format,feedback_mode,observation_encoding,jpeg_quality)
                VALUES (?,?,?,?,?,?,?,?,?,'issued',?,?,?,?,?)""",
                (session_id, settings["task"], settings["shots"], settings["seed"],
                 settings["layout"], settings["profile"], settings["reasoning_effort"], issued, expires, actor_id,
                 *(settings[key] for key in TRANSPORT_DEFAULTS)))
        external_claims = {"sid": session_id, "role": "external_policy",
                           "task": settings["task"], "shots": settings["shots"],
                           "seed": settings["seed"], "layout": settings["layout"],
                           "profile": settings["profile"],
                           "reasoning_effort": settings["reasoning_effort"],
                           "iat": issued, "exp": expires} | transport_options(settings)
        external_token = mint_ticket(self.ticket_secret, external_claims)
        return {"policy_session": session_id,
                "external_policy_ws_url": self._external_session_url(session_id, public_host),
                "external_policy_token": external_token,
                "expires_at": dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat(timespec="seconds"),
                **transport_options(settings), "transport_capabilities": transport_capabilities()}

    def create(self, body: dict, actor_id: str | None = None) -> dict:
        self._require_policy_broker()
        allowed = {"task", "shots", "seed", "layout", "profile", "reasoning_effort", "policy_session"} | set(TRANSPORT_DEFAULTS)
        if set(body) - allowed:
            raise ValueError("request contains unsupported fields")
        settings = self._validated_settings({key: value for key, value in body.items()
                                             if key != "policy_session" and key not in TRANSPORT_DEFAULTS})
        profile_path, _ = _profile_by_id(settings["profile"])
        session_id = body.get("policy_session")
        if not isinstance(session_id, str) or not _RUN_ID.fullmatch(session_id):
            raise ValueError("policy_session must be an active session id")
        layout_path = data_root() / "Assets/Eval_Layout/RoboDojo/arx_x5" / str(settings["seed"]) / f"{settings['task']}_{settings['layout']}.json"
        if not (data_root() / ".roboicl-assets.json").is_file() or not layout_path.is_file():
            raise FileNotFoundError("assets_not_ready")
        run_id = uuid4().hex
        now = _now()
        row = {"id": run_id, **settings, "policy_session_id": session_id,
               "status": "queued", "stage": "queued", "created_at": now,
               "updated_at": now, "started_at": None, "finished_at": None,
               "returncode": None, "pid": None, "proc_start": None,
               "error_code": None, "error_summary": None, "actor_id": actor_id}
        with self._db_lock, self._db() as db:
            queued = db.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0]
            if queued >= self.max_queue:
                raise OverflowError("queue_full")
            session = db.execute("SELECT * FROM policy_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None or session["status"] != "issued" or session["actor_id"] != actor_id:
                raise ValueError("policy_session_reused")
            if session["expires_at"] < int(time.time()):
                raise ValueError("policy_session_expired")
            inherited = {key: session[key] for key in TRANSPORT_DEFAULTS}
            selected = transport_options(inherited | {key: body[key] for key in TRANSPORT_DEFAULTS if key in body})
            if selected != inherited:
                raise ValueError("policy_session_transport_mismatch")
            settings.update(selected)
            row.update(selected)
            for key, value in settings.items():
                if session[key] != value:
                    raise ValueError("policy_session_scope_mismatch")
            db.execute("""INSERT INTO runs
                (id,task,shots,seed,layout,profile,reasoning_effort,policy_session_id,status,stage,created_at,updated_at,actor_id,action_format,feedback_mode,observation_encoding,jpeg_quality)
                VALUES (:id,:task,:shots,:seed,:layout,:profile,:reasoning_effort,:policy_session_id,:status,:stage,:created_at,:updated_at,:actor_id,:action_format,:feedback_mode,:observation_encoding,:jpeg_quality)""", row)
            changed = db.execute("UPDATE policy_sessions SET status='reserved',run_id=? WHERE id=? AND status='issued'",
                                 (run_id, session_id)).rowcount
            if not changed:
                raise ValueError("policy_session_reused")
        row["profile_path"] = profile_path
        row["session_exp"] = session["expires_at"]
        row["queue_span_id"] = uuid4().hex
        initial = emit("span_begin", "queue_wait", source="service", span_id=row["queue_span_id"],
                       attributes={"accepted_at": now},
                       path=self.diagnostics_dir / f"{run_id}.jsonl")
        row["queue_started_mono_ns"] = initial["mono_ns"] if initial else time.monotonic_ns()
        self._queue.put(row)
        return self._public(row)

    def create_diagnostic(self, body: dict, actor_id: str | None = None) -> dict:
        allowed = {"task", "shots", "seed", "layout", "profile", "reasoning_effort",
                   "mode", "max_steps", "timeout_seconds"}
        if set(body) - allowed:
            raise ValueError("request contains unsupported fields")
        settings = self._validated_settings({key: value for key, value in body.items()
                                             if key in {"task", "shots", "seed", "layout", "profile", "reasoning_effort"}})
        mode = body.get("mode")
        if mode not in {"first_observation", "fixed_steps"}:
            raise ValueError("mode must be first_observation or fixed_steps")
        max_steps = _safe_int(body.get("max_steps", 0 if mode == "first_observation" else 5),
                              "max_steps", maximum=20)
        if mode == "first_observation" and max_steps != 0:
            raise ValueError("first_observation requires max_steps=0")
        if mode == "fixed_steps" and not 1 <= max_steps <= 20:
            raise ValueError("fixed_steps requires max_steps in [1, 20]")
        timeout = _safe_int(body.get("timeout_seconds", 1800), "timeout_seconds",
                            minimum=60, maximum=3600)
        layout_path = data_root() / "Assets/Eval_Layout/RoboDojo/arx_x5" / str(settings["seed"]) / f"{settings['task']}_{settings['layout']}.json"
        if not (data_root() / ".roboicl-assets.json").is_file() or not layout_path.is_file():
            raise FileNotFoundError("assets_not_ready")
        run_id = uuid4().hex
        now = _now()
        # The legacy schema requires a non-null policy_session_id; diagnostics
        # use an unguessable sentinel and never create/consume a policy session.
        row = {"id": run_id, **settings, "policy_session_id": "diagnostic_" + run_id,
               "status": "queued", "stage": "queued", "created_at": now,
               "updated_at": now, "started_at": None, "finished_at": None,
               "returncode": None, "gpu": None, "pid": None, "proc_start": None,
               "error_code": None, "error_summary": None, "actor_id": actor_id,
               "diagnostic_mode": mode, "max_steps": max_steps,
               "timeout_seconds": timeout}
        with self._db_lock, self._db() as db:
            queued = db.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0]
            if queued >= self.max_queue:
                raise OverflowError("queue_full")
            db.execute("""INSERT INTO runs
                (id,task,shots,seed,layout,profile,reasoning_effort,policy_session_id,status,stage,
                 created_at,updated_at,diagnostic_mode,max_steps,timeout_seconds,actor_id)
                VALUES (:id,:task,:shots,:seed,:layout,:profile,:reasoning_effort,:policy_session_id,
                 :status,:stage,:created_at,:updated_at,:diagnostic_mode,:max_steps,:timeout_seconds,:actor_id)""", row)
        profile_path, _ = _profile_by_id(settings["profile"])
        row["profile_path"] = profile_path
        row["queue_span_id"] = uuid4().hex
        initial = emit("span_begin", "queue_wait", source="service", span_id=row["queue_span_id"],
                       attributes={"accepted_at": now},
                       path=self.diagnostics_dir / f"{run_id}.jsonl")
        row["queue_started_mono_ns"] = initial["mono_ns"] if initial else time.monotonic_ns()
        self._queue.put(row)
        value = self._public(row)
        value.update({"diagnostic_mode": mode, "diagnostic_success": None, "task_success": None,
                      "diagnostics_url": f"/v1/runs/{run_id}/diagnostics",
                      "logs_url": f"/v1/runs/{run_id}/logs"})
        return value

    def _worker(self):
        next_purge = time.monotonic() + 3600
        while not self._stopping.is_set():
            if time.monotonic() >= next_purge:
                self._purge_expired()
                next_purge = time.monotonic() + 3600
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._stopping.is_set():
                break
            if self._status(item["id"]) != "queued":
                continue
            gpu = None
            try:
                while not self._stopping.is_set():
                    if self._status(item["id"]) != "queued":
                        break
                    try:
                        gpu = self._free_gpus.get(timeout=0.5)
                        break
                    except queue.Empty:
                        continue
                if gpu is not None and not self._stopping.is_set():
                    with self._active_lock:
                        self._leased_gpus.add(gpu)
                    elapsed = max(0.0, (time.monotonic_ns() - item.get("queue_started_mono_ns", time.monotonic_ns())) / 1e9)
                    emit("span_end", "queue_wait", source="service",
                         span_id=item.get("queue_span_id"),
                         attributes={"duration_seconds": elapsed, "outcome": "ok", "gpu": gpu},
                         path=self.diagnostics_dir / f"{item['id']}.jsonl")
                    self._execute(item, gpu)
            finally:
                if gpu is not None:
                    self._free_gpus.put(gpu)
                    with self._active_lock:
                        self._leased_gpus.discard(gpu)
                    if self._status(item["id"]) in _TERMINAL:
                        emit("instant", "cleanup_complete", source="service",
                             attributes={"gpu_slot_released": True, "gpu": gpu},
                             path=self.diagnostics_dir / f"{item['id']}.jsonl")

    def _execute(self, item: dict, gpu: int):
        run_id = item["id"]
        diagnostic = item.get("diagnostic_mode") in {"first_observation", "fixed_steps"}
        runner_id = f"api_{run_id}"
        output = self.results / runner_id
        log_path = self.logs_dir / f"{run_id}.log"
        now = int(time.time())
        if not diagnostic and item["session_exp"] < now:
            self._finish(run_id, "failed", "policy_unreachable", "Policy session expired while queued", 1)
            with self._db_lock, self._db() as db:
                db.execute("UPDATE policy_sessions SET status='expired' WHERE id=?", (item["policy_session_id"],))
            return
        cmd = [self.policy_python, "-m", "roboicl.run", item["task"],
               "--shots", str(item["shots"]), "--seed", str(item["seed"]),
               "--layout", str(item["layout"]), "--profile", item["profile_path"],
               "--gpu", str(gpu), "--run-id", runner_id]
        if diagnostic:
            cmd.extend(["--diagnostic-mode", item["diagnostic_mode"],
                        "--max-steps", str(item.get("max_steps", 0))])
        else:
            claims = {"sid": item["policy_session_id"], "role": "simulator",
                      "run_id": run_id, "task": item["task"],
                      "shots": item["shots"], "seed": item["seed"], "layout": item["layout"],
                      "profile": item["profile"], "reasoning_effort": item["reasoning_effort"],
                      "iat": now, "exp": min(now + 900, item["session_exp"])} | transport_options(item)
            ticket = mint_ticket(self.ticket_secret, claims)
            policy_url = self._session_url(item["policy_session_id"])
            cmd.extend(["--policy-url", policy_url])
        if item["reasoning_effort"]:
            cmd.extend(["--reasoning-effort", item["reasoning_effort"]])
        env = os.environ.copy()
        for key in ("ROBOICL_ACCESS_KEY", "ROBOICL_ACCESS_KEY_FILE", "ROBOICL_API_HOST",
                    "ROBOICL_API_PORT", "ROBOICL_POLICY_WSS_URL", "ROBOICL_POLICY_SESSION_TOKEN",
                    "ROBOICL_POLICY_TICKET_SECRET", "ROBOICL_POLICY_TICKET_SECRET_FILE"):
            env.pop(key, None)
        if diagnostic:
            env.pop("ROBOICL_POLICY_SESSION_TOKEN", None)
        else:
            env["ROBOICL_POLICY_SESSION_TOKEN"] = ticket
        diag_path = self.diagnostics_dir / f"{run_id}.jsonl"
        env["ROBOICL_DIAGNOSTICS_PATH"] = str(diag_path)
        env["ROBOICL_DIAGNOSTIC_SOURCE"] = "runner"
        env["ROBOICL_API_RUN_ID"] = run_id
        env["ROBOICL_TRANSPORT_OPTIONS"] = json.dumps(transport_options(item), separators=(",", ":"))
        env["PYTHONUNBUFFERED"] = "1"
        with self._db_lock, self._db() as db:
            changed = db.execute("UPDATE runs SET status='starting',stage='preflight',gpu=?,started_at=?,updated_at=? WHERE id=? AND status='queued'",
                                 (gpu, _now(), _now(), run_id)).rowcount
            if changed:
                db.execute("UPDATE policy_sessions SET status='consumed' WHERE id=? AND run_id=? AND status='reserved'",
                           (item["policy_session_id"], run_id))
        if not changed:
            return
        launch_span_id = uuid4().hex
        launch_started = time.monotonic_ns()
        emit("span_begin", "runner_process_launch", source="service", span_id=launch_span_id,
             attributes={"gpu": gpu, "runner": "roboicl.run", "diagnostic_mode": item.get("diagnostic_mode")},
             path=diag_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            self._finish(run_id, "failed", "run_failed", "Run output identifier already exists", 1)
            if not diagnostic:
                self._close_session(item["policy_session_id"])
            return
        if self._status(run_id) != "starting":
            self._finish(run_id, "cancelled", None, None, 1)
            if not diagnostic:
                self._close_session(item["policy_session_id"])
            return
        try:
            proc = subprocess.Popen(cmd, cwd=CODE, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True, bufsize=0)
        except OSError:
            self._finish(run_id, "failed", "run_failed", "Could not start simulator runner", 127)
            if not diagnostic:
                self._close_session(item["policy_session_id"])
            return
        emit("span_end", "runner_process_launch", source="service", span_id=launch_span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - launch_started) / 1e9,
                         "outcome": "started", "pid": proc.pid},
             path=diag_path)
        emit("instant", "sim_process_started", source="service",
             attributes={"pid": proc.pid, "gpu": gpu}, path=diag_path)
        log_thread = threading.Thread(target=self._drain_log, args=(proc.stdout, log_path),
                                      name=f"roboicl-log-{run_id}", daemon=True)
        log_thread.start()
        proc_start = self._proc_start(proc.pid)
        with self._active_lock:
            self._active[run_id] = proc
        with self._db_lock, self._db() as db:
            db.execute("UPDATE runs SET status=CASE WHEN status='cancelling' THEN 'cancelling' ELSE 'running' END,stage=CASE WHEN status='cancelling' THEN 'cancelling' ELSE 'simulating' END,pid=?,proc_start=?,updated_at=? WHERE id=?",
                       (proc.pid, proc_start, _now(), run_id))
        if self._status(run_id) == "cancelling":
            self._signal(run_id, proc, signal.SIGTERM)
        deadline = time.monotonic() + self.max_seconds
        if diagnostic:
            deadline = time.monotonic() + item.get("timeout_seconds", 1800)
        timed_out = False
        next_sample = time.monotonic()
        while not self._stopping.is_set():
            if time.monotonic() >= next_sample:
                self._record_resource_sample(run_id, proc.pid, gpu)
                next_sample = time.monotonic() + 2.0
            try:
                returncode = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._signal(run_id, proc, signal.SIGTERM)
                    try:
                        returncode = proc.wait(timeout=_STOP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        self._signal(run_id, proc, signal.SIGKILL)
                        returncode = proc.wait(timeout=5)
                    break
        else:
            self._signal(run_id, proc, signal.SIGTERM)
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal(run_id, proc, signal.SIGKILL)
                returncode = proc.wait(timeout=5)
        with self._active_lock:
            self._active.pop(run_id, None)
        log_thread.join(timeout=5)

        current = self._row(run_id)
        if current and current["status"] == "cancelling":
            self._finish(run_id, "cancelled", None, None, returncode)
            if not diagnostic:
                self._close_session(item["policy_session_id"])
            return
        result_path = output / "result.json"
        diagnostic_success = diagnostic and self._diagnostic_success(run_id)
        if timed_out:
            self._finish(run_id, "failed", "run_timeout", "Run exceeded the configured time limit", returncode)
        elif diagnostic_success and returncode == 0:
            emit("instant", "diagnostic_complete", source="service",
                 attributes={"diagnostic_success": True, "task_success": None}, path=diag_path)
            self._finish(run_id, "succeeded", None, None, returncode)
        elif returncode == 0 and result_path.is_file():
            self._finish(run_id, "succeeded", None, None, returncode)
        else:
            self._finish(run_id, "failed", "run_failed", "Simulator run did not produce a completed result", returncode)
        if not diagnostic:
            self._close_session(item["policy_session_id"])

    def _drain_log(self, stream, path: Path):
        total = 0
        truncated = False
        with path.open("ab", buffering=0) as output:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                remaining = max(0, self.max_log_bytes - total)
                if remaining:
                    output.write(chunk[:remaining])
                    total += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    truncated = True
            if truncated:
                output.write(b"\n[service log truncated at configured size limit]\n")
        stream.close()

    def _purge_expired(self):
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        with self._db_lock, self._db() as db:
            rows = db.execute("SELECT id FROM runs WHERE status IN ('succeeded','failed','cancelled') AND finished_at < ?",
                              (cutoff,)).fetchall()
        for row in rows:
            run_id = row["id"]
            shutil.rmtree(self.results / f"api_{run_id}", ignore_errors=True)
            try:
                (self.logs_dir / f"{run_id}.log").unlink(missing_ok=True)
            except OSError:
                continue
            (self.diagnostics_dir / f"{run_id}.jsonl").unlink(missing_ok=True)
            (self.diagnostics_dir / f"{run_id}.jsonl.lock").unlink(missing_ok=True)
            with self._db_lock, self._db() as db:
                db.execute("DELETE FROM runs WHERE id=? AND status IN ('succeeded','failed','cancelled')", (run_id,))
                db.execute("DELETE FROM policy_sessions WHERE run_id=?", (run_id,))
        expired = int(time.time()) - 86400
        with self._db_lock, self._db() as db:
            db.execute("DELETE FROM policy_sessions WHERE run_id IS NULL AND expires_at < ?", (expired,))
            db.execute("DELETE FROM transfer_tests WHERE created_at < ?", (cutoff,))

    def record_transfer_test(self, *, test_id: str, direction: str, actor_id: str,
                             byte_count: int, elapsed_seconds: float, sha256: str | None,
                             status: str) -> dict:
        created_at = _now()
        row = {
            "transfer_test_id": test_id,
            "direction": direction,
            "bytes": byte_count,
            "server_elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
            "server_mib_per_second": round(
                byte_count / max(elapsed_seconds, 1e-9) / (1024 * 1024), 3
            ),
            "sha256": sha256,
            "status": status,
            "recorded_at": created_at,
        }
        with self._db_lock, self._db() as db:
            db.execute("""INSERT INTO transfer_tests
                (id,direction,actor_id,byte_count,elapsed_seconds,sha256,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (test_id, direction, actor_id, byte_count, elapsed_seconds, sha256, status, created_at))
        return row

    def get_transfer_test(self, test_id: str, actor_id: str):
        with self._db_lock, self._db() as db:
            row = db.execute("""SELECT id,direction,byte_count,elapsed_seconds,sha256,status,created_at
                FROM transfer_tests WHERE id=? AND actor_id=?""", (test_id, actor_id)).fetchone()
        if row is None:
            return None
        return {
            "transfer_test_id": row["id"], "direction": row["direction"],
            "bytes": row["byte_count"], "server_elapsed_seconds": round(row["elapsed_seconds"], 6),
            "server_mib_per_second": round(
                row["byte_count"] / max(row["elapsed_seconds"], 1e-9) / (1024 * 1024), 3
            ),
            "sha256": row["sha256"], "status": row["status"], "recorded_at": row["created_at"],
        }

    def recent_transfer_tests(self, actor_id: str, limit: int = 20):
        with self._db_lock, self._db() as db:
            rows = db.execute("""SELECT id,direction,byte_count,elapsed_seconds,sha256,status,created_at
                FROM transfer_tests WHERE actor_id=? ORDER BY created_at DESC LIMIT ?""",
                (actor_id, limit)).fetchall()
        return [self.get_transfer_test(row["id"], actor_id) for row in rows]

    def _close_session(self, session_id: str):
        with self._db_lock, self._db() as db:
            db.execute("UPDATE policy_sessions SET status='closed' WHERE id=? AND status IN ('reserved','consumed')",
                       (session_id,))

    def _signal(self, run_id: str, proc: subprocess.Popen, sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self, run_id: str, actor_id: str | None = None) -> dict | None:
        was_queued = False
        with self._db_lock, self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or (actor_id is not None and row["actor_id"] != actor_id):
                return None
            if row["status"] in _TERMINAL:
                return self._public(dict(row))
            if row["status"] == "queued":
                was_queued = True
                db.execute("UPDATE runs SET status='cancelled',stage='cancelled',finished_at=?,updated_at=? WHERE id=?",
                           (_now(), _now(), run_id))
                db.execute("UPDATE policy_sessions SET status='cancelled' WHERE run_id=? AND status='reserved'",
                           (run_id,))
            else:
                db.execute("UPDATE runs SET status='cancelling',stage='cancelling',updated_at=? WHERE id=?",
                           (_now(), run_id))
        if was_queued:
            path = self.diagnostics_dir / f"{run_id}.jsonl"
            opened = next((event for event in self._events(run_id, 0, 1000)
                           if event.get("name") == "queue_wait" and event.get("event") == "span_begin"), None)
            if opened:
                emit("span_end", "queue_wait", source="service", span_id=opened.get("span_id"),
                     attributes={"duration_seconds": max(0.0, (time.monotonic_ns() - opened["mono_ns"]) / 1e9),
                                 "outcome": "cancelled_before_gpu"}, path=path)
            emit("instant", "cleanup_complete", source="service",
                 attributes={"gpu_slot_acquired": False, "queued_run_cancelled": True}, path=path)
        with self._active_lock:
            proc = self._active.get(run_id)
        if proc:
            self._signal(run_id, proc, signal.SIGTERM)
            threading.Timer(_STOP_GRACE_SECONDS, self._kill_if_active, args=(run_id, proc)).start()
        return self.get(run_id, actor_id)

    def _kill_if_active(self, run_id: str, proc: subprocess.Popen):
        with self._active_lock:
            same = self._active.get(run_id) is proc
        if same and proc.poll() is None:
            self._signal(run_id, proc, signal.SIGKILL)

    def _row(self, run_id: str):
        with self._db_lock, self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def _owned_row(self, run_id: str, actor_id: str):
        row = self._row(run_id)
        if row is None or not actor_id or row.get("actor_id") != actor_id:
            return None
        return row

    def _events(self, run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict]:
        path = self.diagnostics_dir / f"{run_id}.jsonl"
        if not path.is_file():
            return []
        values = []
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("seq", 0) > after_seq:
                        values.append(row)
                        if len(values) >= limit:
                            break
        except (OSError, json.JSONDecodeError):
            pass
        return values

    def diagnostics(self, run_id: str, actor_id: str, after_seq: int = 0, limit: int = 200):
        row = self._owned_row(run_id, actor_id)
        if row is None:
            return None
        events = self._events(run_id, after_seq, limit + 1)
        has_more = len(events) > limit
        events = events[:limit]
        all_events = self._events(run_id, 0, 100_000)
        current = None
        open_spans: dict[str, dict] = {}
        phase_seconds: dict[str, float] = {}
        completed_steps = 0
        first_ready = False
        cleanup_complete = row.get("status") in _TERMINAL and row.get("diagnostic_mode") is None
        diagnostic_success = None
        task_success = None
        for event in all_events:
            name = event.get("name")
            attrs = event.get("attributes") or {}
            if event.get("event") == "span_begin" and event.get("span_id"):
                open_spans[event["span_id"]] = event
                current = event
            elif event.get("event") == "span_end" and event.get("span_id"):
                opened = open_spans.pop(event["span_id"], None)
                duration = attrs.get("duration_seconds")
                if duration is None and opened and opened.get("clock_id") == event.get("clock_id"):
                    duration = max(0.0, (event["mono_ns"] - opened["mono_ns"]) / 1e9)
                if isinstance(duration, (int, float)):
                    phase_seconds[name] = phase_seconds.get(name, 0.0) + float(duration)
                if current and current.get("span_id") == event.get("span_id"):
                    current = None
            if name == "phase":
                current = {"name": attrs.get("phase"), "at_utc": event.get("at_utc"),
                           "mono_ns": event.get("mono_ns"), "clock_id": event.get("clock_id")}
            if name == "completed_steps":
                completed_steps = max(completed_steps, int(attrs.get("completed_steps", 0)))
            if name == "first_observation_ready":
                first_ready = True
            if name == "cleanup_complete":
                cleanup_complete = True
            if name == "diagnostic_complete":
                diagnostic_success = bool(attrs.get("diagnostic_success"))
                task_success = attrs.get("task_success")
        if current is None and open_spans:
            current = max(open_spans.values(), key=lambda event: event.get("seq", 0))
        now_ns = time.monotonic_ns()
        active_elapsed = None
        if current and current.get("clock_id") == (all_events[-1].get("clock_id") if all_events else None):
            active_elapsed = max(0.0, (now_ns - current["mono_ns"]) / 1e9)
        accepted_mono = next((event["mono_ns"] for event in all_events
                              if event.get("name") == "queue_wait" and event.get("event") == "span_begin"), None)
        ready_event = next((event for event in reversed(all_events) if event.get("name") == "first_observation_ready"), None)
        first_seconds = None
        if accepted_mono is not None and ready_event and accepted_mono <= ready_event["mono_ns"]:
            first_seconds = (ready_event["mono_ns"] - accepted_mono) / 1e9
        next_seq = (all_events[-1].get("seq", 0) + 1) if all_events else 1
        return {
            "schema_version": "1", "run_id": run_id, "status": row["status"],
            "diagnostic_mode": row.get("diagnostic_mode"),
            "current_phase": current.get("name") if current else row.get("stage"),
            "phase_started_at": current.get("at_utc") if current else None,
            "phase_elapsed_seconds": round(active_elapsed, 3) if active_elapsed is not None else None,
            "last_heartbeat_at": all_events[-1].get("at_utc") if all_events else None,
            "completed_steps": completed_steps, "first_observation_ready": first_ready,
            "cleanup_complete": cleanup_complete, "diagnostic_success": diagnostic_success,
            "task_success": task_success, "events": events, "next_seq": next_seq,
            "has_more": has_more,
            "resource_samples": [event for event in events if event.get("event") == "resource_sample"],
            "summary": {
                "queue_wait_seconds": round(phase_seconds["queue_wait"], 3) if "queue_wait" in phase_seconds else None,
                "first_observation_ready_seconds": round(first_seconds, 3) if first_seconds is not None else None,
                "phase_seconds": {key: round(value, 3) for key, value in phase_seconds.items()},
                "observation_wire_bytes": sum(int((e.get("attributes") or {}).get("message_bytes") or 0)
                                              for e in all_events if e.get("name") == "observation_rpc"),
                "observation_count": sum(1 for e in all_events if e.get("name") == "observation_rpc"),
                "decision_count": sum(1 for e in all_events if e.get("name") == "policy_action_wait"),
                "rejection_count": sum(1 for e in all_events if e.get("name") == "action_rejected"),
                "warmup_physics_steps": sum(int((e.get("attributes") or {}).get("physics_steps", 0))
                                             for e in all_events if e.get("name") == "warmup" and e.get("event") == "span_end"),
                "action_physics_steps": sum(int((e.get("attributes") or {}).get("physics_steps", 0))
                                             for e in all_events if e.get("name") == "action_execute" and e.get("event") == "span_end"),
                "unknown_intervals": ["Uninstrumented gaps are not assigned to a stage; use event UTC/monotonic timestamps."],
                "latest_resource_sample": next((e for e in reversed(all_events)
                                                 if e.get("event") == "resource_sample"), None),
            },
        }

    def _diagnostic_success(self, run_id: str) -> bool:
        return any(event.get("name") == "diagnostic_complete"
                   and (event.get("attributes") or {}).get("diagnostic_success") is True
                   for event in self._events(run_id, 0, 100_000))

    def _record_resource_sample(self, run_id: str, root_pid: int, gpu: int) -> None:
        path = self.diagnostics_dir / f"{run_id}.jsonl"
        children: dict[int, list[int]] = {}
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "stat").read_text()
                    tail = raw[raw.rfind(")") + 2:].split()
                    children.setdefault(int(tail[1]), []).append(int(entry.name))
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            children = {}
        pids, todo = [], [root_pid]
        while todo:
            pid = todo.pop()
            if pid in pids:
                continue
            pids.append(pid)
            todo.extend(children.get(pid, ()))
        processes = []
        page_size = os.sysconf("SC_PAGE_SIZE")
        for pid in pids:
            try:
                raw = Path(f"/proc/{pid}/stat").read_text()
                tail = raw[raw.rfind(")") + 2:].split()
                io_values = {}
                for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                    key, value = line.split(":", 1)
                    if key in {"read_bytes", "write_bytes"}:
                        io_values[key] = int(value.strip())
                processes.append({"pid": pid, "cpu_ticks": int(tail[11]) + int(tail[12]),
                                  "rss_bytes": max(0, int(tail[21]) * page_size), **io_values})
            except (OSError, ValueError, IndexError):
                continue
        gpu_data = None
        try:
            result = subprocess.run(["nvidia-smi", f"--id={gpu}",
                                     "--query-gpu=utilization.gpu,memory.used,memory.total",
                                     "--format=csv,noheader,nounits"], capture_output=True,
                                    text=True, timeout=1, check=True)
            fields = [int(v.strip()) for v in result.stdout.strip().split(",")]
            gpu_data = {"scope": "whole_gpu", "gpu_utilization_percent": fields[0],
                        "memory_used_mib": fields[1], "memory_total_mib": fields[2]}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            gpu_data = {"scope": "whole_gpu", "available": False}
        emit("resource_sample", "resource_sample", source="service",
             attributes={"gpu": gpu_data, "process_tree": processes,
                         "process_scope": "runner descendants; cumulative counters, per-process RSS"},
             path=path)

    def _status(self, run_id: str):
        row = self._row(run_id)
        return row["status"] if row else None

    def get(self, run_id: str, actor_id: str | None = None) -> dict | None:
        row = self._row(run_id)
        if row is None or (actor_id is not None and row.get("actor_id") != actor_id):
            return None
        value = self._public(row)
        diag = self.diagnostics(run_id, row.get("actor_id"), 0, 1) if row.get("actor_id") else None
        if diag:
            value.update({"diagnostic_mode": diag["diagnostic_mode"],
                          "diagnostic_phase": diag["current_phase"],
                          "completed_steps": diag["completed_steps"],
                          "first_observation_ready": diag["first_observation_ready"],
                          "cleanup_complete": diag["cleanup_complete"],
                          "diagnostic_success": diag["diagnostic_success"]})
        return value

    def _finish(self, run_id: str, status: str, error_code, summary, returncode):
        with self._db_lock, self._db() as db:
            db.execute("UPDATE runs SET status=?,stage=?,error_code=?,error_summary=?,returncode=?,finished_at=?,updated_at=?,pid=NULL,proc_start=NULL WHERE id=?",
                       (status, status, error_code, summary, returncode, _now(), _now(), run_id))

    @staticmethod
    def _public(row: dict) -> dict:
        return {"run_id": row.get("id")} | {key: row.get(key) for key in (
            "task", "shots", "seed", "layout", "profile", "status", "stage",
            "action_format", "feedback_mode", "observation_encoding", "jpeg_quality",
            "created_at", "updated_at", "started_at", "finished_at", "returncode", "gpu",
            "error_code", "error_summary",
        )} | {"diagnostic_mode": row.get("diagnostic_mode"), "completed_steps": None,
             "status_url": f"/v1/runs/{row.get('id')}",
             "cancel_url": f"/v1/runs/{row.get('id')}/cancel"}

    def result(self, run_id: str, actor_id: str | None = None):
        row = self._row(run_id)
        if row is None or (actor_id is not None and row.get("actor_id") != actor_id):
            return None
        path = self.results / f"api_{run_id}" / "result.json"
        if not path.is_file():
            return row["status"], None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return row["status"], None
        return row["status"], _clean_result(value)

    def logs(self, run_id: str, actor_id: str, source: str, tail_lines: int):
        row = self._owned_row(run_id, actor_id)
        if row is None:
            return None, "run_not_found"
        paths = {"runner": self.logs_dir / f"{run_id}.log",
                 "client": self.results / f"api_{run_id}" / "client.log",
                 "camera": self.results / f"api_{run_id}" / "camera.log"}
        if source not in paths:
            return None, "invalid_source"
        path = paths[source]
        if not path.is_file():
            return None, "log_not_found"
        maximum = 256 * 1024
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                if size > maximum:
                    stream.seek(-maximum, os.SEEK_END)
                raw = stream.read(maximum + 1)
            truncated = size > maximum
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if len(lines) > tail_lines:
                lines = lines[-tail_lines:]
                truncated = True
            text = "\n".join(lines)
            text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
            text = re.sub(r"(?i)(api[_-]?key|access[_-]?key|token)(\s*[:=]\s*)[^\s,;\"']+",
                          r"\1\2[REDACTED]", text)
            return {"run_id": run_id, "source": source, "truncated": truncated,
                    "log_time": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat(),
                    "text": text}, None
        except OSError:
            return None, "log_not_found"


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_threads=32, **kwargs):
        self._thread_limit = threading.BoundedSemaphore(max_threads)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._thread_limit.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._thread_limit.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._thread_limit.release()


def _load_access_keys() -> tuple[str, ...]:
    raw = os.environ.get("ROBOICL_ACCESS_KEY")
    if raw:
        keys = (raw.strip(),)
    else:
        file = os.environ.get("ROBOICL_ACCESS_KEY_FILE")
        if not file:
            raise RuntimeError("configure ROBOICL_ACCESS_KEY or ROBOICL_ACCESS_KEY_FILE")
        path = Path(file).expanduser()
        if path.stat().st_mode & 0o077:
            raise RuntimeError("ROBOICL_ACCESS_KEY_FILE must not be accessible to group or other users")
        keys = tuple(line.strip() for line in path.read_text().splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    if not keys or any(not key.isascii() or len(key) < 32 or any(ch.isspace() for ch in key) for key in keys):
        raise RuntimeError("access keys must be at least 32 ASCII characters without whitespace")
    return keys


def _health(manager: RunManager) -> dict:
    manifest = data_root() / ".roboicl-assets.json"
    assets_ready = manifest.is_file() and (data_root() / "Assets").is_dir()
    available = []
    manager.results.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    with manager._active_lock:
        leased = set(manager._leased_gpus)
    for gpu in manager.gpus:
        if gpu in leased:
            continue
        gpu_free = False
        for slot in range(2):
            lock_path = manager.results / f".{host}.gpu{gpu}.slot{slot}.lock"
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                gpu_free = True
                fcntl.flock(lock, fcntl.LOCK_UN)
                break
        if gpu_free:
            available.append(gpu)
    sim_ready = Path(manager.sim_python).is_file()
    broker_ready = False
    if manager.policy_url and manager.ticket_secret:
        parsed = urlsplit(manager.policy_url)
        try:
            raw_socket = socket.create_connection((parsed.hostname, parsed.port or 443), timeout=1)
            with ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname):
                broker_ready = True
        except (OSError, ssl.SSLError):
            pass
    return {"api_version": "1", "ready": assets_ready and sim_ready and broker_ready,
            "assets_ready": assets_ready, "simulator_python_ready": sim_ready,
            "policy_gateway_ready": broker_ready,
            "available_gpu_slots": len(available), "available_gpus": available,
            "configured_gpus": list(manager.gpus), "max_parallel_runs": manager.max_parallel}


def _diagnostics_capabilities(manager: RunManager) -> dict:
    revision = None
    dirty = None
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=CODE,
                                  capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=CODE,
                                    capture_output=True, text=True, timeout=2, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    upstream = {}
    try:
        lock = json.loads((CODE / "configs/upstream.lock.json").read_text(encoding="utf-8"))
        upstream = {item["name"]: item["commit"] for item in lock.get("packages", [])
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                    and isinstance(item.get("commit"), str)}
    except (OSError, ValueError, KeyError):
        upstream = {}
    asset_mount_type = None
    try:
        asset_mount_type = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(data_root())],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    shared_filesystems = {"nfs", "nfs4", "cifs", "smb3", "ceph", "lustre", "glusterfs", "9p"}
    if asset_mount_type:
        asset_storage_class = ("shared" if asset_mount_type.lower() in shared_filesystems
                               or asset_mount_type.lower().startswith(("nfs", "fuse.sshfs"))
                               else "local")
    else:
        asset_storage_class = "unknown"
    return {
        "schema_version": "1", "code_commit": revision, "working_tree_dirty": dirty,
        "diagnostic_schema_version": "1",
        "policy_transport": transport_capabilities(),
        "implemented_endpoints": ["GET /v1/diagnostics/capabilities",
                                   "GET /v1/runs/{run_id}/diagnostics",
                                   "GET /v1/runs/{run_id}/logs",
                                   "POST /v1/diagnostic-runs"],
        "diagnostic_modes": {"first_observation": {"max_steps": 0},
                             "fixed_steps": {"min_steps": 1, "default_steps": 5, "max_steps": 20}},
        "limits": {"diagnostic_timeout_seconds": {"min": 60, "default": 1800, "max": 3600},
                   "diagnostic_events_per_page": {"default": 200, "max": 1000},
                   "log_tail_lines": {"default": 200, "max": 1000},
                   "log_response_bytes_max": 262144,
                   "diagnostic_events_per_run_max": 20000,
                   "max_parallel_runs": manager.max_parallel,
                   "max_queue": manager.max_queue},
        "resource_sampling": {"interval_seconds": 2,
                              "process_metrics": ["cpu_ticks", "rss_bytes", "read_bytes", "write_bytes"],
                              "gpu_metrics": ["utilization_percent", "memory_used_mib", "memory_total_mib"],
                              "gpu_scope": "whole GPU; not attributable to one run",
                              "process_scope": "runner process descendants; IO counters may be unavailable"},
        "log_sources": {"runner": "API-captured roboicl.run stdout/stderr",
                        "client": "per-run simulator client log",
                        "camera": "optional per-run camera log; 404 when not produced"},
        "runtime_environment": {"asset_storage": {"path": "configured ROBOICL_DATA_ROOT",
                                                    "storage_class": asset_storage_class,
                                                    "filesystem_type": asset_mount_type},
                                "asset_preflight": "complete-tree manifest and SHA-256 summary",
                                "simulator_version": "not dynamically sampled",
                                "upstream_submodule_commits": upstream},
        "diagnostic_success_is_task_success": False,
        "observation_encoding": "raw_rgb by default; explicit session opt-in jpeg via the shared RGB encoder; see policy_transport",
    }


def make_handler(manager: RunManager, keys: tuple[str, ...]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RoboICLControl/1"
        sys_version = ""
        _rate_lock = threading.Lock()
        _run_calls: dict[str, deque[float]] = {}
        _transfer_calls: dict[str, deque[float]] = {}

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            # Do not echo paths, query credentials, authorization, or body data.
            return

        def _send(self, status: int, payload: dict):
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            supplied = header[7:]
            matched = False
            for key in keys:
                matched = hmac.compare_digest(supplied.encode(), key.encode()) or matched
            if matched:
                self.actor_id = hashlib.sha256(supplied.encode()).hexdigest()[:16]
            return matched

        def _run_rate_limited(self) -> bool:
            now = time.monotonic()
            peer = self.client_address[0]
            with self._rate_lock:
                calls = self._run_calls.setdefault(peer, deque())
                while calls and now - calls[0] >= 60:
                    calls.popleft()
                if len(calls) >= manager.create_rate_per_min:
                    return True
                calls.append(now)
                return False

        def _transfer_rate_limited(self) -> bool:
            now = time.monotonic()
            actor = getattr(self, "actor_id", "unknown")
            with self._rate_lock:
                calls = self._transfer_calls.setdefault(actor, deque())
                while calls and now - calls[0] >= 60:
                    calls.popleft()
                if len(calls) >= _TRANSFER_RATE_LIMIT:
                    return True
                calls.append(now)
                return False

        def _path(self):
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                return None
            return parsed.path

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("transfer encoding is not supported")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length < 0 or length > _MAX_BODY:
                raise OverflowError("request body exceeds 16 KiB")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request JSON must be an object")
            return value

        def _error(self, status: int, code: str, message: str):
            self._send(status, {"error": {"code": code, "message": message}})

        def do_GET(self):
            if not self._authorized():
                return self._error(401, "unauthorized", "Valid Bearer access key required")
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.fragment or (query and not re.fullmatch(r"/v1/runs/[0-9a-f]{32}/(diagnostics|logs)", path)):
                return self._error(400, "invalid_request", "Query strings are not accepted")
            if path == "/v1/diagnostics/capabilities":
                return self._send(200, manager.diagnostics_capabilities)
            match = re.fullmatch(r"/v1/runs/([0-9a-f]{32})/diagnostics", path)
            if match:
                try:
                    after_seq_values = query.get("after_seq", ["0"])
                    limit_values = query.get("limit", ["200"])
                    if len(after_seq_values) != 1 or len(limit_values) != 1:
                        raise ValueError
                    after_seq = int(after_seq_values[0])
                    limit = int(limit_values[0])
                    if after_seq < 0 or not 1 <= limit <= 1000:
                        raise ValueError
                except ValueError:
                    return self._error(400, "invalid_query", "after_seq must be >=0 and limit must be 1..1000")
                value = manager.diagnostics(match.group(1), self.actor_id, after_seq, limit)
                if value is None:
                    return self._error(404, "run_not_found", "Run not found")
                return self._send(200, value)
            match = re.fullmatch(r"/v1/runs/([0-9a-f]{32})/logs", path)
            if match:
                try:
                    sources = query.get("source", ["runner"])
                    tails = query.get("tail_lines", ["200"])
                    if len(sources) != 1 or len(tails) != 1:
                        raise ValueError
                    tail_lines = int(tails[0])
                    if not 1 <= tail_lines <= 1000:
                        raise ValueError
                except ValueError:
                    return self._error(400, "invalid_query", "source must be singular and tail_lines must be 1..1000")
                value, error = manager.logs(match.group(1), self.actor_id, sources[0], tail_lines)
                if error == "run_not_found":
                    return self._error(404, "run_not_found", "Run not found")
                if error == "invalid_source":
                    return self._error(400, "invalid_source", "source must be runner, client, or camera")
                if error:
                    return self._error(404, "log_not_found", "Log is not available for this run")
                return self._send(200, value)
            if path == "/v1/health":
                return self._send(200, _health(manager))
            if path == "/v1/transfer-test/info":
                try:
                    metadata = json.loads(manager.transfer_test_metadata.read_text(encoding="utf-8"))
                    if metadata.get("bytes") != manager.transfer_test_file.stat().st_size:
                        raise ValueError("transfer test data size mismatch")
                except (OSError, ValueError, json.JSONDecodeError):
                    return self._error(503, "transfer_test_unavailable", "Transfer test data is not installed")
                return self._send(200, {
                    "format": metadata.get("format"),
                    "copies": metadata.get("copies"),
                    "file_count": metadata.get("file_count"),
                    "bytes": metadata.get("bytes"),
                    "sha256": metadata.get("sha256"),
                    "download_url": "/v1/transfer-test/download",
                    "upload_url": "/v1/transfer-test/upload",
                    "upload_limit_bytes": _MAX_TRANSFER_UPLOAD,
                })
            if path == "/v1/transfer-test/records":
                return self._send(200, {"records": manager.recent_transfer_tests(self.actor_id)})
            match = re.fullmatch(r"/v1/transfer-test/records/([0-9a-f]{32})", path)
            if match:
                value = manager.get_transfer_test(match.group(1), self.actor_id)
                if value is None:
                    return self._error(404, "transfer_test_not_found", "Transfer test record not found")
                return self._send(200, value)
            if path == "/v1/transfer-test/download":
                if self._transfer_rate_limited():
                    return self._error(429, "rate_limited", "Transfer test rate limit exceeded")
                payload = manager.transfer_test_file
                if not payload.is_file():
                    return self._error(503, "transfer_test_unavailable", "Transfer test data is not installed")
                try:
                    metadata = json.loads(manager.transfer_test_metadata.read_text(encoding="utf-8"))
                    expected_bytes = payload.stat().st_size
                    if metadata.get("bytes") != expected_bytes:
                        raise ValueError("transfer test data size mismatch")
                except (OSError, ValueError, json.JSONDecodeError):
                    return self._error(503, "transfer_test_unavailable", "Transfer test data metadata is invalid")
                test_id = uuid4().hex
                self.connection.settimeout(180)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Content-Length", str(expected_bytes))
                self.send_header("Content-Disposition", 'attachment; filename="roboicl-transfer-benchmark.tar"')
                self.send_header("X-Transfer-Test-Id", test_id)
                self.send_header("X-Transfer-Test-Bytes", str(expected_bytes))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                started = time.perf_counter()
                sent = 0
                status = "complete"
                try:
                    with payload.open("rb") as source:
                        while True:
                            chunk = source.read(_TRANSFER_CHUNK_BYTES)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                    self.wfile.flush()
                    if sent != expected_bytes:
                        status = "incomplete"
                except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
                    status = "interrupted"
                    self.close_connection = True
                elapsed = time.perf_counter() - started
                manager.record_transfer_test(
                    test_id=test_id, direction="download", actor_id=self.actor_id,
                    byte_count=sent, elapsed_seconds=elapsed,
                    sha256=metadata.get("sha256"), status=status,
                )
                return
            match = re.fullmatch(r"/v1/runs/([0-9a-f]{32})(/result)?", path)
            if not match:
                return self._error(404, "not_found", "Resource not found")
            run_id, result_suffix = match.groups()
            if result_suffix:
                found = manager.result(run_id, self.actor_id)
                if found is None:
                    return self._error(404, "run_not_found", "Run not found")
                state, result = found
                if result is None:
                    return self._error(409, "result_not_ready", "Run result is not available")
                return self._send(200, {"run_id": run_id, "status": state, "result": result})
            value = manager.get(run_id, self.actor_id)
            if value is None:
                return self._error(404, "run_not_found", "Run not found")
            return self._send(200, value)

        def do_POST(self):
            if not self._authorized():
                return self._error(401, "unauthorized", "Valid Bearer access key required")
            path = self._path()
            if path is None:
                return self._error(400, "invalid_request", "Query strings are not accepted")
            if path == "/v1/transfer-test/upload":
                if self._transfer_rate_limited():
                    return self._error(429, "rate_limited", "Transfer test rate limit exceeded")
                if self.headers.get("Transfer-Encoding"):
                    return self._error(411, "length_required", "Chunked transfer is not supported")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/octet-stream":
                    return self._error(415, "unsupported_media_type", "Content-Type must be application/octet-stream")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._error(400, "invalid_content_length", "Content-Length must be an integer")
                if length <= 0:
                    return self._error(400, "empty_upload", "Upload body must not be empty")
                if length > _MAX_TRANSFER_UPLOAD:
                    return self._error(413, "upload_too_large", "Upload exceeds the 64 MiB limit")
                self.connection.settimeout(180)
                test_id = uuid4().hex
                digest = hashlib.sha256()
                received = 0
                status = "complete"
                started = time.perf_counter()
                try:
                    while received < length:
                        chunk = self.rfile.read(min(_TRANSFER_CHUNK_BYTES, length - received))
                        if not chunk:
                            status = "incomplete"
                            break
                        digest.update(chunk)
                        received += len(chunk)
                except (ConnectionResetError, OSError, socket.timeout):
                    status = "interrupted"
                elapsed = time.perf_counter() - started
                checksum = digest.hexdigest()
                record = manager.record_transfer_test(
                    test_id=test_id, direction="upload", actor_id=self.actor_id,
                    byte_count=received, elapsed_seconds=elapsed,
                    sha256=checksum if received else None, status=status,
                )
                if status != "complete":
                    return self._send(400, {"error": {"code": "incomplete_upload",
                                                       "message": "Upload ended before Content-Length bytes arrived"},
                                        "transfer_test": record})
                expected_checksum = None
                try:
                    expected_checksum = json.loads(
                        manager.transfer_test_metadata.read_text(encoding="utf-8")
                    ).get("sha256")
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                record["matches_download_payload"] = (
                    checksum == expected_checksum if expected_checksum else None
                )
                return self._send(201, record)
            if path in {"/v1/policy-sessions", "/v1/runs"}:
                if self._run_rate_limited():
                    encoded = json.dumps({"error": {"code": "rate_limited", "message": "Run creation limit exceeded"}}, separators=(",", ":")).encode()
                    self.send_response(429)
                    self.send_header("Retry-After", "60")
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                try:
                    content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json":
                        return self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                    body = self._body()
                    if path == "/v1/policy-sessions":
                        value = manager.create_session(
                            body, self.actor_id,
                            self.headers.get("X-Forwarded-Host") or self.headers.get("Host"),
                        )
                    else:
                        value = manager.create(body, self.actor_id)
                except OverflowError as exc:
                    if str(exc) == "queue_full":
                        return self._error(429, "queue_full", "Run queue is full")
                    return self._error(413, "request_too_large", "Request body exceeds limit")
                except RuntimeError as exc:
                    if str(exc) == "policy_unreachable":
                        return self._error(503, "policy_unreachable", "Remote policy gateway is not configured")
                    raise
                except FileNotFoundError:
                    return self._error(503, "assets_not_ready", "Required simulator assets are not ready")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._error(400, "invalid_json", "Request body must be valid UTF-8 JSON")
                except ValueError as exc:
                    message = str(exc)
                    if message == "policy_session_reused":
                        return self._error(409, "policy_session_reused", "Policy session is already bound to a run")
                    if message == "policy_session_expired":
                        return self._error(410, "policy_session_expired", "Policy session expired; create a new session")
                    if message == "policy_session_scope_mismatch":
                        return self._error(422, "invalid_request", "Run settings do not match the policy session")
                    return self._error(422, "invalid_request", message)
                return self._send(201 if path == "/v1/policy-sessions" else 202, value)
            if path == "/v1/diagnostic-runs":
                if self._run_rate_limited():
                    return self._error(429, "rate_limited", "Run creation limit exceeded")
                try:
                    content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json":
                        return self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                    body = self._body()
                    value = manager.create_diagnostic(body, self.actor_id)
                except OverflowError as exc:
                    return self._error(429 if str(exc) == "queue_full" else 413,
                                       "queue_full" if str(exc) == "queue_full" else "request_too_large",
                                       "Run queue is full" if str(exc) == "queue_full" else "Request body exceeds limit")
                except FileNotFoundError:
                    return self._error(503, "assets_not_ready", "Required simulator assets are not ready")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._error(400, "invalid_json", "Request body must be valid UTF-8 JSON")
                except ValueError as exc:
                    return self._error(422, "invalid_request", str(exc))
                value.update({"diagnostics_url": f"/v1/runs/{value['run_id']}/diagnostics",
                              "logs_url": f"/v1/runs/{value['run_id']}/logs"})
                return self._send(202, value)
            match = re.fullmatch(r"/v1/runs/([0-9a-f]{32})/cancel", path)
            if match:
                value = manager.cancel(match.group(1), self.actor_id)
                if value is None:
                    return self._error(404, "run_not_found", "Run not found")
                return self._send(202, {"run_id": match.group(1), "status": value["status"],
                                        "cancel_requested": value["status"] in {"cancelling", "cancelled"}})
            return self._error(404, "not_found", "Resource not found")

        def do_PUT(self):
            self._error(405, "method_not_allowed", "Method not allowed")

        def do_DELETE(self):
            self._error(405, "method_not_allowed", "Method not allowed")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ROBOICL_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ROBOICL_API_PORT", "8080")))
    parser.add_argument("--state-dir", default=os.environ.get("ROBOICL_API_STATE_DIR", str(CODE / ".roboicl-api")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    keys = _load_access_keys()
    manager = RunManager(Path(args.state_dir).expanduser().resolve())
    server = BoundedHTTPServer((args.host, args.port), make_handler(manager, keys))
    server.timeout = 1
    manager.start()
    previous_sigterm = signal.signal(signal.SIGTERM, lambda _signum, _frame: manager._stopping.set())
    try:
        while not manager._stopping.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.server_close()
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
