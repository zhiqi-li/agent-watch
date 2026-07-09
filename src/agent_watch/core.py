#!/usr/bin/env python3
"""Watch local Codex and Claude Code sessions and notify when they need attention.

The monitor deliberately uses explicit local lifecycle state whenever possible:

* Claude Code: ~/.claude/sessions/*.json
* Codex: the main rollout JSONL opened by each Codex process
* Native hooks: permission/input/completion events for newly started sessions
* tmux capture: a narrow fallback for interactive prompts not covered above
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import glob
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable, Mapping, Sequence

from . import __version__
from .processes import (
    ProcessInfo,
    exact_agent_processes,
    open_process_files,
    process_details,
)

APP_NAME = "agent-watch"
VERSION = __version__
HOME = pathlib.Path.home()
DEFAULT_CONFIG_PATH = pathlib.Path(
    os.environ.get("AGENT_WATCH_CONFIG", HOME / ".config/agent-watch/config.toml")
)
DEFAULT_STATE_DIR = pathlib.Path(
    os.environ.get("AGENT_WATCH_STATE_DIR", HOME / ".local/state/agent-watch")
)

DEFAULT_CONFIG: dict[str, Any] = {
    "monitor": {
        "interval_seconds": 5.0,
        "ready_delay_seconds": 12.0,
        "missing_grace_seconds": 10.0,
        "notify_existing": True,
        "needs_input_repeat_seconds": 1800.0,
        "capture_lines": 80,
        "tmux_fallback_interval_seconds": 6.0,
        "tmux_fallback": True,
        "activity_stale_seconds": 600.0,
        "retention_days": 30.0,
        "process_exit_notifications": True,
        "ignore_tmux_sessions": ["agent-watch"],
    },
    "ui": {
        # Conversation text can be sensitive on shared terminals, so public
        # installations opt in explicitly. Pressing "p" toggles it at runtime.
        "conversation_preview": False,
    },
    "notifications": {
        "console": True,
        "tmux": True,
        "desktop": False,
        "timeout_seconds": 6.0,
        "include_cwd": False,
        "include_message_preview": False,
        "include_tmux_socket": False,
        "allow_insecure_http": False,
        "cursor": {"enabled": False, "socket": "", "include_prompt": False},
        "command": {"argv": []},
        "webhook": {"url": "", "bearer_token": ""},
        "ntfy": {"url": "", "token": "", "priority": "high"},
        "telegram": {"bot_token": "", "chat_id": ""},
        "bark": {"url": ""},
    },
}

BUSY_STATES = {"running", "auto_wait"}
ATTENTION_STATES = {"ready", "needs_input", "error", "exited"}
CLAUDE_BUSY = {"busy", "shell", "thinking", "working", "tool", "compacting"}
CLAUDE_INPUT = {"permission", "prompt", "input", "waiting_for_input", "needs_input"}

ANSI_RE = re.compile(
    r"(?:\x1B\][^\x07]*(?:\x07|\x1B\\)|\x1B\[[0-?]*[ -/]*[@-~]|\x1B[@-_])"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TMUX_DYNAMIC_RE = re.compile(
    r"(?:\d+\s*[dhms]|\d{1,2}:\d{2}(?::\d{2})?|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✳✶✻✽●•])"
)
INPUT_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"would you like to (?:run|proceed|allow|approve)",
        r"do you want to proceed",
        r"approve (?:this|the) (?:command|action|plan)",
        r"yes, (?:allow|and don't ask)",
        r"press enter to (?:confirm|select|submit)",
        r"enter to (?:confirm|select|submit)",
        r"select (?:an|one) option",
        r"choose (?:an|one) option",
        r"waiting for (?:your )?(?:approval|input|response)",
        r"需要(?:你的)?(?:批准|审批|授权|回复|输入|选择)",
        r"请选择.*(?:选项|一项)",
    )
]

# These caches live only inside the long-running daemon. Hooks are short-lived and
# do not rely on them. They keep the steady-state poll cheap even with large rollouts.
CODEX_ROLLOUT_CACHE: dict[tuple[int, str], tuple[pathlib.Path, str]] = {}
CODEX_LIFECYCLE_CACHE: dict[str, tuple[int, int, tuple[str, str, str]]] = {}
CLAUDE_TRANSCRIPT_CACHE: dict[str, pathlib.Path] = {}
PANE_CAPTURE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
CURSOR_SOCKET_GENERATIONS: dict[str, tuple[int, int]] = {}
CURSOR_PROMPT_LOADER: Any | None = None

CURSOR_NOTIFY_SOCKET_NAME = "cursor-notify.sock"
CURSOR_NOTIFY_MAX_PAYLOAD_BYTES = 256 * 1024
CURSOR_NOTIFY_MAX_ACK_BYTES = 4 * 1024
CURSOR_NOTIFY_DEFAULT_TIMEOUT = 2.0
CURSOR_NOTIFY_MAX_TIMEOUT = 30.0


class CursorNotifyInputError(ValueError):
    """The Cursor notification request or local endpoint is unsafe/invalid."""


class CursorNotifyDeliveryError(RuntimeError):
    """The Cursor extension could not accept a valid notification request."""


def utc_now() -> float:
    return time.time()


def iso_time(value: float | None = None) -> str:
    value = utc_now() if value is None else value
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in base.items():
        result[key] = deep_merge(value, {}) if isinstance(value, dict) else value
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: pathlib.Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = deep_merge(DEFAULT_CONFIG, {})
    if path.is_symlink():
        raise ValueError(f"refusing symlinked config: {path}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return config
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"config is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise ValueError(f"config must be owned by uid {os.getuid()}: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(f"config may contain secrets and must use mode 0600: {path}")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            loaded = tomllib.load(fh)
    finally:
        os.close(fd)
    if not isinstance(loaded, Mapping):
        raise ValueError(f"config must contain a TOML table: {path}")
    config = deep_merge(config, loaded)
    validate_config(config)
    return config


def validate_config(config: Mapping[str, Any]) -> None:
    monitor = config.get("monitor", {})
    if not isinstance(monitor, Mapping):
        raise ValueError("monitor must be a TOML table")
    for key in (
        "notify_existing",
        "tmux_fallback",
        "process_exit_notifications",
    ):
        if not isinstance(monitor.get(key), bool):
            raise ValueError(f"monitor.{key} must be true or false (without quotes)")
    for key in (
        "interval_seconds",
        "ready_delay_seconds",
        "missing_grace_seconds",
        "needs_input_repeat_seconds",
        "tmux_fallback_interval_seconds",
        "activity_stale_seconds",
        "retention_days",
    ):
        value = monitor.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"monitor.{key} must be a non-negative number")
    capture_lines = monitor.get("capture_lines")
    if isinstance(capture_lines, bool) or not isinstance(capture_lines, int) or capture_lines < 1:
        raise ValueError("monitor.capture_lines must be a positive integer")
    ignored = monitor.get("ignore_tmux_sessions")
    if not isinstance(ignored, list) or not all(isinstance(item, str) for item in ignored):
        raise ValueError("monitor.ignore_tmux_sessions must be an array of strings")

    ui = config.get("ui", {})
    if not isinstance(ui, Mapping):
        raise ValueError("ui must be a TOML table")
    if not isinstance(ui.get("conversation_preview"), bool):
        raise ValueError("ui.conversation_preview must be true or false (without quotes)")

    notifications = config.get("notifications", {})
    if not isinstance(notifications, Mapping):
        raise ValueError("notifications must be a TOML table")
    for key in (
        "console",
        "tmux",
        "desktop",
        "include_cwd",
        "include_message_preview",
        "include_tmux_socket",
        "allow_insecure_http",
    ):
        if not isinstance(notifications.get(key), bool):
            raise ValueError(
                f"notifications.{key} must be true or false (without quotes)"
            )
    timeout = notifications.get("timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("notifications.timeout_seconds must be a positive number")
    command = notifications.get("command", {})
    if not isinstance(command, Mapping):
        raise ValueError("notifications.command must be a TOML table")
    argv = command.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("notifications.command.argv must be an array of strings")

    cursor = notifications.get("cursor", {})
    if not isinstance(cursor, Mapping):
        raise ValueError("notifications.cursor must be a TOML table")
    if not isinstance(cursor.get("enabled"), bool):
        raise ValueError("notifications.cursor.enabled must be true or false")
    if not isinstance(cursor.get("socket"), str):
        raise ValueError("notifications.cursor.socket must be a string")
    if not isinstance(cursor.get("include_prompt"), bool):
        raise ValueError("notifications.cursor.include_prompt must be true or false")

    allow_http = notifications["allow_insecure_http"]
    for channel in ("webhook", "ntfy", "bark"):
        settings = notifications.get(channel, {})
        if not isinstance(settings, Mapping):
            raise ValueError(f"notifications.{channel} must be a TOML table")
        if settings.get("url"):
            validate_notification_url(str(settings["url"]), allow_http)
    telegram = notifications.get("telegram", {})
    if not isinstance(telegram, Mapping):
        raise ValueError("notifications.telegram must be a TOML table")


def ensure_private_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def clean_text(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    text = ANSI_RE.sub("", text).replace("\u00a0", " ")
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def project_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    path = pathlib.PurePath(cwd)
    name = path.name or str(path)
    if name in {".worktree", "worktree"} and len(path.parts) > 1:
        return clean_text(path.parts[-2], 160)
    return clean_text(name, 160)


def stable_hash(*parts: Any, length: int = 20) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:length]


def canonical_hash(value: Any, length: int = 24) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:length]


@dataclasses.dataclass(slots=True)
class Pane:
    pane_id: str
    session: str
    window: str
    index: str
    pid: int
    tty: str
    command: str
    cwd: str
    title: str
    dead: bool
    socket_path: str = ""

    @property
    def target(self) -> str:
        return f"{self.session}:{self.window}.{self.index}"


@dataclasses.dataclass(slots=True)
class Observation:
    key: str
    provider: str
    session_id: str
    pid: int | None
    proc_start: str
    pane_id: str
    tmux_target: str
    cwd: str
    name: str
    state: str
    event_id: str
    source: str
    raw_status: str = ""
    message: str = ""
    tmux_socket: str = ""
    pane_activity_id: str = ""
    artifact_activity_id: str = ""
    observed_at: float = dataclasses.field(default_factory=utc_now)


class StateDB:
    def __init__(self, state_dir: pathlib.Path = DEFAULT_STATE_DIR, timeout: float = 10.0):
        self.state_dir = state_dir
        ensure_private_dir(state_dir)
        self.path = state_dir / "state.sqlite3"
        # Pre-create with a private mode. SQLite derives WAL/SHM permissions
        # from the main database on supported Unix platforms.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.conn = sqlite3.connect(self.path, timeout=timeout)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
        self._init_schema()
        self._secure_files()

    def _secure_files(self) -> None:
        for path in (self.path, pathlib.Path(f"{self.path}-wal"), pathlib.Path(f"{self.path}-shm")):
            with contextlib.suppress(FileNotFoundError, PermissionError):
                path.chmod(0o600)

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                proc_start TEXT NOT NULL DEFAULT '',
                pane_id TEXT NOT NULL DEFAULT '',
                tmux_target TEXT NOT NULL DEFAULT '',
                tmux_socket TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                state_since REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_activity_at REAL NOT NULL DEFAULT 0,
                pane_activity_id TEXT NOT NULL DEFAULT '',
                artifact_activity_id TEXT NOT NULL DEFAULT '',
                event_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                raw_status TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                notified_event_id TEXT NOT NULL DEFAULT '',
                notified_at REAL,
                first_seen REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_last_seen_idx ON sessions(last_seen);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                session_key TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                delivered_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                available_at REAL NOT NULL,
                session_key TEXT NOT NULL,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_until REAL,
                sent_at REAL,
                delivered_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS outbox_due_idx
                ON outbox(sent_at, available_at, claimed_until);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        # Existing installations are migrated in place. SQLite only supports
        # adding one column at a time, and these defaults keep old readers safe.
        columns = {
            str(row[1]) for row in self.conn.execute("PRAGMA table_info(sessions)")
        }
        migrations = {
            "tmux_socket": "TEXT NOT NULL DEFAULT ''",
            "last_activity_at": "REAL NOT NULL DEFAULT 0",
            "pane_activity_id": "TEXT NOT NULL DEFAULT ''",
            "artifact_activity_id": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {name} {declaration}"
                )
        self.conn.commit()

    def close(self) -> None:
        self._secure_files()
        self.conn.close()

    def prune(self, retention_days: float, now: float | None = None) -> dict[str, int]:
        """Remove delivered history and stale sessions older than the retention window."""
        if retention_days <= 0:
            return {"notifications": 0, "outbox": 0, "sessions": 0}
        cutoff = (utc_now() if now is None else now) - retention_days * 86400
        counts: dict[str, int] = {}
        cursor = self.conn.execute(
            "DELETE FROM notifications WHERE created_at < ?", (cutoff,)
        )
        counts["notifications"] = cursor.rowcount
        cursor = self.conn.execute(
            "DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (cutoff,)
        )
        counts["outbox"] = cursor.rowcount
        cursor = self.conn.execute(
            """DELETE FROM sessions
               WHERE last_seen < ? AND NOT EXISTS (
                 SELECT 1 FROM outbox
                 WHERE outbox.session_key=sessions.session_key
                   AND outbox.sent_at IS NULL
               )""",
            (cutoff,),
        )
        counts["sessions"] = cursor.rowcount
        self.conn.commit()
        self._secure_files()
        return counts

    def clear_history(self) -> dict[str, int]:
        """Delete session and notification history while preserving daemon metadata."""
        counts: dict[str, int] = {}
        for table in ("notifications", "outbox", "sessions"):
            cursor = self.conn.execute(f"DELETE FROM {table}")
            counts[table] = cursor.rowcount
        self.conn.commit()
        self._secure_files()
        return counts

    def set_meta(self, key: str, value: str) -> None:
        now = utc_now()
        self.conn.execute(
            """INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> tuple[str, float] | None:
        row = self.conn.execute("SELECT value, updated_at FROM meta WHERE key=?", (key,)).fetchone()
        return (row["value"], row["updated_at"]) if row else None

    def daemon_alive(self, max_age: float = 15.0) -> bool:
        value = self.get_meta("heartbeat")
        return bool(value and value[0] != "stopped" and utc_now() - value[1] <= max_age)

    def upsert(self, obs: Observation, notify_existing: bool = True) -> tuple[sqlite3.Row | None, sqlite3.Row]:
        # A process can be discovered before its session metadata file/rollout is
        # ready. Once the canonical session id appears, discard the temporary PID
        # alias instead of later reporting a fake process exit.
        if obs.pid is not None and not obs.session_id.startswith("pid-"):
            aliases = self.conn.execute(
                """SELECT session_key FROM sessions
                   WHERE provider=? AND pid=? AND proc_start=? AND session_key!=?
                     AND session_id LIKE 'pid-%'""",
                (obs.provider, obs.pid, obs.proc_start, obs.key),
            ).fetchall()
            for alias in aliases:
                self.conn.execute("DELETE FROM outbox WHERE session_key=?", (alias["session_key"],))
                self.conn.execute("DELETE FROM sessions WHERE session_key=?", (alias["session_key"],))
        previous = self.conn.execute(
            "SELECT * FROM sessions WHERE session_key=?", (obs.key,)
        ).fetchone()
        # Claude Stop can explicitly say background tasks or session crons are
        # still active while its coarse session file already says "idle". Keep
        # the richer hook state until real activity or a later hook changes it.
        if (
            previous is not None
            and previous["state"] == "auto_wait"
            and str(previous["source"]).endswith("-hook")
            and obs.source in {"claude-session", "process"}
            and obs.state in {"ready", "unknown"}
        ):
            obs = dataclasses.replace(
                obs,
                state="auto_wait",
                event_id=previous["event_id"],
                source=previous["source"],
                raw_status=previous["raw_status"],
                message=previous["message"],
            )
        now = obs.observed_at
        state_changed = previous is None or previous["state"] != obs.state
        event_changed = previous is None or previous["event_id"] != obs.event_id
        state_since = now if state_changed or event_changed else previous["state_since"]
        previous_pane_activity = previous["pane_activity_id"] if previous else ""
        previous_artifact_activity = previous["artifact_activity_id"] if previous else ""
        pane_activity_id = obs.pane_activity_id or previous_pane_activity
        artifact_activity_id = obs.artifact_activity_id or previous_artifact_activity
        activity_changed = previous is None or bool(
            (obs.pane_activity_id and obs.pane_activity_id != previous_pane_activity)
            or (
                obs.artifact_activity_id
                and obs.artifact_activity_id != previous_artifact_activity
            )
        )
        previous_activity_at = float(previous["last_activity_at"] or 0) if previous else 0.0
        last_activity_at = (
            now
            if state_changed or event_changed or activity_changed
            else previous_activity_at
        )

        notified_event_id = previous["notified_event_id"] if previous else ""
        notified_at = previous["notified_at"] if previous else None
        if previous is None and not notify_existing:
            notified_event_id = obs.event_id
            notified_at = now
        elif event_changed or state_changed:
            notified_event_id = ""
            notified_at = None

        self.conn.execute(
            """
            INSERT INTO sessions(
                session_key, provider, session_id, pid, proc_start, pane_id, tmux_target,
                tmux_socket, cwd, name, state, state_since, last_seen, last_activity_at,
                pane_activity_id, artifact_activity_id, event_id, source, raw_status,
                message, notified_event_id, notified_at, first_seen
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                provider=excluded.provider,
                session_id=CASE WHEN excluded.session_id != '' THEN excluded.session_id ELSE sessions.session_id END,
                pid=COALESCE(excluded.pid, sessions.pid),
                proc_start=CASE WHEN excluded.proc_start != '' THEN excluded.proc_start ELSE sessions.proc_start END,
                pane_id=CASE WHEN excluded.pane_id != '' AND excluded.tmux_target != '' AND excluded.tmux_socket != '' THEN excluded.pane_id ELSE sessions.pane_id END,
                tmux_target=CASE WHEN excluded.pane_id != '' AND excluded.tmux_target != '' AND excluded.tmux_socket != '' THEN excluded.tmux_target ELSE sessions.tmux_target END,
                tmux_socket=CASE WHEN excluded.pane_id != '' AND excluded.tmux_target != '' AND excluded.tmux_socket != '' THEN excluded.tmux_socket ELSE sessions.tmux_socket END,
                cwd=CASE WHEN excluded.cwd != '' THEN excluded.cwd ELSE sessions.cwd END,
                name=CASE WHEN excluded.name != '' THEN excluded.name ELSE sessions.name END,
                state=excluded.state,
                state_since=excluded.state_since,
                last_seen=excluded.last_seen,
                last_activity_at=excluded.last_activity_at,
                pane_activity_id=excluded.pane_activity_id,
                artifact_activity_id=excluded.artifact_activity_id,
                event_id=excluded.event_id,
                source=excluded.source,
                raw_status=excluded.raw_status,
                message=excluded.message,
                notified_event_id=excluded.notified_event_id,
                notified_at=excluded.notified_at
            """,
            (
                obs.key,
                obs.provider,
                obs.session_id,
                obs.pid,
                obs.proc_start,
                obs.pane_id,
                obs.tmux_target,
                obs.tmux_socket,
                obs.cwd,
                obs.name,
                obs.state,
                state_since,
                now,
                last_activity_at,
                pane_activity_id,
                artifact_activity_id,
                obs.event_id,
                obs.source,
                obs.raw_status,
                clean_text(obs.message, 1000),
                notified_event_id,
                notified_at,
                previous["first_seen"] if previous else now,
            ),
        )
        if previous is not None and (state_changed or event_changed):
            self.conn.execute(
                """UPDATE outbox SET sent_at=?, claimed_by=NULL, claimed_until=NULL,
                   delivered_json=?
                   WHERE session_key=? AND sent_at IS NULL
                     AND (event_id!=? OR kind!=?)""",
                (
                    now,
                    json.dumps({"cancelled": "superseded"}),
                    obs.key,
                    obs.event_id,
                    obs.state,
                ),
            )
        self.conn.commit()
        current = self.conn.execute(
            "SELECT * FROM sessions WHERE session_key=?", (obs.key,)
        ).fetchone()
        assert current is not None
        return previous, current

    def mark_missing_exited(self, seen_keys: set[str], grace: float, notify: bool = True) -> None:
        cutoff = utc_now() - grace
        rows = self.conn.execute(
            """SELECT * FROM sessions
               WHERE pid IS NOT NULL AND state NOT IN ('exited','resolved','superseded')
                 AND last_seen < ?""",
            (cutoff,),
        ).fetchall()
        now = utc_now()
        for row in rows:
            if row["session_key"] in seen_keys:
                continue
            # If READY was already reported, a later normal process exit is just cleanup.
            already_reported_ready = (
                row["state"] == "ready"
                and row["notified_event_id"] == row["event_id"]
            )
            event_id = f"exit:{row['pid']}:{row['proc_start']}"
            self.conn.execute(
                """UPDATE sessions SET state='exited', state_since=?, event_id=?,
                   message='process disappeared', notified_event_id=?, notified_at=?, last_seen=?
                   WHERE session_key=?""",
                (
                    now,
                    event_id,
                    event_id if (already_reported_ready or not notify) else "",
                    now if (already_reported_ready or not notify) else None,
                    now,
                    row["session_key"],
                ),
            )
        self.conn.commit()

    def due(self, config: Mapping[str, Any]) -> list[sqlite3.Row]:
        now = utc_now()
        ready_delay = float(config["monitor"]["ready_delay_seconds"])
        repeat = float(config["monitor"]["needs_input_repeat_seconds"])
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE state IN ('ready','needs_input','error','exited')"
        ).fetchall()
        due: list[sqlite3.Row] = []
        for row in rows:
            if row["state"] == "ready" and now - row["state_since"] < ready_delay:
                continue
            same_event = row["notified_event_id"] == row["event_id"]
            if not same_event:
                # A prior delivery attempt may have failed. Retry, but do not
                # hammer a broken webhook every poll cycle.
                if (
                    row["notified_event_id"] == ""
                    and row["notified_at"] is not None
                    and now - row["notified_at"] < 60
                ):
                    continue
                due.append(row)
                continue
            if (
                row["state"] == "needs_input"
                and repeat > 0
                and not str(row["source"]).endswith("-hook")
                and row["notified_at"] is not None
                and now - row["notified_at"] >= repeat
            ):
                due.append(row)
        return due

    def mark_notified(self, rows: Sequence[sqlite3.Row]) -> None:
        now = utc_now()
        self.conn.executemany(
            """UPDATE sessions SET notified_event_id=?, notified_at=?
               WHERE session_key=? AND event_id=?""",
            [(row["event_id"], now, row["session_key"], row["event_id"]) for row in rows],
        )
        self.conn.commit()

    def mark_delivery_attempt(self, rows: Sequence[sqlite3.Row]) -> None:
        now = utc_now()
        self.conn.executemany(
            "UPDATE sessions SET notified_at=? WHERE session_key=?",
            [(now, row["session_key"]) for row in rows],
        )
        self.conn.commit()

    @staticmethod
    def snapshot(
        row: Mapping[str, Any], config: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        fields = (
            "session_key",
            "provider",
            "session_id",
            "pid",
            "proc_start",
            "pane_id",
            "tmux_target",
            "tmux_socket",
            "cwd",
            "name",
            "state",
            "state_since",
            "event_id",
            "source",
            "raw_status",
            "message",
        )
        result = {field: row[field] for field in fields}
        settings = (config or DEFAULT_CONFIG)["notifications"]
        if not bool(settings.get("include_cwd", False)):
            result["cwd"] = project_name(str(result.get("cwd") or ""))
        if not bool(settings.get("include_message_preview", False)):
            result["message"] = ""
        return result

    def enqueue_due(self, config: Mapping[str, Any]) -> int:
        rows = self.due(config)
        now = utc_now()
        repeat = float(config["monitor"]["needs_input_repeat_seconds"])
        inserted = 0
        for row in rows:
            repeat_suffix = ""
            if row["notified_event_id"] == row["event_id"] and repeat > 0:
                repeat_suffix = f":repeat:{int(now // repeat)}"
            event_key = stable_hash(
                row["session_key"], row["event_id"], row["state"], repeat_suffix, length=40
            )
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO outbox(
                       event_key, created_at, available_at, session_key, event_id,
                       kind, snapshot_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event_key,
                    now,
                    now,
                    row["session_key"],
                    row["event_id"],
                    row["state"],
                    json.dumps(self.snapshot(row, config), ensure_ascii=False),
                ),
            )
            inserted += cursor.rowcount
        self.conn.commit()
        return inserted

    def enqueue_session_now(
        self, session_key: str, config: Mapping[str, Any] | None = None
    ) -> bool:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_key=?", (session_key,)
        ).fetchone()
        if row is None:
            return False
        now = utc_now()
        event_key = stable_hash(
            row["session_key"], row["event_id"], row["state"], "", length=40
        )
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO outbox(
                   event_key, created_at, available_at, session_key, event_id,
                   kind, snapshot_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                event_key,
                now,
                now,
                row["session_key"],
                row["event_id"],
                row["state"],
                json.dumps(self.snapshot(row, config), ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def claim_outbox(self, limit: int = 50, lease_seconds: float = 60.0) -> list[sqlite3.Row]:
        now = utc_now()
        token = f"{os.getpid()}:{uuid.uuid4()}"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """UPDATE outbox SET sent_at=?, claimed_by=NULL, claimed_until=NULL,
                   delivered_json=?
                   WHERE sent_at IS NULL AND NOT EXISTS (
                     SELECT 1 FROM sessions
                     WHERE sessions.session_key=outbox.session_key
                       AND sessions.event_id=outbox.event_id
                       AND sessions.state=outbox.kind
                   )""",
                (now, json.dumps({"cancelled": "stale"})),
            )
            rows = self.conn.execute(
                """SELECT outbox.event_key FROM outbox
                   JOIN sessions ON sessions.session_key=outbox.session_key
                   WHERE outbox.sent_at IS NULL AND outbox.available_at<=?
                     AND (outbox.claimed_until IS NULL OR outbox.claimed_until<?)
                     AND sessions.event_id=outbox.event_id
                     AND sessions.state=outbox.kind
                   ORDER BY outbox.created_at LIMIT ?""",
                (now, now, limit),
            ).fetchall()
            keys = [row["event_key"] for row in rows]
            if keys:
                placeholders = ",".join("?" for _ in keys)
                self.conn.execute(
                    f"""UPDATE outbox SET claimed_by=?, claimed_until=?
                        WHERE event_key IN ({placeholders}) AND sent_at IS NULL""",
                    (token, now + lease_seconds, *keys),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        return self.conn.execute(
            f"SELECT * FROM outbox WHERE claimed_by=? AND event_key IN ({placeholders})",
            (token, *keys),
        ).fetchall()

    def finish_outbox(
        self,
        claimed: Sequence[sqlite3.Row],
        delivered: Mapping[str, Any],
        success: bool,
    ) -> None:
        now = utc_now()
        delivered_json = json.dumps(delivered, ensure_ascii=False)
        if success:
            for item in claimed:
                self.conn.execute(
                    """UPDATE outbox SET sent_at=?, claimed_by=NULL, claimed_until=NULL,
                       attempts=attempts+1, delivered_json=? WHERE event_key=?""",
                    (now, delivered_json, item["event_key"]),
                )
                snapshot = json.loads(item["snapshot_json"])
                self.conn.execute(
                    """UPDATE sessions SET notified_event_id=?, notified_at=?
                       WHERE session_key=? AND event_id=?""",
                    (
                        snapshot["event_id"],
                        now,
                        snapshot["session_key"],
                        snapshot["event_id"],
                    ),
                )
        else:
            for item in claimed:
                attempts = int(item["attempts"]) + 1
                delay = min(1800.0, 60.0 * (2 ** min(attempts - 1, 5)))
                self.conn.execute(
                    """UPDATE outbox SET available_at=?, claimed_by=NULL, claimed_until=NULL,
                       attempts=?, delivered_json=? WHERE event_key=?""",
                    (now + delay, attempts, delivered_json, item["event_key"]),
                )
        self.conn.commit()

    def wake_failed_channel(self, channel: str, now: float | None = None) -> int:
        """Make delayed failures due when a local channel gets a new listener."""
        now = utc_now() if now is None else now
        rows = self.conn.execute(
            """SELECT event_key, delivered_json FROM outbox
               WHERE sent_at IS NULL AND available_at>?""",
            (now,),
        ).fetchall()
        keys: list[str] = []
        for row in rows:
            try:
                delivered = json.loads(row["delivered_json"])
            except (TypeError, ValueError):
                continue
            if (
                isinstance(delivered, Mapping)
                and channel in delivered
                and not channel_succeeded(delivered[channel])
            ):
                keys.append(str(row["event_key"]))
        if keys:
            self.conn.executemany(
                "UPDATE outbox SET available_at=? WHERE event_key=?",
                ((now, key) for key in keys),
            )
            self.conn.commit()
        return len(keys)

    def resolve_attention(
        self,
        provider: str,
        session_id: str,
        include_errors: bool = False,
        include_background: bool = False,
        older_than_seconds: float = 0.0,
    ) -> None:
        now = utc_now()
        attention_clause = "state='needs_input'"
        if include_errors:
            attention_clause = f"({attention_clause} OR state='error')"
        if include_background:
            attention_clause = (
                f"({attention_clause} OR (state='ready' AND raw_status='agent_completed'))"
            )
        rows = self.conn.execute(
            f"""SELECT session_key FROM sessions
               WHERE provider=? AND session_id=?
                 AND state_since<=?
                 AND {attention_clause}""",
            (provider, session_id, now - older_than_seconds),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                """UPDATE sessions SET state='resolved', state_since=?, last_seen=?,
                   notified_event_id=event_id, notified_at=? WHERE session_key=?""",
                (now, now, now, row["session_key"]),
            )
            self.conn.execute(
                """UPDATE outbox SET sent_at=?, claimed_by=NULL, claimed_until=NULL
                   WHERE session_key=? AND sent_at IS NULL""",
                (now, row["session_key"]),
            )
        self.conn.commit()

    def record_notification(
        self,
        kind: str,
        rows: Sequence[sqlite3.Row],
        payload: Mapping[str, Any],
        delivered: Mapping[str, Any],
    ) -> None:
        now = utc_now()
        session_key = rows[0]["session_key"] if len(rows) == 1 else ""
        provider = rows[0]["provider"] if len(rows) == 1 else "mixed"
        self.conn.execute(
            """INSERT INTO notifications(created_at, kind, session_key, provider, payload_json, delivered_json)
               VALUES(?,?,?,?,?,?)""",
            (
                now,
                kind,
                session_key,
                provider,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(delivered, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def sessions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions ORDER BY provider, name, session_key"
        ).fetchall()

    def recent_notifications(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def tmux_socket_paths(extra: Iterable[str] = ()) -> list[str]:
    base = pathlib.Path(f"/tmp/tmux-{os.getuid()}")
    sockets: set[str] = set()
    if base.is_dir():
        for child in base.iterdir():
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(child.stat().st_mode):
                    sockets.add(str(child))
    for value in extra:
        if not value:
            continue
        with contextlib.suppress(OSError):
            if stat.S_ISSOCK(pathlib.Path(value).stat().st_mode):
                sockets.add(value)
    return sorted(sockets)


def tmux_environment_location() -> tuple[str, str]:
    value = os.environ.get("TMUX", "")
    socket_path = value.rsplit(",", 2)[0] if value.count(",") >= 2 else ""
    if any(not character.isprintable() for character in socket_path):
        socket_path = ""
    return socket_path, clean_text(os.environ.get("TMUX_PANE", ""), 40)


def tmux_target_for_pane(socket_path: str, pane_id: str) -> str:
    if not pane_id:
        return ""
    command = ["tmux"]
    if socket_path:
        command += ["-S", socket_path]
    command += [
        "display-message",
        "-p",
        "-t",
        pane_id,
        "#{session_name}:#{window_index}.#{pane_index}",
    ]
    try:
        run = subprocess.run(
            command, capture_output=True, text=True, timeout=1, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return clean_text(run.stdout, 100) if run.returncode == 0 else ""


def list_tmux_panes(extra_sockets: Iterable[str] = ()) -> list[Pane]:
    # tmux renders control characters in format output as literal octal escapes,
    # so use a printable delimiter that cannot occur in normal tmux metadata.
    sep = "|#AGENT_WATCH#|"
    fmt = sep.join(
        (
            "#{pane_id}",
            "#{session_name}",
            "#{window_index}",
            "#{pane_index}",
            "#{pane_pid}",
            "#{pane_tty}",
            "#{pane_current_command}",
            "#{pane_current_path}",
            "#{pane_title}",
            "#{pane_dead}",
        )
    )
    sockets = tmux_socket_paths(extra_sockets) or [""]
    panes: list[Pane] = []
    seen: set[tuple[str, str]] = set()
    for socket_path in sockets:
        cmd = ["tmux"]
        if socket_path:
            cmd += ["-S", socket_path]
        cmd += ["list-panes", "-a", "-F", fmt]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if run.returncode != 0:
            continue
        for line in run.stdout.splitlines():
            fields = line.split(sep)
            if len(fields) != 10:
                continue
            identity = (socket_path, fields[0])
            if identity in seen:
                continue
            seen.add(identity)
            with contextlib.suppress(ValueError):
                panes.append(
                    Pane(
                        pane_id=fields[0],
                        session=fields[1],
                        window=fields[2],
                        index=fields[3],
                        pid=int(fields[4]),
                        tty=fields[5],
                        command=fields[6],
                        cwd=fields[7],
                        title=clean_text(fields[8], 160),
                        dead=fields[9] == "1",
                        socket_path=socket_path,
                    )
                )
    return panes


def capture_pane(pane: Pane, lines: int = 80, min_interval: float = 0.0) -> str:
    cache_key = (pane.socket_path, pane.pane_id)
    cached = PANE_CAPTURE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and min_interval > 0 and now - cached[0] < min_interval:
        return cached[1]
    cmd = ["tmux"]
    if pane.socket_path:
        cmd += ["-S", pane.socket_path]
    cmd += ["capture-pane", "-p", "-t", pane.pane_id, "-S", f"-{max(lines, 20)}"]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = ANSI_RE.sub("", run.stdout).replace("\u00a0", " ") if run.returncode == 0 else ""
    if run.returncode == 0:
        PANE_CAPTURE_CACHE[cache_key] = (now, text)
    return text


def pane_activity_id(text: str) -> str:
    """Fingerprint substantive pane content while ignoring TUI animation noise."""
    if not text:
        return ""
    lines = text.splitlines()[-max(20, min(80, len(text.splitlines()))):]
    normalized_lines: list[str] = []
    for line in lines:
        line = ANSI_RE.sub("", line).replace("\u00a0", " ")
        line = TMUX_DYNAMIC_RE.sub("", line)
        line = CONTROL_RE.sub("", line)
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        normalized_lines.append(line)
    normalized = "\n".join(normalized_lines).strip()
    return stable_hash(normalized, length=32) if normalized else ""


def file_activity_id(path: pathlib.Path | None) -> str:
    if path is None:
        return ""
    try:
        info = path.stat()
    except OSError:
        return ""
    return stable_hash(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, length=32)


def pane_input_prompt(text: str) -> tuple[bool, str]:
    lines = text.splitlines()[-18:]
    tail = "\n".join(lines)
    for pattern in INPUT_PATTERNS:
        match = pattern.search(tail)
        if match:
            # Fingerprint the active prompt and nearby choices, not unrelated
            # output above it that may keep changing.
            excerpt = tail[max(0, match.start() - 120) : match.end() + 500]
            normalized = TMUX_DYNAMIC_RE.sub("", clean_text(excerpt, 800))
            return True, stable_hash(normalized)
    return False, ""


def pane_shows_running(text: str, provider: str) -> bool:
    tail = "\n".join(text.splitlines()[-16:])
    if provider == "codex":
        return bool(
            re.search(r"\bWorking\s*\([^\n]+esc to interrupt", tail, re.I)
            or re.search(r"\b正在工作\s*\(", tail)
        )
    # Claude normally exposes explicit session state. This is only a fallback.
    return bool(re.search(r"(?:esc to interrupt|ctrl-c to interrupt)", tail, re.I))


def pane_for_process(proc: ProcessInfo, panes: Sequence[Pane]) -> Pane | None:
    if proc.tmux_pane:
        for pane in panes:
            if pane.pane_id == proc.tmux_pane and (
                not proc.tmux_socket or pane.socket_path == proc.tmux_socket
            ):
                return pane
    if proc.tty:
        for pane in panes:
            if pane.tty == proc.tty:
                return pane
    return None


def read_json_first_line(path: pathlib.Path) -> Mapping[str, Any] | None:
    try:
        with path.open("rb") as fh:
            line = fh.readline(1024 * 1024)
        value = json.loads(line)
        return value if isinstance(value, Mapping) else None
    except (OSError, ValueError, UnicodeError):
        return None


def find_main_codex_rollout(pid: int, proc_start: str = "") -> tuple[pathlib.Path, str] | None:
    cache_key = (pid, proc_start)
    cached = CODEX_ROLLOUT_CACHE.get(cache_key)
    if cached and cached[0].exists():
        return cached
    paths = {
        path
        for path in open_process_files(pid)
        if "rollout-" in path.name and path.suffix == ".jsonl"
    }
    candidates: list[tuple[pathlib.Path, str]] = []
    for path in paths:
        first = read_json_first_line(path)
        if not first or first.get("type") != "session_meta":
            continue
        payload = first.get("payload")
        if not isinstance(payload, Mapping) or payload.get("source") != "cli":
            continue
        session_id = clean_text(payload.get("id"), 200)
        if session_id:
            candidates.append((path, session_id))
    if candidates:
        current = max(
            candidates,
            key=lambda item: item[0].stat().st_mtime_ns if item[0].exists() else 0,
        )
        CODEX_ROLLOUT_CACHE[cache_key] = current
        return current
    return None


def find_claude_transcript(session_id: str) -> pathlib.Path | None:
    if not session_id or session_id.startswith("pid-"):
        return None
    cached = CLAUDE_TRANSCRIPT_CACHE.get(session_id)
    if cached is not None and cached.exists():
        return cached
    pattern = str(HOME / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    candidates = [pathlib.Path(value) for value in glob.glob(pattern)]
    if not candidates:
        return None
    current = max(
        candidates,
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
    )
    CLAUDE_TRANSCRIPT_CACHE[session_id] = current
    return current


def reverse_json_objects(
    path: pathlib.Path, max_bytes: int = 64 * 1024 * 1024, chunk_size: int = 256 * 1024
) -> Iterable[Mapping[str, Any]]:
    """Yield JSONL objects newest-first without loading a large rollout into memory."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            lower_bound = max(0, size - max_bytes)
            position = size
            carry = b""
            while position > lower_bound:
                amount = min(chunk_size, position - lower_bound)
                position -= amount
                fh.seek(position)
                data = fh.read(amount) + carry
                lines = data.split(b"\n")
                carry = lines[0]
                for raw in reversed(lines[1:]):
                    if not raw:
                        continue
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, Mapping):
                        yield value
            # Only parse the leading fragment when scanning reached file offset 0.
            if position == 0 and carry:
                try:
                    value = json.loads(carry)
                except (ValueError, UnicodeError):
                    value = None
                if isinstance(value, Mapping):
                    yield value
    except OSError:
        return


def codex_last_lifecycle(path: pathlib.Path) -> tuple[str, str, str]:
    """Return state, event id and raw lifecycle event for a main rollout."""
    try:
        stat_result = path.stat()
    except OSError:
        return "unknown", stable_hash(path, "missing"), "unknown"
    cache_key = str(path)
    cached = CODEX_LIFECYCLE_CACHE.get(cache_key)
    if cached and cached[0] == stat_result.st_ino and cached[1] == stat_result.st_size:
        return cached[2]
    relevant = {"task_started", "task_complete", "turn_aborted", "user_message"}
    for item in reverse_json_objects(path):
        if item.get("type") != "event_msg":
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        event_type = clean_text(payload.get("type"), 80)
        if event_type not in relevant:
            continue
        turn_id = clean_text(payload.get("turn_id"), 200)
        timestamp = clean_text(item.get("timestamp"), 100)
        event_id = turn_id or stable_hash(timestamp, event_type)
        if event_type in {"task_started", "user_message"}:
            result = ("running", event_id, event_type)
        elif event_type == "task_complete":
            result = ("ready", event_id, event_type)
        else:
            result = ("error", event_id, event_type)
        CODEX_LIFECYCLE_CACHE[cache_key] = (stat_result.st_ino, stat_result.st_size, result)
        return result
    result = ("unknown", stable_hash(path, stat_result.st_mtime_ns), "unknown")
    CODEX_LIFECYCLE_CACHE[cache_key] = (stat_result.st_ino, stat_result.st_size, result)
    return result


def codex_observation(
    proc: ProcessInfo, panes: Sequence[Pane], capture_lines: int, capture_interval: float = 0.0
) -> Observation:
    pane = pane_for_process(proc, panes)
    rollout = find_main_codex_rollout(proc.pid, proc.start_time)
    session_id = rollout[1] if rollout else f"pid-{proc.pid}-{proc.start_time}"
    rollout_path = rollout[0] if rollout else None
    state, event_id, raw = (
        codex_last_lifecycle(rollout_path) if rollout_path else ("unknown", "", "no-rollout")
    )
    source = "codex-rollout" if rollout else "process"
    pane_text = ""
    if pane and state in {"running", "unknown"}:
        pane_text = capture_pane(pane, capture_lines, capture_interval)
        if pane_shows_running(pane_text, "codex"):
            state, event_id, raw, source = (
                "running",
                stable_hash(session_id, "tmux-running"),
                "tmux-running",
                "tmux",
            )
        else:
            prompted, prompt_id = pane_input_prompt(pane_text)
            if prompted:
                state, event_id, raw, source = "needs_input", prompt_id, "tmux-prompt", "tmux"
    cwd = proc.cwd or (pane.cwd if pane else "")
    return Observation(
        key=f"codex:{session_id}",
        provider="codex",
        session_id=session_id,
        pid=proc.pid,
        proc_start=proc.start_time,
        pane_id=pane.pane_id if pane else "",
        tmux_target=pane.target if pane else "",
        cwd=cwd,
        name=(pane.title if pane and pane.title else project_name(cwd)),
        state=state,
        event_id=event_id or stable_hash(proc.pid, proc.start_time, state),
        source=source,
        raw_status=raw,
        tmux_socket=pane.socket_path if pane else "",
        pane_activity_id=pane_activity_id(pane_text),
        artifact_activity_id=file_activity_id(rollout_path),
    )


def claude_status_to_state(status_value: str) -> str:
    value = status_value.strip().lower()
    if value in CLAUDE_BUSY:
        return "running"
    if value == "idle":
        return "ready"
    if value in CLAUDE_INPUT:
        return "needs_input"
    if value in {"error", "failed", "failure"}:
        return "error"
    if value in {"ended", "exited", "done", "completed"}:
        return "ready"
    return "unknown"


def load_claude_session(pid: int) -> Mapping[str, Any] | None:
    path = HOME / ".claude" / "sessions" / f"{pid}.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    if value.get("pid") is not None and str(value.get("pid")) != str(pid):
        return None
    details = process_details(pid)
    if details and value.get("procStart") is not None:
        if str(value.get("procStart")) != details[1]:
            return None
    return value


def claude_observation(
    proc: ProcessInfo, panes: Sequence[Pane], capture_lines: int, capture_interval: float = 0.0
) -> Observation:
    pane = pane_for_process(proc, panes)
    session = load_claude_session(proc.pid) or {}
    session_id = clean_text(session.get("sessionId"), 200) or f"pid-{proc.pid}-{proc.start_time}"
    raw = clean_text(session.get("status"), 100) or "unknown"
    state = claude_status_to_state(raw)
    stamp = clean_text(session.get("statusUpdatedAt") or session.get("updatedAt"), 100)
    event_id = stable_hash(session_id, raw, stamp)
    source = "claude-session" if session else "process"
    pane_text = ""
    if pane and (not session or state in {"running", "unknown"}):
        pane_text = capture_pane(pane, capture_lines, capture_interval)
    if pane_text and (not session or state == "unknown"):
        prompted, prompt_id = pane_input_prompt(pane_text)
        if prompted:
            state, event_id, raw, source = "needs_input", prompt_id, "tmux-prompt", "tmux"
    cwd = clean_text(session.get("cwd"), 1000) or proc.cwd or (pane.cwd if pane else "")
    name = clean_text(session.get("name"), 160) or (pane.title if pane else "") or project_name(cwd)
    return Observation(
        key=f"claude:{session_id}",
        provider="claude",
        session_id=session_id,
        pid=proc.pid,
        proc_start=proc.start_time,
        pane_id=pane.pane_id if pane else "",
        tmux_target=pane.target if pane else "",
        cwd=cwd,
        name=name,
        state=state,
        event_id=event_id,
        source=source,
        raw_status=raw,
        tmux_socket=pane.socket_path if pane else "",
        pane_activity_id=pane_activity_id(pane_text),
        artifact_activity_id=file_activity_id(
            find_claude_transcript(session_id)
            or (HOME / ".claude" / "sessions" / f"{proc.pid}.json")
        ),
    )


def hook_payload_from_input(extra: Sequence[str]) -> Mapping[str, Any]:
    candidates: list[str] = []
    if extra:
        candidates.extend(reversed(extra))
    if not sys.stdin.isatty():
        with contextlib.suppress(OSError, UnicodeError):
            stdin_text = sys.stdin.read(2 * 1024 * 1024 + 1)
            if len(stdin_text) > 2 * 1024 * 1024:
                return {}
            if stdin_text.strip():
                candidates.insert(0, stdin_text)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, Mapping):
            return value
    return {}


def hook_to_observation(source: str, payload: Mapping[str, Any]) -> Observation | None:
    event = clean_text(
        payload.get("hook_event_name") or payload.get("type") or payload.get("event"), 100
    )
    notification_type = clean_text(payload.get("notification_type"), 100)
    early_tool_name = clean_text(payload.get("tool_name"), 120)
    event_lower = event.lower().replace("-", "_")
    notice_lower = notification_type.lower().replace("-", "_")

    state = "unknown"
    if event_lower in {"permissionrequest", "permission_request", "elicitation"}:
        state = "needs_input"
    elif event_lower in {"pretooluse", "pre_tool_use"} and early_tool_name in {
        "AskUserQuestion",
        "ExitPlanMode",
    }:
        state = "needs_input"
    elif event_lower == "notification":
        if notice_lower in {
            "agent_needs_input",
            "permission_prompt",
            "idle_prompt",
            "elicitation_dialog",
        }:
            state = "needs_input"
        elif notice_lower == "agent_completed":
            state = "ready"
        else:
            return None
    elif event_lower in {"stopfailure", "stop_failure", "turn_aborted", "error"}:
        state = "error"
    elif event_lower in {
        "userpromptsubmit",
        "user_prompt_submit",
        "task_started",
        "turn_started",
    } or (
        event_lower in {"posttooluse", "post_tool_use"}
        and early_tool_name in {"AskUserQuestion", "ExitPlanMode"}
    ):
        state = "running"
    elif event_lower in {
        "stop",
        "agent_turn_complete",
        "agent_turn_completed",
        "task_complete",
        "turn_complete",
        "turn_completed",
    }:
        # Claude Stop can happen while its own background work remains active.
        background = payload.get("background_tasks")
        pending_background = False
        if isinstance(background, list):
            for task in background:
                if isinstance(task, Mapping):
                    task_state = clean_text(task.get("status"), 80).lower()
                    if task_state in {"running", "pending", "active", "working"}:
                        pending_background = True
                        break
        session_crons = payload.get("session_crons")
        pending_crons = isinstance(session_crons, list) and bool(session_crons)
        state = "auto_wait" if (pending_background or pending_crons) else "ready"
    elif event_lower in {"sessionend", "session_end"}:
        state = "exited"
    else:
        return None

    provider = "claude" if source.lower().startswith("claude") else "codex"
    session_id = clean_text(
        payload.get("session_id")
        or payload.get("thread-id")
        or payload.get("thread_id"),
        200,
    )
    tmux_socket, pane_id = tmux_environment_location()
    tmux_target = tmux_target_for_pane(tmux_socket, pane_id)
    if not session_id:
        session_id = f"pane-{pane_id}" if pane_id else f"hook-{stable_hash(payload)}"
    cwd = clean_text(payload.get("cwd") or os.getcwd(), 1000)
    turn_id = clean_text(payload.get("turn_id") or payload.get("turn-id"), 200)
    tool_name = early_tool_name
    identity_payload = {
        "provider": provider,
        "session_id": session_id,
        "event": event_lower,
        "notification_type": notice_lower,
        "turn_id": turn_id,
        "tool_name": tool_name,
        "tool_input_hash": canonical_hash(payload.get("tool_input")),
        "permission_suggestions_hash": canonical_hash(payload.get("permission_suggestions")),
        "message": clean_text(payload.get("message"), 500),
        "title": clean_text(payload.get("title"), 200),
        "agent_id": clean_text(payload.get("agent_id") or payload.get("agentId"), 200),
        "error": clean_text(payload.get("error") or payload.get("error_details"), 500),
    }
    # Hooks without a stable turn/tool/agent identity still need to distinguish
    # later occurrences. Five-second bucketing only applies to that final fallback.
    occurrence_id = clean_text(
        payload.get("tool_use_id")
        or payload.get("call_id")
        or payload.get("timestamp")
        or payload.get("created_at"),
        200,
    )
    if occurrence_id:
        identity_payload["occurrence_id"] = occurrence_id
    elif state in {"needs_input", "error"} or notice_lower == "agent_completed":
        # Some hooks have no stable occurrence id. A short deterministic bucket
        # deduplicates immediate replays without collapsing later real prompts.
        identity_payload["time_bucket"] = int(utc_now() // 5)
    elif not any((turn_id, tool_name, identity_payload["agent_id"], identity_payload["message"])):
        identity_payload["time_bucket"] = int(utc_now() // 5)
    unique = canonical_hash(identity_payload)
    message = clean_text(
        payload.get("message")
        or payload.get("error")
        or payload.get("error_details")
        or payload.get("last_assistant_message"),
        500,
    )
    if not message and tool_name:
        message = f"tool: {tool_name}"
    key = f"{provider}:{session_id}"
    if state == "needs_input":
        key = f"{key}:attention:{unique}"
    elif state == "error":
        key = f"{key}:error:{unique}"
    elif event_lower == "notification" and notice_lower == "agent_completed":
        key = f"{key}:background:{unique}"
    name = clean_text(payload.get("title"), 160) or project_name(cwd)
    return Observation(
        key=key,
        provider=provider,
        session_id=session_id,
        pid=None,
        proc_start="",
        pane_id=pane_id,
        tmux_target=tmux_target,
        cwd=cwd,
        name=name,
        state=state,
        event_id=f"hook:{event_lower or notice_lower}:{unique}",
        source=f"{provider}-hook",
        raw_status=notification_type or event,
        message=message,
        tmux_socket=tmux_socket,
    )


STATE_LABELS = {
    "ready": "Turn finished; review needed",
    "needs_input": "Needs your response or approval",
    "error": "Run failed; review needed",
    "exited": "Process exited (reason unknown)",
    "running": "Running",
    "auto_wait": "Waiting automatically",
    "resolved": "Resolved",
    "unknown": "Unknown status",
}
SHORT_LABELS = {
    "ready": "Review",
    "needs_input": "Reply",
    "error": "Error",
    "exited": "Exited",
    "running": "Running",
    "auto_wait": "Auto-wait",
    "resolved": "Resolved",
    "unknown": "Unknown",
}


def row_public(
    row: Mapping[str, Any], config: Mapping[str, Any], *, local: bool = False
) -> dict[str, Any]:
    include_cwd = bool(config["notifications"].get("include_cwd", False))
    include_preview = bool(config["notifications"].get("include_message_preview", False))
    include_socket = local or bool(
        config["notifications"].get("include_tmux_socket", False)
    )
    result = {
        "provider": row["provider"],
        "tmux_target": row["tmux_target"],
        "project": project_name(row["cwd"]),
        "state": row["state"],
        "state_label": STATE_LABELS.get(row["state"], row["state"]),
        "time": iso_time(),
    }
    if include_socket:
        result["tmux_socket"] = mapping_value(row, "tmux_socket", "")
        result["pane_id"] = mapping_value(row, "pane_id", "")
    if local:
        result["name"] = clean_text(mapping_value(row, "name", ""), 120)
    if include_cwd:
        result["cwd"] = row["cwd"]
    message = mapping_value(row, "message", "")
    if include_preview and message:
        result["message"] = clean_text(message, 240)
    return result


def mapping_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def tmux_attach_command(row: Mapping[str, Any]) -> str:
    target = clean_text(
        mapping_value(row, "pane_id", "") or mapping_value(row, "tmux_target", ""),
        200,
    )
    if not target:
        return ""
    command = ["tmux"]
    socket_path = str(mapping_value(row, "tmux_socket", "") or "")
    if socket_path:
        command += ["-S", socket_path]
    command += ["attach", "-t", target]
    return shlex.join(command)


def format_notification(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    local: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    host = socket.gethostname()
    public_rows = [row_public(row, config, local=local) for row in rows]
    include_socket = local or bool(
        config["notifications"].get("include_tmux_socket", False)
    )
    if len(rows) == 1:
        row = rows[0]
        provider = "Codex" if row["provider"] == "codex" else "Claude"
        label = STATE_LABELS.get(row["state"], row["state"])
        project = project_name(row["cwd"])
        title = f"{provider} · {label}"
        bits = [f"Host: {host}", f"Project: {project}"]
        if row["tmux_target"]:
            socket_path = str(mapping_value(row, "tmux_socket", "") or "")
            server = (
                f" ({pathlib.PurePath(socket_path).name})"
                if socket_path and include_socket
                else ""
            )
            bits.append(f"tmux: {row['tmux_target']}{server}")
            if include_socket:
                bits.append(f"Open: {tmux_attach_command(row)}")
        elif row["pid"]:
            bits.append(f"PID: {row['pid']}")
        body = "\n".join(bits)
    else:
        title = f"Agent Watch · {len(rows)} sessions need attention"
        lines = [f"Host: {host}"]
        for row in rows[:12]:
            provider = "Codex" if row["provider"] == "codex" else "Claude"
            label = SHORT_LABELS.get(row["state"], row["state"])
            socket_path = str(mapping_value(row, "tmux_socket", "") or "")
            socket_name = (
                pathlib.PurePath(socket_path).name
                if socket_path and include_socket
                else ""
            )
            target = (
                f"{socket_name}/{row['tmux_target']}"
                if socket_name and socket_name != "default"
                else row["tmux_target"]
            )
            where = f" · tmux {target}" if target else ""
            lines.append(f"[{label}] {provider} · {project_name(row['cwd'])}{where}")
        if len(rows) > 12:
            lines.append(f"{len(rows) - 12} more sessions")
        body = "\n".join(lines)
    payload = {
        "app": APP_NAME,
        "version": VERSION,
        "host": host,
        "title": title,
        "body": body,
        "events": public_rows,
        "created_at": iso_time(),
    }
    return title, body, payload


def cursor_prompt_for_row(row: Mapping[str, Any]) -> str:
    """Return a bounded latest user prompt, falling back to the hook message."""
    global CURSOR_PROMPT_LOADER
    try:
        if CURSOR_PROMPT_LOADER is None:
            # The dashboard loader already enforces history-root containment,
            # owner checks, bounded reverse reads, and user-message filtering.
            from .dashboard import ConversationPreviewLoader

            CURSOR_PROMPT_LOADER = ConversationPreviewLoader(refresh_seconds=1.0)
        preview = CURSOR_PROMPT_LOADER.load(row)
        user = preview.get("user", {}) if isinstance(preview, Mapping) else {}
        if isinstance(user, Mapping):
            prompt = clean_text(user.get("text"), 220)
            if prompt:
                return prompt
    except (ImportError, OSError, TypeError, ValueError):
        pass
    if mapping_value(row, "source", "") == "manual-test":
        return clean_text(mapping_value(row, "message", ""), 220)
    return ""


def cursor_tmux_label(row: Mapping[str, Any]) -> str:
    target = clean_text(mapping_value(row, "tmux_target", ""), 200)
    if not target:
        return ""
    socket_path = str(mapping_value(row, "tmux_socket", "") or "")
    socket_name = pathlib.PurePath(socket_path).name if socket_path else ""
    if socket_name and socket_name != "default":
        return f"{socket_name}/{target}"
    return target


def format_cursor_notification(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    title: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the editor-only payload: tmux and optional prompt, never host."""
    cursor = config["notifications"].get("cursor", {})
    include_prompt = bool(
        isinstance(cursor, Mapping) and cursor.get("include_prompt", False)
    )
    source_events = payload.get("events", [])
    events: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        source = (
            source_events[index]
            if isinstance(source_events, list)
            and index < len(source_events)
            and isinstance(source_events[index], Mapping)
            else {}
        )
        event = {
            key: source[key]
            for key in (
                "provider",
                "project",
                "state",
                "state_label",
                "time",
            )
            if key in source
        }
        event["tmux_target"] = cursor_tmux_label(row)
        if include_prompt and index < 3:
            prompt = cursor_prompt_for_row(row)
            event["prompt"] = prompt or "unavailable — open the tmux pane"
        events.append(event)

    lines: list[str] = []
    if len(events) == 1:
        event = events[0]
        if event.get("tmux_target"):
            lines.append(f"tmux: {event['tmux_target']}")
        elif event.get("project"):
            lines.append(f"Project: {event['project']}")
        if event.get("prompt"):
            lines.append(f"Prompt: {event['prompt']}")
    else:
        for event in events[:3]:
            provider = {"codex": "Codex", "claude": "Claude"}.get(
                str(event.get("provider") or ""), "Agent"
            )
            state = event.get("state_label") or event.get("state") or "Attention"
            location = (
                f"tmux {event['tmux_target']}"
                if event.get("tmux_target")
                else str(event.get("project") or "unknown")
            )
            line = f"{provider} · {state} · {location}"
            if event.get("prompt"):
                line += f" · Prompt: {event['prompt']}"
            lines.append(line)
        if len(events) > 3:
            lines.append(f"+{len(events) - 3} more")

    return {
        "app": APP_NAME,
        "version": VERSION,
        "title": title,
        "body": "\n".join(lines) or title,
        "events": events,
        "created_at": payload.get("created_at", iso_time()),
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())


def open_no_redirect(request: urllib.request.Request, timeout: float) -> Any:
    return NO_REDIRECT_OPENER.open(request, timeout=timeout)


def validate_notification_url(url: str, allow_insecure_http: bool = False) -> str:
    """Validate an outbound notification URL and return it unchanged."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid notification URL: {clean_text(exc, 120)}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("notification URL must be an absolute http(s) URL")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ValueError(
            "plain HTTP notifications are disabled; use HTTPS or set "
            "notifications.allow_insecure_http=true"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("notification URL port is out of range")
    return url


def http_json(
    url: str,
    payload: Mapping[str, Any],
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "User-Agent": f"agent-watch/{VERSION}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with open_no_redirect(request, timeout) as response:
            response.read(4096)
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, clean_text(exc, 200)


def cursor_notify_socket_path(
    state_dir: str | pathlib.Path,
    requested: str | None = None,
) -> pathlib.Path:
    """Resolve the Cursor bridge socket without accepting ambiguous paths."""
    if requested is not None:
        raw_path = requested
        if not raw_path:
            raise CursorNotifyInputError("--socket must not be empty")
    else:
        raw_path = os.environ.get("AGENT_WATCH_CURSOR_SOCKET", "")
        if not raw_path:
            raw_path = os.fspath(pathlib.Path(state_dir) / CURSOR_NOTIFY_SOCKET_NAME)
    if "\x00" in raw_path:
        raise CursorNotifyInputError("Cursor socket path contains a NUL byte")
    path = pathlib.Path(raw_path).expanduser()
    if not path.is_absolute():
        raise CursorNotifyInputError("Cursor socket path must be absolute")
    return path


def _strict_json_object(raw: bytes, description: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        text = raw.decode("utf-8")
        value = json.loads(text, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{description} must be one valid UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def read_cursor_notification(
    stream: Any | None = None,
    max_bytes: int = CURSOR_NOTIFY_MAX_PAYLOAD_BYTES,
) -> dict[str, Any]:
    """Read exactly one bounded notification object from stdin-like input."""
    if stream is None:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        incoming = stream.read(max_bytes + 1)
    except OSError as exc:
        raise CursorNotifyInputError(
            f"could not read notification payload: {clean_text(exc, 160)}"
        ) from exc
    if isinstance(incoming, str):
        raw = incoming.encode("utf-8")
    elif isinstance(incoming, (bytes, bytearray)):
        raw = bytes(incoming)
    else:
        raise CursorNotifyInputError("notification input must be text or bytes")
    if len(raw) > max_bytes:
        raise CursorNotifyInputError(
            f"notification payload exceeds {max_bytes} bytes"
        )
    if not raw.strip():
        raise CursorNotifyInputError("notification payload is empty")
    try:
        payload = _strict_json_object(raw, "notification payload")
    except ValueError as exc:
        raise CursorNotifyInputError(str(exc)) from exc
    for field in ("title", "body"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CursorNotifyInputError(
                f"notification payload field {field!r} must be a non-empty string"
            )
    return payload


def _encode_cursor_notification(payload: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CursorNotifyInputError("notification payload is not valid JSON") from exc
    if len(raw) > CURSOR_NOTIFY_MAX_PAYLOAD_BYTES:
        raise CursorNotifyInputError(
            f"encoded notification exceeds {CURSOR_NOTIFY_MAX_PAYLOAD_BYTES} bytes"
        )
    return raw + b"\n"


def _validate_cursor_socket(path: pathlib.Path) -> os.stat_result:
    try:
        parent_metadata = path.parent.lstat()
    except FileNotFoundError as exc:
        raise CursorNotifyDeliveryError(
            f"Cursor socket directory does not exist: {path.parent}"
        ) from exc
    except OSError as exc:
        raise CursorNotifyDeliveryError(
            f"cannot inspect Cursor socket directory: {clean_text(exc, 160)}"
        ) from exc
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise CursorNotifyInputError("Cursor socket parent must be a real directory")
    if parent_metadata.st_uid != os.getuid():
        raise CursorNotifyInputError("Cursor socket directory must be owned by this user")
    if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise CursorNotifyInputError(
            "Cursor socket directory must not be accessible by other users"
        )

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise CursorNotifyDeliveryError(
            "Cursor notification socket is not running"
        ) from exc
    except OSError as exc:
        raise CursorNotifyDeliveryError(
            f"cannot inspect Cursor socket: {clean_text(exc, 160)}"
        ) from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise CursorNotifyInputError("Cursor endpoint must be a real Unix socket")
    if metadata.st_uid != os.getuid():
        raise CursorNotifyInputError("Cursor socket must be owned by this user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CursorNotifyInputError("Cursor socket must use mode 0600")
    return metadata


def _cursor_remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CursorNotifyDeliveryError("timed out waiting for the Cursor extension")
    return remaining


def _validate_cursor_peer(client: socket.socket) -> None:
    peer_credential_option = getattr(socket, "SO_PEERCRED", None)
    if peer_credential_option is None:
        return
    credential_size = struct.calcsize("3i")
    credentials = client.getsockopt(
        socket.SOL_SOCKET, peer_credential_option, credential_size
    )
    if len(credentials) != credential_size:
        raise CursorNotifyDeliveryError("could not verify the Cursor extension peer")
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    if peer_uid != os.getuid():
        raise CursorNotifyInputError("Cursor extension peer belongs to another user")


def _read_cursor_ack(client: socket.socket, deadline: float) -> dict[str, Any]:
    line = bytearray()
    while True:
        client.settimeout(_cursor_remaining_timeout(deadline))
        chunk = client.recv(min(4096, CURSOR_NOTIFY_MAX_ACK_BYTES + 1 - len(line)))
        if not chunk:
            raise CursorNotifyDeliveryError(
                "Cursor extension closed without acknowledging the notification"
            )
        newline = chunk.find(b"\n")
        if newline >= 0:
            line.extend(chunk[:newline])
            if len(line) > CURSOR_NOTIFY_MAX_ACK_BYTES:
                raise CursorNotifyDeliveryError("Cursor acknowledgement is too large")
            if chunk[newline + 1 :]:
                raise CursorNotifyDeliveryError(
                    "Cursor extension returned more than one acknowledgement"
                )
            break
        line.extend(chunk)
        if len(line) > CURSOR_NOTIFY_MAX_ACK_BYTES:
            raise CursorNotifyDeliveryError("Cursor acknowledgement is too large")
    try:
        acknowledgement = _strict_json_object(bytes(line), "Cursor acknowledgement")
    except ValueError as exc:
        raise CursorNotifyDeliveryError(str(exc)) from exc
    if acknowledgement.get("ok") is not True:
        detail = clean_text(acknowledgement.get("error"), 200)
        suffix = f": {detail}" if detail else ""
        raise CursorNotifyDeliveryError(
            f"Cursor extension rejected the notification{suffix}"
        )
    return acknowledgement


def send_cursor_notification(
    payload: Mapping[str, Any],
    socket_path: pathlib.Path,
    timeout: float = CURSOR_NOTIFY_DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send one NDJSON notification to a same-user Cursor extension endpoint."""
    wire_payload = _encode_cursor_notification(payload)
    initial_metadata = _validate_cursor_socket(socket_path)
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_cursor_remaining_timeout(deadline))
            client.connect(os.fspath(socket_path))
            connected_metadata = _validate_cursor_socket(socket_path)
            if (
                connected_metadata.st_dev != initial_metadata.st_dev
                or connected_metadata.st_ino != initial_metadata.st_ino
            ):
                raise CursorNotifyInputError(
                    "Cursor socket changed while the connection was established"
                )
            _validate_cursor_peer(client)
            client.settimeout(_cursor_remaining_timeout(deadline))
            client.sendall(wire_payload)
            return _read_cursor_ack(client, deadline)
    except (CursorNotifyInputError, CursorNotifyDeliveryError):
        raise
    except (OSError, TimeoutError) as exc:
        detail = clean_text(exc, 200) or exc.__class__.__name__
        raise CursorNotifyDeliveryError(
            f"could not deliver notification to Cursor: {detail}"
        ) from exc


def send_desktop_notification(title: str, body: str, timeout: float) -> bool | str:
    """Deliver a native desktop notification on Linux or macOS."""

    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if not executable:
            return "unavailable"
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        command = [executable, "-e", script, "--", title, body]
    else:
        executable = shutil.which("notify-send")
        if not executable or not (
            os.environ.get("DISPLAY") or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        ):
            return "unavailable"
        command = [executable, title, body]
    try:
        run = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return run.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def send_notifications(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    skip_channels: set[str] | None = None,
    state_dir: str | pathlib.Path = DEFAULT_STATE_DIR,
) -> tuple[dict[str, Any], dict[str, Any]]:
    title, body, payload = format_notification(rows, config)
    local_title, local_body, _local_payload = format_notification(
        rows, config, local=True
    )
    settings = config["notifications"]
    timeout = float(settings.get("timeout_seconds", 6.0))
    delivered: dict[str, Any] = {}
    skip = skip_channels or set()

    if settings.get("console", True) and "console" not in skip:
        print(f"[{iso_time()}] {local_title}\n{local_body}", flush=True)
        delivered["console"] = True

    if settings.get("tmux", True) and "tmux" not in skip:
        summary = clean_text(
            f"{local_title}: {local_body.splitlines()[-1] if local_body else ''}", 300
        )
        success = False
        for socket_path in tmux_socket_paths(
            str(row.get("tmux_socket") or "") for row in rows
        ) or [""]:
            base = ["tmux"] + (["-S", socket_path] if socket_path else [])
            try:
                clients = subprocess.run(
                    base + ["list-clients", "-F", "#{client_name}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                for client in clients.stdout.splitlines():
                    run = subprocess.run(
                        base
                        + ["display-message", "-d", "10000", "-c", client, "-l", summary],
                        capture_output=True,
                        timeout=2,
                        check=False,
                    )
                    success = success or run.returncode == 0
            except (OSError, subprocess.SubprocessError):
                continue
        delivered["tmux"] = success

    if settings.get("desktop", False) and "desktop" not in skip:
        delivered["desktop"] = send_desktop_notification(
            local_title, local_body, timeout
        )

    cursor = settings.get("cursor", {})
    if (
        "cursor" not in skip
        and isinstance(cursor, Mapping)
        and cursor.get("enabled", False)
    ):
        try:
            configured_socket = str(cursor.get("socket") or "")
            socket_path = cursor_notify_socket_path(
                state_dir,
                configured_socket or None,
            )
            send_cursor_notification(
                format_cursor_notification(rows, config, title, payload),
                socket_path,
                min(timeout, CURSOR_NOTIFY_MAX_TIMEOUT),
            )
            delivered["cursor"] = True
        except (CursorNotifyInputError, CursorNotifyDeliveryError) as exc:
            delivered["cursor"] = (False, clean_text(exc, 200))

    command = settings.get("command", {})
    argv = command.get("argv", []) if isinstance(command, Mapping) else []
    if (
        "command" not in skip
        and isinstance(argv, list)
        and argv
        and all(isinstance(item, str) for item in argv)
    ):
        try:
            run = subprocess.run(
                argv,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            delivered["command"] = run.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            delivered["command"] = clean_text(exc, 200)

    webhook = settings.get("webhook", {})
    if "webhook" not in skip and isinstance(webhook, Mapping) and webhook.get("url"):
        headers: dict[str, str] = {}
        if webhook.get("bearer_token"):
            headers["Authorization"] = f"Bearer {webhook['bearer_token']}"
        try:
            url = validate_notification_url(
                str(webhook["url"]), bool(settings.get("allow_insecure_http", False))
            )
            delivered["webhook"] = http_json(url, payload, timeout, headers)
        except ValueError as exc:
            delivered["webhook"] = (False, clean_text(exc, 200))

    ntfy = settings.get("ntfy", {})
    if "ntfy" not in skip and isinstance(ntfy, Mapping) and ntfy.get("url"):
        headers = {
            "Title": "Agent Watch",
            "Priority": str(ntfy.get("priority", "high")),
            "Tags": "computer,warning",
        }
        if ntfy.get("token"):
            headers["Authorization"] = f"Bearer {ntfy['token']}"
        try:
            url = validate_notification_url(
                str(ntfy["url"]), bool(settings.get("allow_insecure_http", False))
            )
            request = urllib.request.Request(
                url,
                data=f"{title}\n{body}".encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with open_no_redirect(request, timeout) as response:
                response.read(4096)
                delivered["ntfy"] = 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            delivered["ntfy"] = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            delivered["ntfy"] = clean_text(exc, 200)

    telegram = settings.get("telegram", {})
    if (
        "telegram" not in skip
        and isinstance(telegram, Mapping)
        and telegram.get("bot_token")
        and telegram.get("chat_id")
    ):
        url = f"https://api.telegram.org/bot{telegram['bot_token']}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": telegram["chat_id"], "text": f"{title}\n\n{body}"}
        ).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with open_no_redirect(request, timeout) as response:
                response.read(4096)
                delivered["telegram"] = 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            delivered["telegram"] = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            delivered["telegram"] = clean_text(exc, 200)

    bark = settings.get("bark", {})
    if "bark" not in skip and isinstance(bark, Mapping) and bark.get("url"):
        try:
            url = validate_notification_url(
                str(bark["url"]), bool(settings.get("allow_insecure_http", False))
            )
            delivered["bark"] = http_json(
                url,
                {"title": title, "body": body, "group": "agent-watch", "level": "timeSensitive"},
                timeout,
            )
        except ValueError as exc:
            delivered["bark"] = (False, clean_text(exc, 200))

    return payload, delivered


def channel_succeeded(value: Any) -> bool:
    return bool(
        value is True
        or (isinstance(value, (tuple, list)) and value and value[0] is True)
    )


def required_channels(config: Mapping[str, Any]) -> set[str]:
    settings = config["notifications"]
    remote: set[str] = set()
    cursor = settings.get("cursor", {})
    if isinstance(cursor, Mapping) and cursor.get("enabled", False):
        remote.add("cursor")
    command = settings.get("command", {})
    if isinstance(command, Mapping) and command.get("argv"):
        remote.add("command")
    webhook = settings.get("webhook", {})
    if isinstance(webhook, Mapping) and webhook.get("url"):
        remote.add("webhook")
    ntfy = settings.get("ntfy", {})
    if isinstance(ntfy, Mapping) and ntfy.get("url"):
        remote.add("ntfy")
    telegram = settings.get("telegram", {})
    if (
        isinstance(telegram, Mapping)
        and telegram.get("bot_token")
        and telegram.get("chat_id")
    ):
        remote.add("telegram")
    bark = settings.get("bark", {})
    if isinstance(bark, Mapping) and bark.get("url"):
        remote.add("bark")
    if remote:
        return remote
    if settings.get("console", True):
        return {"console"}
    if settings.get("tmux", True):
        return {"tmux"}
    if settings.get("desktop", False):
        return {"desktop"}
    return set()


def wake_cursor_retries_for_new_listener(
    db: StateDB,
    config: Mapping[str, Any],
) -> int:
    """Wake delayed Cursor deliveries once for each newly bound socket inode."""
    settings = config["notifications"].get("cursor", {})
    if not isinstance(settings, Mapping) or not settings.get("enabled", False):
        return 0
    try:
        configured_socket = str(settings.get("socket") or "")
        path = cursor_notify_socket_path(db.state_dir, configured_socket or None)
        metadata = _validate_cursor_socket(path)
    except (CursorNotifyInputError, CursorNotifyDeliveryError):
        return 0
    key = os.fspath(path)
    generation = (metadata.st_dev, metadata.st_ino)
    if CURSOR_SOCKET_GENERATIONS.get(key) == generation:
        return 0
    CURSOR_SOCKET_GENERATIONS[key] = generation
    return db.wake_failed_channel("cursor")


def notify_due(db: StateDB, config: Mapping[str, Any]) -> int:
    db.enqueue_due(config)
    wake_cursor_retries_for_new_listener(db, config)
    claimed = db.claim_outbox()
    if not claimed:
        return 0
    grouped: dict[frozenset[str], list[tuple[sqlite3.Row, dict[str, Any]]]] = {}
    for item in claimed:
        try:
            value = json.loads(item["delivered_json"])
        except (TypeError, ValueError):
            value = {}
        previous = value if isinstance(value, dict) else {}
        success_set = frozenset(
            channel for channel, result in previous.items() if channel_succeeded(result)
        )
        grouped.setdefault(success_set, []).append((item, previous))

    required = required_channels(config)
    for successful_channels, group in grouped.items():
        group_claimed = [item for item, _previous in group]
        rows: list[dict[str, Any]] = [
            json.loads(item["snapshot_json"]) for item in group_claimed
        ]
        payload, attempted = send_notifications(
            rows,
            config,
            skip_channels=set(successful_channels),
            state_dir=db.state_dir,
        )
        delivered: dict[str, Any] = dict(group[0][1])
        delivered.update(attempted)
        # An empty channel set is a valid UI-only configuration. In that mode
        # the event is considered handled locally instead of retrying forever.
        success = all(channel_succeeded(delivered.get(name)) for name in required)
        kind = rows[0]["state"] if len({row["state"] for row in rows}) == 1 else "batch"
        db.record_notification(kind, rows, payload, delivered)
        db.finish_outbox(group_claimed, delivered, success)
        if not success:
            print(
                f"[{iso_time()}] required notification delivery failed; queued for retry: {delivered}",
                file=sys.stderr,
                flush=True,
            )
    return len(claimed)


def scan_once(db: StateDB, config: Mapping[str, Any]) -> list[Observation]:
    monitor = config["monitor"]
    processes = exact_agent_processes()
    panes = list_tmux_panes(proc.tmux_socket for proc in processes)
    ignored = {str(value) for value in monitor.get("ignore_tmux_sessions", [])}
    panes = [pane for pane in panes if pane.session not in ignored]
    capture_lines = int(monitor.get("capture_lines", 80))
    capture_interval = float(monitor.get("tmux_fallback_interval_seconds", 6.0))
    use_tmux = bool(monitor.get("tmux_fallback", True))
    observations: list[Observation] = []
    for proc in processes:
        active_panes = panes if use_tmux else []
        if proc.provider == "codex":
            obs = codex_observation(proc, active_panes, capture_lines, capture_interval)
        else:
            obs = claude_observation(proc, active_panes, capture_lines, capture_interval)
        observations.append(obs)
        if obs.state == "running" and (
            (obs.provider == "claude" and obs.source == "claude-session")
            or (obs.provider == "codex" and obs.raw_status == "tmux-running")
        ):
            db.resolve_attention(
                obs.provider, obs.session_id, older_than_seconds=5.0
            )
        db.upsert(obs, notify_existing=bool(monitor.get("notify_existing", True)))
    seen = {obs.key for obs in observations}
    db.mark_missing_exited(
        seen,
        float(monitor.get("missing_grace_seconds", 10.0)),
        notify=bool(monitor.get("process_exit_notifications", True)),
    )
    return observations


class DaemonLock:
    def __init__(self, path: pathlib.Path):
        ensure_private_dir(path.parent)
        self.fh = path.open("a+")
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def acquire(self) -> None:
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("agent-watch daemon is already running") from exc
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        self.fh.close()


def is_user_prompt_observation(obs: Observation) -> bool:
    raw = obs.raw_status.lower().replace("-", "_")
    return raw in {
        "userpromptsubmit",
        "user_prompt_submit",
        "task_started",
        "turn_started",
        "posttooluse",
        "post_tool_use",
    }


def apply_hook_observation(
    db: StateDB,
    obs: Observation,
    config: Mapping[str, Any] = DEFAULT_CONFIG,
) -> None:
    raw = obs.raw_status.lower().replace("-", "_")
    if is_user_prompt_observation(obs):
        db.resolve_attention(
            obs.provider,
            obs.session_id,
            include_errors=raw in {"userpromptsubmit", "user_prompt_submit"},
            include_background=True,
        )
    elif raw in {
        "stop",
        "stopfailure",
        "stop_failure",
        "sessionend",
        "session_end",
    }:
        db.resolve_attention(
            obs.provider,
            obs.session_id,
            include_errors=raw in {"sessionend", "session_end"},
            include_background=raw in {"sessionend", "session_end"},
        )
    db.upsert(obs, notify_existing=True)
    if obs.state in {"needs_input", "error"} or ":background:" in obs.key:
        db.enqueue_session_now(obs.key, config)


def spool_observation(state_dir: pathlib.Path, obs: Observation) -> None:
    spool = state_dir / "spool"
    ensure_private_dir(spool)
    name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}.json"
    path = spool / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dataclasses.asdict(obs), fh, ensure_ascii=False)
        fh.write("\n")


def drain_spool(
    db: StateDB,
    state_dir: pathlib.Path,
    config: Mapping[str, Any] = DEFAULT_CONFIG,
) -> int:
    spool = state_dir / "spool"
    if not spool.is_dir():
        return 0
    count = 0
    for path in sorted(spool.glob("*.json")):
        try:
            value = json.loads(path.read_text())
            obs = Observation(**value)
            apply_hook_observation(db, obs, config)
            path.unlink()
            count += 1
        except (OSError, ValueError, TypeError):
            # Leave malformed/partially written files for inspection instead of
            # deleting evidence. Atomic O_EXCL writes make this path very rare.
            continue
    return count


def run_daemon(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    state_dir = pathlib.Path(args.state_dir)
    db = StateDB(state_dir)
    if args.notify_existing is not None:
        config = deep_merge(dict(config), {"monitor": {"notify_existing": args.notify_existing}})
    lock = DaemonLock(state_dir / "daemon.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        db.close()
        return 1
    if args.once:
        try:
            db.prune(float(config["monitor"].get("retention_days", 30)))
            drain_spool(db, state_dir, config)
            observations = scan_once(db, config)
            notified = notify_due(db, config)
            print(f"scan: {len(observations)} sessions, {notified} notifications")
        finally:
            lock.close()
            db.close()
        return 0

    stopping = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    interval = max(0.5, float(config["monitor"]["interval_seconds"]))
    retention_days = float(config["monitor"].get("retention_days", 30))
    last_prune = 0.0
    print(f"agent-watch {VERSION} started; polling every {interval:g}s", flush=True)
    try:
        while not stopping:
            started = time.monotonic()
            try:
                db.set_meta("heartbeat", str(os.getpid()))
                if time.monotonic() - last_prune >= 3600:
                    db.prune(retention_days)
                    last_prune = time.monotonic()
                drain_spool(db, state_dir, config)
                notify_due(db, config)
                observations = scan_once(db, config)
                notify_due(db, config)
                db.set_meta("last_success", f"{len(observations)} sessions")
                db.set_meta("last_error", "")
            except Exception as exc:  # keep the monitor alive, but make the failure visible
                with contextlib.suppress(sqlite3.Error):
                    db.conn.rollback()
                message = clean_text(exc, 500)
                with contextlib.suppress(sqlite3.Error):
                    db.set_meta("last_error", message)
                print(f"[{iso_time()}] scan error: {message}", file=sys.stderr, flush=True)
            remaining = interval - (time.monotonic() - started)
            end = time.monotonic() + max(0.0, remaining)
            while not stopping and time.monotonic() < end:
                time.sleep(min(0.5, end - time.monotonic()))
    finally:
        db.set_meta("heartbeat", "stopped")
        lock.close()
        db.close()
    return 0


def run_hook(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    # Hook mode must be silent and must never alter the parent agent's decision.
    try:
        payload = hook_payload_from_input(args.payload)
        obs = hook_to_observation(args.source, payload)
        if obs is None:
            return 0
        state_dir = pathlib.Path(args.state_dir)
        try:
            db = StateDB(state_dir, timeout=0.25)
            apply_hook_observation(db, obs, config)
            db.close()
        except sqlite3.Error:
            spool_observation(state_dir, obs)
    except Exception as exc:
        state_dir = pathlib.Path(args.state_dir)
        ensure_private_dir(state_dir)
        with contextlib.suppress(OSError):
            error_path = state_dir / "hook-errors.log"
            fd = os.open(error_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(error_path, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(f"{iso_time()} {clean_text(exc, 1000)}\n")
    return 0


def atomic_json_write(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def load_json_config(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def backup_file(path: pathlib.Path) -> pathlib.Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.agent-watch-backup-{stamp}")
    shutil.copy2(path, target)
    return target


HOOK_OWNER_MARKER = "AGENT_WATCH_HOOK=v1"


def hook_handler_command(
    source: str,
    state_dir: str | pathlib.Path = DEFAULT_STATE_DIR,
    config_path: str | pathlib.Path = DEFAULT_CONFIG_PATH,
) -> str:
    invoked = pathlib.Path(sys.argv[0]).expanduser()
    hook_args = [
        "--config",
        str(pathlib.Path(config_path).expanduser().absolute()),
        "--state-dir",
        str(pathlib.Path(state_dir).expanduser().absolute()),
        "hook",
        "--source",
        source,
    ]
    if invoked.name == "agent-watch":
        if not invoked.is_absolute():
            located = shutil.which(str(invoked))
            invoked = pathlib.Path(located) if located else invoked
        command = [str(invoked.absolute()), *hook_args]
    else:
        command = [sys.executable, "-m", "agent_watch", *hook_args]
    return f"{HOOK_OWNER_MARKER} {shlex.join(command)}"


def _hook_command_parts(handler: Mapping[str, Any]) -> list[str]:
    if handler.get("type") != "command":
        return []
    try:
        return shlex.split(str(handler.get("command", "")), posix=True)
    except ValueError:
        return []


def is_owned_hook_handler(
    handler: Mapping[str, Any], source: str | None = None
) -> bool:
    parts = _hook_command_parts(handler)
    if not parts or parts[0] != HOOK_OWNER_MARKER:
        return False
    args = parts[2:]
    if args[:2] == ["-m", "agent_watch"]:
        args = args[2:]
    seen_options: set[str] = set()
    while args[:1] and args[0] in {"--config", "--state-dir"} and len(args) >= 3:
        if args[0] in seen_options:
            return False
        seen_options.add(args[0])
        args = args[2:]
    if len(args) != 3 or args[:2] != ["hook", "--source"]:
        return False
    actual_source = args[-1]
    return actual_source in {"codex", "claude"} and (
        source is None or actual_source == source
    )


def is_legacy_agent_watch_handler(
    handler: Mapping[str, Any], source: str | None = None
) -> bool:
    """Recognize only the exact command shape emitted by pre-0.2 installers."""
    parts = _hook_command_parts(handler)
    if len(parts) != 4 or parts[1:3] != ["hook", "--source"]:
        return False
    executable = pathlib.PurePath(parts[0]).name
    actual_source = parts[-1]
    return (
        executable in {"agent-watch", "agent_watch.py"}
        and actual_source in {"codex", "claude"}
        and (source is None or actual_source == source)
    )


def add_hook(
    settings: dict[str, Any],
    event: str,
    source: str,
    matcher: str | None = None,
    async_handler: bool = False,
    state_dir: str | pathlib.Path = DEFAULT_STATE_DIR,
    config_path: str | pathlib.Path = DEFAULT_CONFIG_PATH,
) -> bool:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"hooks.{event} must be a JSON array")
    desired_handler: dict[str, Any] = {
        "type": "command",
        "command": hook_handler_command(source, state_dir, config_path),
        "timeout": 10,
    }
    if source == "claude" and async_handler:
        desired_handler["async"] = True
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for index, handler in enumerate(group["hooks"]):
            if isinstance(handler, Mapping) and (
                is_owned_hook_handler(handler, source)
                or is_legacy_agent_watch_handler(handler, source)
            ):
                changed = dict(handler) != desired_handler
                if matcher is None:
                    if "matcher" in group:
                        group.pop("matcher", None)
                        changed = True
                elif group.get("matcher") != matcher:
                    group["matcher"] = matcher
                    changed = True
                if changed:
                    group["hooks"][index] = desired_handler
                return changed
    group: dict[str, Any] = {"hooks": [desired_handler]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)
    return True


@contextlib.contextmanager
def hook_settings_lock(path: pathlib.Path) -> Iterable[None]:
    ensure_private_dir(path.parent)
    lock_path = path.with_name(f".{path.name}.agent-watch.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _install_hooks_file(
    path: pathlib.Path,
    source: str,
    specifications: Sequence[tuple[str, str | None, bool]],
    state_dir: str | pathlib.Path,
    config_path: str | pathlib.Path,
) -> tuple[bool, pathlib.Path | None]:
    with hook_settings_lock(path):
        settings = load_json_config(path)
        changed = False
        for event, matcher, async_handler in specifications:
            changed = (
                add_hook(
                    settings,
                    event,
                    source,
                    matcher,
                    async_handler,
                    state_dir,
                    config_path,
                )
                or changed
            )
        if not changed:
            return False, None
        backup = backup_file(path)
        atomic_json_write(path, settings)
        return True, backup


def install_hooks(args: argparse.Namespace) -> int:
    changed: list[str] = []
    backups: list[pathlib.Path] = []

    codex_path = HOME / ".codex" / "hooks.json"
    codex_changed, backup = _install_hooks_file(
        codex_path,
        "codex",
        (
            ("UserPromptSubmit", None, False),
            ("PermissionRequest", "*", False),
            ("Stop", None, False),
        ),
        args.state_dir,
        args.config,
    )
    if codex_changed:
        if backup:
            backups.append(backup)
        changed.append(str(codex_path))

    claude_path = HOME / ".claude" / "settings.json"
    claude_changed, backup = _install_hooks_file(
        claude_path,
        "claude",
        (
            ("UserPromptSubmit", None, False),
            ("PreToolUse", "AskUserQuestion|ExitPlanMode", True),
            ("PostToolUse", "AskUserQuestion|ExitPlanMode", False),
            ("PermissionRequest", "*", True),
            ("Elicitation", "*", True),
            ("Stop", None, False),
            ("StopFailure", None, False),
            ("SessionEnd", None, False),
            ("Notification", "agent_needs_input|agent_completed", True),
        ),
        args.state_dir,
        args.config,
    )
    if claude_changed:
        if backup:
            backups.append(backup)
        changed.append(str(claude_path))

    if changed:
        print("Hooks installed:")
        for item in changed:
            print(f"  {item}")
    else:
        print("Hooks are already up to date.")
    if backups:
        print("Backups:")
        for item in backups:
            print(f"  {item}")
    print("New Codex hooks must be trusted once; run /hooks in any Codex session.")
    return 0


def remove_agent_watch_handlers(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            remaining = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, Mapping)
                    and (
                        is_owned_hook_handler(handler)
                        or is_legacy_agent_watch_handler(handler)
                    )
                )
            ]
            if len(remaining) != len(handlers):
                changed = True
            if remaining:
                group = dict(group)
                group["hooks"] = remaining
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return changed


def uninstall_hooks(_args: argparse.Namespace) -> int:
    for path in (HOME / ".codex" / "hooks.json", HOME / ".claude" / "settings.json"):
        if not path.exists():
            continue
        with hook_settings_lock(path):
            settings = load_json_config(path)
            if remove_agent_watch_handlers(settings):
                backup = backup_file(path)
                atomic_json_write(path, settings)
                print(f"Removed agent-watch hooks from {path}; backup: {backup}")
    return 0


def status_command(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    if not args.json and sys.stdout.isatty():
        try:
            from .dashboard import RICH_AVAILABLE, load_snapshot, print_static

            if RICH_AVAILABLE:
                print_static(
                    load_snapshot(
                        pathlib.Path(args.state_dir),
                        heartbeat_max_age=max(
                            60.0,
                            float(config["monitor"].get("interval_seconds", 5)) * 4,
                        ),
                        activity_stale_seconds=float(
                            config["monitor"].get("activity_stale_seconds", 600)
                        ),
                    )
                )
                return 0
        except (ImportError, OSError, RuntimeError, sqlite3.Error):
            pass
    db = StateDB(pathlib.Path(args.state_dir))
    rows = db.sessions()
    heartbeat = db.get_meta("heartbeat")
    alive = db.daemon_alive(
        max_age=max(60.0, float(config["monitor"]["interval_seconds"]) * 4)
    )
    if args.json:
        if args.full:
            sessions = [dict(row) for row in rows]
        else:
            sessions = [
                {
                    "provider": row["provider"],
                    "project": project_name(row["cwd"]),
                    "state": row["state"],
                    "state_since": row["state_since"],
                    "last_seen": row["last_seen"],
                    "last_activity_at": row["last_activity_at"],
                    "tmux_target": row["tmux_target"],
                    "source": row["source"],
                }
                for row in rows
            ]
        result = {
            "daemon_alive": alive,
            "heartbeat": iso_time(heartbeat[1]) if heartbeat else None,
            "sessions": sessions,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        db.close()
        return 0
    print(f"daemon: {'running' if alive else 'stopped'}", end="")
    if heartbeat:
        print(f" (last heartbeat {iso_time(heartbeat[1])})")
    else:
        print()
    if not rows:
        print("No monitored sessions yet.")
        db.close()
        return 0
    print(f"{'State':<10} {'Agent':<7} {'PID':<8} {'tmux':<10} {'Project':<28} Source")
    for row in rows:
        state = SHORT_LABELS.get(row["state"], row["state"])
        provider = "Codex" if row["provider"] == "codex" else "Claude"
        print(
            f"{state:<10} {provider:<7} {str(row['pid'] or '-'):<8} "
            f"{(row['tmux_target'] or '-'):<10} {project_name(row['cwd'])[:28]:<28} {row['source']}"
        )
    db.close()
    return 0


def clear_history_command(args: argparse.Namespace) -> int:
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "Refusing to clear history in a non-interactive shell without --yes.",
                file=sys.stderr,
            )
            return 2
        answer = input("Delete Agent Watch local session and notification history? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    state_dir = pathlib.Path(args.state_dir)
    lock = DaemonLock(state_dir / "daemon.lock")
    try:
        lock.acquire()
    except RuntimeError:
        lock.close()
        print(
            "Stop the agent-watch daemon before clearing history to avoid concurrent writes.",
            file=sys.stderr,
        )
        return 1
    try:
        db = StateDB(state_dir)
        counts = db.clear_history()
        db.close()
        spool_count = 0
        spool_dir = state_dir / "spool"
        if spool_dir.is_dir() and not spool_dir.is_symlink():
            for path in spool_dir.glob("*.json"):
                try:
                    metadata = path.lstat()
                    if (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_uid == os.getuid()
                    ):
                        path.unlink()
                        spool_count += 1
                except OSError:
                    continue
        error_log_removed = False
        error_path = state_dir / "hook-errors.log"
        try:
            metadata = error_path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid():
                error_path.unlink()
                error_log_removed = True
        except OSError:
            pass
    finally:
        lock.close()
    print(
        "Cleared local history: "
        f"{counts['sessions']} sessions, {counts['notifications']} notifications, "
        f"{counts['outbox']} outbox records, {spool_count} hook spool files; "
        f"error log {'removed' if error_log_removed else 'not found'}."
    )
    return 0


def test_notification(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    db = StateDB(pathlib.Path(args.state_dir))
    now = utc_now()
    obs = Observation(
        key=f"test:{stable_hash(now)}",
        provider=args.provider,
        session_id="test",
        pid=os.getpid(),
        proc_start="test",
        pane_id=os.environ.get("TMUX_PANE", ""),
        tmux_target="",
        cwd=os.getcwd(),
        name="notification-test",
        state=args.kind,
        event_id=f"test:{now}",
        source="manual-test",
        message="This is a test notification.",
        observed_at=now - 3600,
    )
    row = {
        "session_key": obs.key,
        "provider": obs.provider,
        "session_id": obs.session_id,
        "pid": obs.pid,
        "proc_start": obs.proc_start,
        "pane_id": obs.pane_id,
        "tmux_target": obs.tmux_target,
        "cwd": obs.cwd,
        "name": obs.name,
        "state": obs.state,
        "state_since": obs.observed_at,
        "event_id": obs.event_id,
        "source": obs.source,
        "raw_status": obs.raw_status,
        "message": obs.message,
    }
    payload, delivered = send_notifications(
        [row],
        config,
        state_dir=pathlib.Path(args.state_dir),
    )
    db.record_notification("test", [row], payload, delivered)
    db.close()
    required = required_channels(config)
    success = bool(required) and all(channel_succeeded(delivered.get(name)) for name in required)
    print(f"Test notification {'succeeded' if success else 'failed'}: {delivered}")
    return 0


def cursor_notify_command(args: argparse.Namespace) -> int:
    """CLI adapter used by the custom-command notification channel."""
    try:
        timeout = float(args.timeout)
        if (
            isinstance(args.timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > CURSOR_NOTIFY_MAX_TIMEOUT
        ):
            raise CursorNotifyInputError(
                f"--timeout must be greater than 0 and at most {CURSOR_NOTIFY_MAX_TIMEOUT:g}"
            )
        requested_socket = getattr(args, "socket_path", None)
        socket_path = cursor_notify_socket_path(args.state_dir, requested_socket)
        payload = read_cursor_notification()
        send_cursor_notification(payload, socket_path, timeout)
        return 0
    except CursorNotifyInputError as exc:
        print(f"Cursor notification input error: {clean_text(exc, 300)}", file=sys.stderr)
        return 2
    except CursorNotifyDeliveryError as exc:
        print(f"Cursor notification failed: {clean_text(exc, 300)}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-watch", description="Monitor Codex and Claude Code sessions"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="command")

    ui = sub.add_parser("ui", aliases=("dashboard",), help="open the live terminal dashboard")
    ui.add_argument("--refresh", type=float, default=1.0, help="database refresh interval in seconds")

    daemon = sub.add_parser("daemon", help="run the monitor")
    daemon.add_argument("--once", action="store_true", help="scan once and exit")
    group = daemon.add_mutually_exclusive_group()
    group.add_argument("--notify-existing", dest="notify_existing", action="store_true")
    group.add_argument("--no-notify-existing", dest="notify_existing", action="store_false")
    daemon.set_defaults(notify_existing=None)

    hook = sub.add_parser("hook", help="ingest a native Codex/Claude hook event")
    hook.add_argument("--source", required=True, choices=("codex", "claude"))
    hook.add_argument("payload", nargs="*")

    status = sub.add_parser("status", help="show monitored sessions")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--full",
        action="store_true",
        help="include sensitive local paths, session IDs, messages, and tmux sockets",
    )

    test = sub.add_parser("test-notification", help="send a test notification")
    test.add_argument("--kind", choices=tuple(ATTENTION_STATES), default="needs_input")
    test.add_argument("--provider", choices=("codex", "claude"), default="codex")

    cursor_notify = sub.add_parser(
        "cursor-notify",
        help="forward one JSON notification from stdin to the Cursor extension",
    )
    cursor_notify.add_argument(
        "--socket",
        dest="socket_path",
        help=(
            "absolute extension socket path (default: "
            "AGENT_WATCH_CURSOR_SOCKET or STATE_DIR/cursor-notify.sock)"
        ),
    )
    cursor_notify.add_argument(
        "--timeout",
        type=float,
        default=CURSOR_NOTIFY_DEFAULT_TIMEOUT,
        help=f"delivery timeout in seconds (default: {CURSOR_NOTIFY_DEFAULT_TIMEOUT:g})",
    )

    sub.add_parser("install-hooks", help="merge native hooks into Codex and Claude settings")
    sub.add_parser("uninstall-hooks", help="remove only agent-watch hooks")
    clear = sub.add_parser("clear-history", help="delete locally retained session history")
    clear.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "ui"
        args.refresh = 1.0
    if args.command == "hook":
        # Hook mode stays silent and falls back to privacy-safe defaults while a
        # config is missing, unsafe, or being edited.
        try:
            hook_config = load_config(pathlib.Path(args.config))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            hook_config = deep_merge(DEFAULT_CONFIG, {})
        return run_hook(args, hook_config)
    if args.command == "cursor-notify":
        # This command is itself a configured notification sink. It must remain
        # usable while the main config is absent or being repaired.
        return cursor_notify_command(args)
    try:
        config = load_config(pathlib.Path(args.config))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "daemon":
        return run_daemon(args, config)
    if args.command in {"ui", "dashboard"}:
        try:
            from .dashboard import run_dashboard
        except ImportError as exc:
            print(f"Failed to load UI: {exc}", file=sys.stderr)
            return 2
        return run_dashboard(
            pathlib.Path(args.state_dir),
            refresh_seconds=max(0.2, float(args.refresh)),
            activity_stale_seconds=float(
                config["monitor"].get("activity_stale_seconds", 600)
            ),
            conversation_preview=bool(
                config.get("ui", {}).get("conversation_preview", False)
            ),
            heartbeat_max_age=max(
                60.0, float(config["monitor"].get("interval_seconds", 5)) * 4
            ),
        )
    if args.command == "status":
        return status_command(args, config)
    if args.command == "test-notification":
        return test_notification(args, config)
    if args.command == "install-hooks":
        return install_hooks(args)
    if args.command == "uninstall-hooks":
        return uninstall_hooks(args)
    if args.command == "clear-history":
        return clear_history_command(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
