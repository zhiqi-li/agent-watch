#!/usr/bin/env python3
"""Claude-inspired terminal dashboard for agent-watch.

Database access is intentionally read-only: the dashboard opens monitor state
with SQLite's mode=ro and never participates in daemon/outbox writes. Explicit
operator actions may resume an agent or send a temporary ``/btw`` progress
question to a validated, hidden tmux pane.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import functools
import json
import os
import pathlib
import queue
import re
import secrets
import select
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from typing import Any, Mapping, Sequence

from . import __version__
from .processes import open_process_files
from .resume import resume_availability, resume_in_new_tmux

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group, RenderableType
    from rich.layout import Layout
    from rich.live import Live
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback is exercised on minimal hosts
    RICH_AVAILABLE = False


VERSION = __version__
# Warm, low-glare terminal palette designed for long-running monitoring.
ORANGE = "color(174)"
ORANGE_SOFT = "color(216)"
TEXT = "default"
MUTED = "color(246)"
FAINT = "color(244)"
GREEN = "color(114)"
YELLOW = "color(220)"
RED = "color(197)"
BLUE = "color(44)"
PURPLE = "color(141)"
SELECT_BG = "color(237)"

GIT_CONTEXT_CACHE: dict[str, tuple[float, str, str]] = {}

SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
STATE_META: dict[str, tuple[str, str, str, int]] = {
    "needs_input": ("?", "Needs input", YELLOW, 0),
    "error": ("×", "Error", RED, 1),
    "ready": ("●", "Ready", GREEN, 2),
    "exited": ("○", "Exited", MUTED, 3),
    "running": ("✳", "Running", ORANGE, 4),
    "auto_wait": ("◷", "Auto-wait", BLUE, 5),
    "unknown": ("?", "Unknown", MUTED, 6),
    "resolved": ("·", "Resolved", FAINT, 7),
    "superseded": ("·", "Superseded", FAINT, 8),
}

FILTERS = ("all", "attention", "running")
FILTER_LABELS = {"all": "Current", "attention": "Attention", "running": "Running"}
EXITED_SUMMARY_KEY = "__agent_watch_exited_sessions__"

ANSI_RE = re.compile(
    r"(?:\x1B\][^\x07]*(?:\x07|\x1B\\)|\x1B\[[0-?]*[ -/]*[@-~]|\x1B[@-_])"
)


def rich_console_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a Rich environment that reflects an interactive terminal emulator.

    Cursor and VS Code tasks may provide a real PTY while inheriting
    ``TERM=dumb`` from their parent process. Rich then deliberately suppresses
    every live refresh, leaving the dashboard apparently blank until it exits.
    ``TERM_PROGRAM`` and ``WT_SESSION`` identify terminal emulators that support
    the full-screen control sequences used here. Keep an explicit dumb terminal
    untouched when neither signal is present.
    """
    result = dict(environ)
    term = result.get("TERM", "").lower()
    emulator_present = bool(result.get("TERM_PROGRAM") or result.get("WT_SESSION"))
    explicitly_compatible = result.get("TTY_COMPATIBLE") == "1"
    if term in {"", "dumb", "unknown"} and (emulator_present or explicitly_compatible):
        result["TERM"] = "xterm-256color"
    return result


@dataclasses.dataclass(slots=True)
class DashboardSnapshot:
    sessions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    daemon_alive: bool = False
    daemon_pid: str = ""
    heartbeat_at: float | None = None
    last_success_at: float | None = None
    last_scan_error: str = ""
    pending_outbox: int = 0
    retrying_outbox: int = 0
    last_notification: dict[str, Any] | None = None
    loaded_at: float = dataclasses.field(default_factory=time.time)
    error: str = ""
    activity_stale_seconds: float = 600.0


@dataclasses.dataclass(slots=True)
class ConversationCacheEntry:
    path: pathlib.Path | None
    signature: tuple[int, int, int, int] | None
    preview: dict[str, dict[str, Any]]
    checked_at: float
    used_at: float


@dataclasses.dataclass(slots=True, frozen=True)
class ProgressSummary:
    """Provider-neutral task progress returned by a temporary /btw query."""

    goal: str
    done: str
    current: str
    next: str
    blocker: str
    provider: str
    captured_at: float


@dataclasses.dataclass(slots=True, frozen=True)
class ProgressProbeResult:
    session_key: str
    summary: ProgressSummary | None = None
    error: str = ""
    bulk: bool = False


def _read_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def load_snapshot(
    state_dir: pathlib.Path,
    heartbeat_max_age: float = 20.0,
    activity_stale_seconds: float = 600.0,
) -> DashboardSnapshot:
    snapshot = DashboardSnapshot(
        activity_stale_seconds=max(30.0, activity_stale_seconds)
    )
    path = state_dir / "state.sqlite3"
    if not path.exists():
        snapshot.error = "State database not found; start agent-watch daemon first."
        return snapshot
    uri = path.resolve().as_uri() + "?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        conn.execute("BEGIN")
        rows = conn.execute(
            """SELECT * FROM sessions
               WHERE state NOT IN ('resolved','superseded')
               ORDER BY last_seen DESC"""
        ).fetchall()
        snapshot.sessions = [dict(row) for row in rows]
        heartbeat = conn.execute(
            "SELECT value, updated_at FROM meta WHERE key='heartbeat'"
        ).fetchone()
        if heartbeat:
            snapshot.daemon_pid = str(heartbeat["value"])
            snapshot.heartbeat_at = float(heartbeat["updated_at"])
            snapshot.daemon_alive = (
                snapshot.daemon_pid != "stopped"
                and time.time() - snapshot.heartbeat_at <= heartbeat_max_age
            )
        with contextlib.suppress(sqlite3.Error):
            success = conn.execute(
                "SELECT value, updated_at FROM meta WHERE key='last_success'"
            ).fetchone()
            failure = conn.execute(
                "SELECT value, updated_at FROM meta WHERE key='last_error'"
            ).fetchone()
            if success:
                snapshot.last_success_at = float(success["updated_at"])
            if failure and str(failure["value"]):
                success_at = snapshot.last_success_at or 0.0
                if float(failure["updated_at"]) >= success_at:
                    snapshot.last_scan_error = sanitize(failure["value"], 500)
        with contextlib.suppress(sqlite3.Error):
            pending = conn.execute(
                """SELECT count(*) AS total,
                          sum(CASE WHEN attempts>0 THEN 1 ELSE 0 END) AS retrying
                   FROM outbox WHERE sent_at IS NULL"""
            ).fetchone()
            snapshot.pending_outbox = int(pending["total"] or 0)
            snapshot.retrying_outbox = int(pending["retrying"] or 0)
        with contextlib.suppress(sqlite3.Error):
            notification = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if notification:
                item = dict(notification)
                item["payload"] = _read_json(item.get("payload_json", ""), {})
                item["delivered"] = _read_json(item.get("delivered_json", ""), {})
                snapshot.last_notification = item
        conn.commit()
    except (OSError, sqlite3.Error) as exc:
        snapshot.error = f"Failed to read state: {exc}"
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
    snapshot.loaded_at = time.time()
    return snapshot


def project_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    path = pathlib.PurePath(cwd)
    name = path.name or str(path)
    if name in {".worktree", "worktree"} and len(path.parts) > 1:
        name = path.parts[-2]
    return sanitize(name, 100)


def git_context(cwd: str, refresh_seconds: float = 5.0) -> tuple[str, str]:
    """Return the repository root and branch for a working directory."""

    if not cwd or len(cwd) > 4096 or "\0" in cwd:
        return "", ""
    now = time.monotonic()
    cached = GIT_CONTEXT_CACHE.get(cwd)
    if cached and now - cached[0] < max(0.0, refresh_seconds):
        return cached[1], cached[2]
    root = ""
    branch = ""
    try:
        if pathlib.Path(cwd).is_dir():
            run = subprocess.run(
                [
                    "git",
                    "-C",
                    cwd,
                    "rev-parse",
                    "--show-toplevel",
                    "--abbrev-ref",
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            lines = run.stdout.splitlines() if run.returncode == 0 else []
            if len(lines) >= 2:
                root = sanitize(lines[0], 1000)
                branch = sanitize(lines[1], 200)
                if branch == "HEAD":
                    branch = "detached HEAD"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    GIT_CONTEXT_CACHE[cwd] = (now, root, branch)
    if len(GIT_CONTEXT_CACHE) > 128:
        oldest = min(GIT_CONTEXT_CACHE, key=lambda key: GIT_CONTEXT_CACHE[key][0])
        GIT_CONTEXT_CACHE.pop(oldest, None)
    return root, branch


def sanitize(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = ANSI_RE.sub("", text)
    # Terminal data ultimately comes from process metadata and SQLite. Remove
    # every control/format character at the output boundary, including OSC/CSI,
    # C1 controls and bidi overrides.
    text = "".join(character if character.isprintable() else " " for character in text)
    text = " ".join(text.split())
    return text[:limit]


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def clock_time(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    return dt.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def shorten_middle(value: str, width: int) -> str:
    value = sanitize(value, max(width * 2, 20))
    if width <= 1:
        return "…"
    if cell_width(value) <= width:
        return value
    if width < 8:
        return crop_cells(value, width - 1) + "…"
    left = (width - 1) // 2
    right = width - left - 1
    return crop_cells(value, left) + "…" + crop_cells(value[::-1], right)[::-1]


def char_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def cell_width(value: str) -> int:
    return sum(char_width(character) for character in value)


def crop_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        size = char_width(character)
        if used + size > width:
            break
        result.append(character)
        used += size
    return "".join(result)


CONTEXT_MAX_BYTES = 4 * 1024 * 1024
CONTEXT_MAX_LINE = 512 * 1024
CONTEXT_MAX_OBJECTS = 4000
SYNTHETIC_CONTEXT_PREFIXES = (
    "<task-notification",
    "<system-reminder",
    "<environment_context",
    "<local-command",
    "<command-name",
    "<permissions instructions",
)


def context_timestamp(value: Any) -> float:
    text = sanitize(value, 100)
    if not text:
        return 0.0
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def clean_context_text(value: Any, limit: int = 1600) -> str:
    raw = "" if value is None else str(value)
    raw = ANSI_RE.sub("", raw)
    cleaned_lines: list[str] = []
    for raw_line in raw.splitlines() or [raw]:
        line = sanitize(raw_line, max(limit * 2, 2000)).strip()
        if not line or line.startswith("```"):
            continue
        if re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        line = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*+]\s+", "• ", line)
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            line = " · ".join(cell for cell in cells if cell)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        if line:
            cleaned_lines.append(line)
    return sanitize(" ".join(cleaned_lines), limit)


def context_entry(text: Any, timestamp: Any) -> dict[str, Any] | None:
    cleaned = clean_context_text(text, 1600)
    if not cleaned or cleaned.lower().startswith(SYNTHETIC_CONTEXT_PREFIXES):
        return None
    return {"text": cleaned, "at": context_timestamp(timestamp)}


def reverse_context_objects(
    path: pathlib.Path,
    max_bytes: int = CONTEXT_MAX_BYTES,
    chunk_size: int = 128 * 1024,
) -> Any:
    """Yield bounded JSONL objects newest-first without loading the transcript."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            lower_bound = max(0, size - max_bytes)
            position = size
            carry = b""
            yielded = 0
            while position > lower_bound and yielded < CONTEXT_MAX_OBJECTS:
                amount = min(chunk_size, position - lower_bound)
                position -= amount
                handle.seek(position)
                data = handle.read(amount) + carry
                lines = data.split(b"\n")
                carry = lines[0]
                for raw in reversed(lines[1:]):
                    if not raw or len(raw) > CONTEXT_MAX_LINE:
                        continue
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, Mapping):
                        yielded += 1
                        yield value
                        if yielded >= CONTEXT_MAX_OBJECTS:
                            break
            if position == 0 and carry and len(carry) <= CONTEXT_MAX_LINE:
                try:
                    value = json.loads(carry)
                except (ValueError, UnicodeError):
                    value = None
                if isinstance(value, Mapping):
                    yield value
    except OSError:
        return


def extract_codex_preview(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    preview: dict[str, dict[str, Any]] = {}
    for item in reverse_context_objects(path):
        item_type = str(item.get("type") or "")
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        timestamp = item.get("timestamp")
        payload_type = str(payload.get("type") or "")
        if (
            item_type == "event_msg"
            and payload_type == "user_message"
            and "user" not in preview
        ):
            entry = context_entry(payload.get("message"), timestamp)
            if entry:
                preview["user"] = entry
        elif (
            item_type == "event_msg"
            and payload_type == "agent_message"
            and "assistant" not in preview
        ):
            entry = context_entry(payload.get("message"), timestamp)
            if entry:
                preview["assistant"] = entry
        elif (
            item_type == "response_item"
            and payload_type in {"function_call", "custom_tool_call"}
            and "tool" not in preview
        ):
            name = sanitize(payload.get("name"), 100)
            if name:
                preview["tool"] = {"text": name, "at": context_timestamp(timestamp)}
        if all(key in preview for key in ("user", "assistant", "tool")):
            break
    return preview


def _claude_text_content(content: Any, role: str) -> tuple[str, str]:
    if isinstance(content, str):
        return (content, "") if role == "user" else ("", "")
    if not isinstance(content, list):
        return "", ""
    if role == "user" and any(
        isinstance(block, Mapping) and block.get("type") == "tool_result"
        for block in content
    ):
        return "", ""
    texts: list[str] = []
    tool = ""
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            value = block.get("text")
            if isinstance(value, str):
                texts.append(value)
        elif role == "assistant" and block_type == "tool_use" and not tool:
            tool = sanitize(block.get("name"), 100)
    return " ".join(texts), tool


def extract_claude_preview(
    path: pathlib.Path, session_id: str
) -> dict[str, dict[str, Any]]:
    preview: dict[str, dict[str, Any]] = {}
    for item in reverse_context_objects(path):
        if item.get("isSidechain") is True or item.get("isMeta") is True:
            continue
        item_session = str(item.get("sessionId") or "")
        if item_session and item_session != session_id:
            continue
        item_type = str(item.get("type") or "")
        if item_type not in {"user", "assistant"}:
            continue
        if item_type == "user" and (
            str(item.get("promptSource") or "").lower() == "system"
            or item.get("sourceToolAssistantUUID")
            or item.get("sourceToolUseID")
        ):
            continue
        message = item.get("message")
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or item_type)
        text, tool = _claude_text_content(message.get("content"), role)
        timestamp = item.get("timestamp")
        if role == "user" and "user" not in preview:
            entry = context_entry(text, timestamp)
            if entry:
                preview["user"] = entry
        elif role == "assistant":
            if "assistant" not in preview:
                entry = context_entry(text, timestamp)
                if entry:
                    preview["assistant"] = entry
            if tool and "tool" not in preview:
                preview["tool"] = {"text": tool, "at": context_timestamp(timestamp)}
        if all(key in preview for key in ("user", "assistant", "tool")):
            break
    return preview


class ConversationPreviewLoader:
    """Read only the selected session's visible messages into a small memory cache."""

    def __init__(self, home: pathlib.Path | None = None, refresh_seconds: float = 1.0):
        self.home = (home or pathlib.Path.home()).resolve()
        self.codex_root = (self.home / ".codex" / "sessions").resolve()
        self.claude_root = (self.home / ".claude" / "projects").resolve()
        self.refresh_seconds = max(0.2, refresh_seconds)
        self.cache: dict[str, ConversationCacheEntry] = {}

    def _safe_file(self, path: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
        try:
            if path.is_symlink():
                return None
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                return None
            return resolved
        except (OSError, ValueError):
            return None

    @staticmethod
    def _valid_session_id(session_id: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9._-]{3,200}", session_id))

    @staticmethod
    def _codex_session_matches(path: pathlib.Path, session_id: str) -> bool:
        try:
            with path.open("rb") as handle:
                raw = handle.readline(CONTEXT_MAX_LINE + 1)
            if len(raw) > CONTEXT_MAX_LINE:
                return False
            item = json.loads(raw)
            return bool(
                isinstance(item, Mapping)
                and item.get("type") == "session_meta"
                and isinstance(item.get("payload"), Mapping)
                and str(item["payload"].get("id") or "") == session_id
            )
        except (OSError, ValueError, UnicodeError):
            return False

    @staticmethod
    def _claude_session_matches(path: pathlib.Path, session_id: str) -> bool:
        try:
            consumed = 0
            with path.open("rb") as handle:
                for _index in range(32):
                    raw = handle.readline(CONTEXT_MAX_LINE + 1)
                    if not raw or len(raw) > CONTEXT_MAX_LINE:
                        break
                    consumed += len(raw)
                    if consumed > CONTEXT_MAX_LINE:
                        break
                    try:
                        item = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(item, Mapping) and item.get("sessionId") is not None:
                        return str(item.get("sessionId")) == session_id
        except OSError:
            return False
        return False

    def _find_codex(
        self, row: Mapping[str, Any], session_id: str
    ) -> pathlib.Path | None:
        pid = int(row.get("pid") or 0)
        if pid > 0:
            for target in open_process_files(pid):
                if "rollout-" not in target.name or target.suffix != ".jsonl":
                    continue
                safe = self._safe_file(target, self.codex_root)
                if safe and self._codex_session_matches(safe, session_id):
                    return safe
        pattern = f"*/*/*/rollout-*{session_id}.jsonl"
        candidates: list[pathlib.Path] = []
        with contextlib.suppress(OSError):
            candidates = list(self.codex_root.glob(pattern))
        for candidate in sorted(
            candidates,
            key=lambda value: value.stat().st_mtime_ns if value.exists() else 0,
            reverse=True,
        ):
            safe = self._safe_file(candidate, self.codex_root)
            if safe and self._codex_session_matches(safe, session_id):
                return safe
        return None

    def _find_claude(self, session_id: str) -> pathlib.Path | None:
        candidates: list[pathlib.Path] = []
        with contextlib.suppress(OSError):
            candidates = list(self.claude_root.glob(f"*/{session_id}.jsonl"))
        for candidate in candidates:
            safe = self._safe_file(candidate, self.claude_root)
            if safe and self._claude_session_matches(safe, session_id):
                return safe
        return None

    def _find_path(self, row: Mapping[str, Any]) -> pathlib.Path | None:
        provider = str(row.get("provider") or "")
        session_id = str(row.get("session_id") or "")
        if not self._valid_session_id(session_id) or session_id.startswith("pid-"):
            return None
        if provider == "codex":
            return self._find_codex(row, session_id)
        if provider == "claude":
            return self._find_claude(session_id)
        return None

    @staticmethod
    def _signature(path: pathlib.Path) -> tuple[int, int, int, int] | None:
        try:
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        except OSError:
            return None

    def _trim(self) -> None:
        if len(self.cache) <= 12:
            return
        oldest = min(self.cache, key=lambda key: self.cache[key].used_at)
        self.cache.pop(oldest, None)

    def load(self, row: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not row:
            return {}
        key = f"{row.get('provider')}:{row.get('session_id')}"
        now = time.monotonic()
        cached = self.cache.get(key)
        if cached and now - cached.checked_at < self.refresh_seconds:
            cached.used_at = now
            return cached.preview

        path = cached.path if cached else None
        provider = str(row.get("provider") or "")
        root = self.codex_root if provider == "codex" else self.claude_root
        if path is None or self._safe_file(path, root) is None:
            path = self._find_path(row)
        signature = self._signature(path) if path else None
        if cached and path == cached.path and signature == cached.signature:
            cached.checked_at = now
            cached.used_at = now
            return cached.preview

        preview: dict[str, dict[str, Any]] = {}
        if path and signature:
            if provider == "codex":
                preview = extract_codex_preview(path)
            elif provider == "claude":
                preview = extract_claude_preview(path, str(row.get("session_id") or ""))
        if cached and path == cached.path:
            merged = dict(cached.preview)
            merged.update(preview)
            preview = merged
        self.cache[key] = ConversationCacheEntry(
            path=path,
            signature=signature,
            preview=preview,
            checked_at=now,
            used_at=now,
        )
        self._trim()
        return preview


def state_visual(state: str, spinner_index: int = 0) -> tuple[str, str, str]:
    symbol, label, color, _priority = STATE_META.get(state, STATE_META["unknown"])
    if state == "running":
        symbol = SPINNER[spinner_index % len(SPINNER)]
    return symbol, label, color


def last_activity_age(row: Mapping[str, Any], now: float | None = None) -> float | None:
    value = float(row.get("last_activity_at") or 0)
    if value <= 0:
        return None
    return max(0.0, (time.time() if now is None else now) - value)


def is_stalled(
    row: Mapping[str, Any], stale_seconds: float, now: float | None = None
) -> bool:
    age = last_activity_age(row, now)
    return (
        str(row.get("state") or "") == "running"
        and age is not None
        and age >= stale_seconds
    )


def session_priority(
    row: Mapping[str, Any], stale_seconds: float = 600.0
) -> tuple[Any, ...]:
    state = str(row.get("state", "unknown"))
    priority = STATE_META.get(state, STATE_META["unknown"])[3]
    if is_stalled(row, stale_seconds):
        priority = 3.5
    return (
        priority,
        -float(row.get("state_since") or 0),
        project_name(str(row.get("cwd") or "")).lower(),
    )


def visible_sessions(
    snapshot: DashboardSnapshot, filter_mode: str = "all", query: str = ""
) -> list[dict[str, Any]]:
    # Exited sessions are retained as resumable history, not current work. They
    # live behind the dashboard's history entry instead of competing with
    # sessions that can still require attention.
    rows = [row for row in snapshot.sessions if row.get("state") != "exited"]
    if filter_mode == "attention":
        rows = [
            row
            for row in rows
            if row.get("state") in {"needs_input", "error", "ready"}
            or is_stalled(row, snapshot.activity_stale_seconds)
        ]
    elif filter_mode == "running":
        rows = [row for row in rows if row.get("state") in {"running", "auto_wait"}]
    query = query.strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if query
            in " ".join(
                sanitize(row.get(field, ""), 1000).lower()
                for field in (
                    "provider",
                    "name",
                    "cwd",
                    "tmux_target",
                    "session_id",
                    "state",
                )
            )
        ]
    return sorted(
        rows,
        key=lambda row: session_priority(row, snapshot.activity_stale_seconds),
    )


def exited_sessions(
    snapshot: DashboardSnapshot, query: str = ""
) -> list[dict[str, Any]]:
    """Return retained exited sessions, newest exit first."""
    rows = [row for row in snapshot.sessions if row.get("state") == "exited"]
    query = query.strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if query
            in " ".join(
                sanitize(row.get(field, ""), 1000).lower()
                for field in ("provider", "name", "cwd", "session_id", "state")
            )
        ]
    return sorted(
        rows,
        key=lambda row: (
            -float(row.get("state_since") or 0),
            project_name(str(row.get("cwd") or "")).lower(),
            str(row.get("session_key") or ""),
        ),
    )


def exited_summary_row(snapshot: DashboardSnapshot) -> dict[str, Any] | None:
    """Build the selectable main-screen entry for retained exited sessions."""
    rows = exited_sessions(snapshot)
    if not rows:
        return None
    latest = max(float(row.get("state_since") or 0) for row in rows)
    return {
        "_kind": "exited_summary",
        "session_key": EXITED_SUMMARY_KEY,
        "state": "exited",
        "name": "Exited sessions",
        "exit_count": len(rows),
        "state_since": latest,
    }


def is_exited_summary(row: Mapping[str, Any] | None) -> bool:
    return bool(row and row.get("_kind") == "exited_summary")


def state_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in STATE_META}
    for row in rows:
        state = str(row.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def provider_label(provider: str) -> tuple[str, str]:
    if provider == "claude":
        return "Claude", ORANGE_SOFT
    if provider == "codex":
        return "Codex", PURPLE
    return sanitize(provider or "Agent", 12), MUTED


def tmux_location_label(row: Mapping[str, Any], limit: int = 80) -> str:
    target = sanitize(row.get("tmux_target") or "", limit)
    if not target:
        return ""
    socket_path = sanitize(row.get("tmux_socket") or "", 500)
    socket_name = pathlib.PurePath(socket_path).name if socket_path else ""
    if socket_name and socket_name != "default":
        return sanitize(f"{socket_name}/{target}", limit)
    return target


def successful_channels(delivered: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for name, value in delivered.items():
        ok = value is True or (
            isinstance(value, (list, tuple)) and bool(value) and value[0] is True
        )
        if ok:
            result.append(sanitize(name, 60))
    return sorted(result)


class DashboardView:
    def __init__(self, snapshot: DashboardSnapshot, conversation_preview: bool = True):
        self.snapshot = snapshot
        self.selected_key = ""
        self.selected_index = 0
        self.filter_mode = "all"
        self.history_mode = False
        self.query = ""
        self.searching = False
        self.show_help = False
        self.flash = ""
        self.flash_until = 0.0
        self.spinner_index = 0
        self.conversation_preview = conversation_preview
        self.context_session_key = ""
        self.context_preview: dict[str, dict[str, Any]] = {}
        self.progress_summaries: dict[str, ProgressSummary] = {}
        self.progress_errors: dict[str, str] = {}
        self.progress_pending: set[str] = set()
        self._ensure_selection()

    @property
    def rows(self) -> list[dict[str, Any]]:
        if self.history_mode:
            return exited_sessions(self.snapshot, self.query)
        rows = visible_sessions(self.snapshot, self.filter_mode, self.query)
        if not self.query:
            summary = exited_summary_row(self.snapshot)
            if summary is not None:
                rows.append(summary)
        return rows

    @property
    def selected(self) -> dict[str, Any] | None:
        rows = self.rows
        if not rows:
            return None
        self._ensure_selection()
        return rows[self.selected_index]

    def update(self, snapshot: DashboardSnapshot) -> None:
        if snapshot.error and not snapshot.sessions and self.snapshot.sessions:
            snapshot = dataclasses.replace(
                self.snapshot,
                loaded_at=snapshot.loaded_at,
                error=snapshot.error,
            )
        self.snapshot = snapshot
        current_keys = {
            str(row.get("session_key") or "") for row in snapshot.sessions
        }
        self.progress_summaries = {
            key: summary
            for key, summary in self.progress_summaries.items()
            if key in current_keys
        }
        self.progress_errors = {
            key: error
            for key, error in self.progress_errors.items()
            if key in current_keys
        }
        self._ensure_selection()

    def _ensure_selection(self) -> None:
        rows = self.rows
        if not rows:
            self.selected_index = 0
            self.selected_key = ""
            return
        if self.selected_key:
            for index, row in enumerate(rows):
                if row.get("session_key") == self.selected_key:
                    self.selected_index = index
                    return
        self.selected_index = min(max(0, self.selected_index), len(rows) - 1)
        self.selected_key = str(rows[self.selected_index].get("session_key", ""))

    def move(self, delta: int) -> None:
        rows = self.rows
        if not rows:
            return
        self.selected_index = min(max(0, self.selected_index + delta), len(rows) - 1)
        self.selected_key = str(rows[self.selected_index].get("session_key", ""))

    def jump(self, index: int) -> None:
        rows = self.rows
        if not rows:
            return
        self.selected_index = min(max(0, index), len(rows) - 1)
        self.selected_key = str(rows[self.selected_index].get("session_key", ""))

    def cycle_filter(self) -> None:
        if self.history_mode:
            return
        index = FILTERS.index(self.filter_mode)
        self.filter_mode = FILTERS[(index + 1) % len(FILTERS)]
        self.selected_index = 0
        self.selected_key = ""
        self._ensure_selection()

    def open_exited_sessions(self) -> None:
        self.history_mode = True
        self.query = ""
        self.searching = False
        self.selected_index = 0
        self.selected_key = ""
        self.set_context("", {})
        self._ensure_selection()

    def close_exited_sessions(self) -> None:
        self.history_mode = False
        self.query = ""
        self.searching = False
        self.selected_index = 0
        self.selected_key = EXITED_SUMMARY_KEY
        self.set_context("", {})
        self._ensure_selection()

    def set_flash(self, message: str, seconds: float = 3.0) -> None:
        self.flash = sanitize(message, 300)
        self.flash_until = time.time() + seconds

    def set_context(self, session_key: str, preview: dict[str, dict[str, Any]]) -> bool:
        changed = (
            session_key != self.context_session_key or preview != self.context_preview
        )
        self.context_session_key = session_key
        self.context_preview = preview
        return changed

    def begin_progress(self, session_key: str) -> bool:
        if not session_key or session_key in self.progress_pending:
            return False
        self.progress_errors.pop(session_key, None)
        self.progress_pending.add(session_key)
        return True

    def finish_progress(self, result: ProgressProbeResult) -> None:
        self.progress_pending.discard(result.session_key)
        if result.summary is not None:
            self.progress_summaries[result.session_key] = result.summary
            self.progress_errors.pop(result.session_key, None)
        elif result.error:
            self.progress_errors[result.session_key] = result.error


def render_header(view: DashboardView, width: int) -> RenderableType:
    snapshot = view.snapshot
    counts = state_counts(snapshot.sessions)
    stalled_count = sum(
        1
        for row in snapshot.sessions
        if is_stalled(row, snapshot.activity_stale_seconds)
    )
    brand = Text()
    brand.append("✳ ", style=f"bold {ORANGE}")
    brand.append("agent-watch", style=f"bold {TEXT}")
    if width >= 58:
        brand.append(f"  v{VERSION}", style=FAINT)

    health = Text(justify="right")
    if snapshot.daemon_alive:
        if snapshot.last_scan_error:
            health.append("● LIVE" if width >= 48 else "● RUN", style=f"bold {YELLOW}")
            if width >= 48:
                health.append("  ·  Recent scan failed", style=YELLOW)
        else:
            health.append("● LIVE" if width >= 48 else "● OK", style=f"bold {GREEN}")
            if width >= 48:
                health.append("  ·  Updated just now", style=MUTED)
    else:
        health.append("× MONITOR STOPPED", style=f"bold {RED}")
        if snapshot.heartbeat_at:
            health.append(
                f"  ·  {human_duration(time.time() - snapshot.heartbeat_at)} ago",
                style=MUTED,
            )
    top = Table.grid(expand=True, padding=0)
    top.add_column(ratio=1)
    top.add_column(justify="right", no_wrap=True)
    top.add_row(brand, health)

    summary = Text(no_wrap=True, overflow="ellipsis")
    if view.history_mode:
        exited_count = counts.get("exited", 0)
        summary.append("○ ", style=f"bold {MUTED}")
        summary.append(str(exited_count), style=f"bold {TEXT}")
        summary.append(" exited sessions", style=MUTED)
        if width >= 70:
            summary.append("  ·  Enter resumes in a new tmux session", style=FAINT)
    elif width < 110:
        chips = [
            ("needs_input", counts.get("needs_input", 0), "input"),
            ("ready", counts.get("ready", 0), "ready"),
            ("running", counts.get("running", 0), "running"),
        ]
        if counts.get("error", 0):
            chips.insert(1, ("error", counts.get("error", 0), "errors"))
    else:
        chips = [
            ("needs_input", counts.get("needs_input", 0), "needs input"),
            ("error", counts.get("error", 0), "errors"),
            ("ready", counts.get("ready", 0), "ready"),
            ("running", counts.get("running", 0), "running"),
            ("auto_wait", counts.get("auto_wait", 0), "auto-wait"),
        ]
    if not view.history_mode:
        for index, (state, count, label) in enumerate(chips):
            symbol, _full_label, color = state_visual(state, view.spinner_index)
            if index:
                summary.append("    ")
            summary.append(f"{symbol} ", style=f"bold {color}")
            summary.append(str(count), style=f"bold {TEXT}")
            summary.append(f" {label}", style=MUTED)
    if stalled_count and not view.history_mode:
        summary.append("    ")
        summary.append("⚠ ", style=f"bold {YELLOW}")
        summary.append(str(stalled_count), style=f"bold {TEXT}")
        summary.append(" possibly stalled" if width >= 70 else " stalled", style=YELLOW)
    if snapshot.pending_outbox:
        summary.append("    ")
        summary.append(f"⇡ {snapshot.pending_outbox} pending", style=YELLOW)
    if snapshot.retrying_outbox:
        summary.append(f" / {snapshot.retrying_outbox} retrying", style=RED)

    content: list[RenderableType] = [
        Padding(top, (0, 1)),
        Rule(style=FAINT),
        Padding(summary, (0, 2)),
    ]
    if snapshot.error:
        content.append(
            Padding(
                Text(sanitize(snapshot.error, 500), style=RED, overflow="ellipsis"),
                (0, 2),
            )
        )
    elif snapshot.last_scan_error:
        content.append(
            Padding(
                Text(
                    f"Latest scan failed: {snapshot.last_scan_error}",
                    style=YELLOW,
                    overflow="ellipsis",
                ),
                (0, 2),
            )
        )
    return Group(*content)


def render_sessions(view: DashboardView, width: int, height: int) -> RenderableType:
    rows = view.rows
    compact = width < 72
    now = time.time()
    max_items = max(1, (height - 4) // 2)
    if rows and len(rows) > max_items:
        start = max(0, view.selected_index - max_items // 2)
        start = min(start, len(rows) - max_items)
        shown = rows[start : start + max_items]
    else:
        start = 0
        shown = rows
    content: list[RenderableType] = []
    last_group = ""
    for offset, row in enumerate(shown):
        index = start + offset
        selected = index == view.selected_index
        if is_exited_summary(row):
            if last_group != "history-summary":
                if content:
                    content.append(Text(""))
                heading = Text()
                heading.append("History", style=f"bold {TEXT}")
                content.append(Padding(heading, (0, 2)))
                last_group = "history-summary"

            count = int(row.get("exit_count") or 0)
            elapsed = human_duration(now - float(row.get("state_since") or now))
            left = Text()
            left.append(
                "❯ " if selected else "  ",
                style=f"bold {ORANGE}" if selected else FAINT,
            )
            left.append("○  ", style=f"bold {MUTED}")
            left.append("Exited sessions", style=f"bold {TEXT}" if selected else TEXT)
            if selected:
                left.stylize(f"on {SELECT_BG}")
            line = Table.grid(expand=True, padding=0)
            line.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            line.add_column(width=28, justify="left", no_wrap=True, overflow="ellipsis")
            line.add_row(
                left, Text(f"{count} retained  ·  latest {elapsed} ago", style=MUTED)
            )
            content.append(Padding(line, (0, 1)))

            meta = Text()
            meta.append("   ⎿  ", style=MUTED)
            meta.append("Enter", style=f"bold {ORANGE}")
            meta.append(" to browse and resume in a new tmux session", style=MUTED)
            content.append(Padding(meta, (0, 1)))
            continue

        state = str(row.get("state") or "unknown")
        symbol, label, color = state_visual(state, view.spinner_index)
        stalled = is_stalled(row, view.snapshot.activity_stale_seconds, now)
        if stalled:
            symbol, label, color = "⚠", "Possibly stalled", YELLOW
        provider, provider_color = provider_label(str(row.get("provider") or ""))
        group = (
            "history"
            if view.history_mode
            else (
                "attention"
                if state in {"needs_input", "error", "ready"} or stalled
                else "active"
            )
        )
        if group != last_group:
            if content:
                content.append(Text(""))
            if group == "history":
                group_count = len(rows)
                group_label = "Exited sessions"
            else:
                group_count = sum(
                    1
                    for item in rows
                    if not is_exited_summary(item)
                    and (
                        item.get("state") in {"needs_input", "error", "ready"}
                        or is_stalled(item, view.snapshot.activity_stale_seconds, now)
                    )
                    == (group == "attention")
                )
                group_label = "Needs attention" if group == "attention" else "Active"
            heading = Text()
            heading.append(group_label, style=f"bold {TEXT}")
            heading.append(f"  {group_count}", style=MUTED)
            if group == "history":
                heading.append("  ·  newest first", style=FAINT)
            content.append(Padding(heading, (0, 2)))
            last_group = group

        left = Text()
        left.append(
            "❯ " if selected else "  ", style=f"bold {ORANGE}" if selected else FAINT
        )
        left.append(f"{symbol}  ", style=f"bold {color}")
        if not compact:
            left.append(f"{provider:<7}", style=provider_color)
        left.append(
            project_name(str(row.get("cwd") or "")),
            style=f"bold {TEXT}" if selected else TEXT,
        )
        if selected:
            left.stylize(f"on {SELECT_BG}")
        target = "" if view.history_mode else tmux_location_label(row, 28)
        elapsed = human_duration(now - float(row.get("state_since") or now))
        activity_age = last_activity_age(row, now)
        if view.history_mode:
            timing = f"exited {elapsed} ago"
        elif state == "running":
            if activity_age is None:
                timing = "Establishing baseline"
            elif stalled:
                timing = f"⚠ no update {human_duration(activity_age)}"
            else:
                timing = f"updated {human_duration(activity_age)} ago"
        else:
            timing = elapsed
        line = Table.grid(expand=True, padding=0)
        line.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        if compact:
            line.add_column(width=1, no_wrap=True)
            line.add_column(width=16, justify="left", no_wrap=True, overflow="ellipsis")
            line.add_row(
                left,
                Text(""),
                Text(timing, style=YELLOW if stalled else MUTED),
            )
        else:
            location_width = 20 if width >= 80 else 17
            line.add_column(width=1, no_wrap=True)
            line.add_column(
                width=location_width,
                justify="left",
                no_wrap=True,
                overflow="ellipsis",
            )
            line.add_column(width=1, no_wrap=True)
            line.add_column(
                width=16,
                justify="left",
                no_wrap=True,
                overflow="ellipsis",
            )
            if view.history_mode:
                available, _reason = resume_availability(row)
                location = Text(
                    "Resume ↵" if available else "Unavailable",
                    style=GREEN if available else MUTED,
                )
            else:
                location = Text(f"tmux {target}" if target else "", style=BLUE)
            timing_cell = Text(
                f"{'·' if (target or view.history_mode) else ' '}  {timing}",
                style=YELLOW if stalled else MUTED,
            )
            line.add_row(left, Text(""), location, Text(""), timing_cell)
        content.append(Padding(line, (0, 1)))

        meta = Table.grid(expand=True, padding=0)
        meta.add_column(width=5, no_wrap=True)
        meta.add_column(width=11, no_wrap=True, overflow="ellipsis")
        meta.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        branch = Text("   ⎿", style=MUTED)
        state_cell = Text(label, style=color)
        context = Text()
        session_key = str(row.get("session_key") or "")
        progress = view.progress_summaries.get(session_key)
        progress_error = view.progress_errors.get(session_key, "")
        if view.history_mode:
            available, reason = resume_availability(row)
            context.append(
                (
                    "·  Enter to resume in a new tmux session"
                    if available
                    else f"·  {reason}"
                ),
                style=GREEN if available else MUTED,
            )
        elif session_key in view.progress_pending:
            context.append("·  asking /btw for global progress…", style=BLUE)
        elif progress is not None:
            progress_text = progress.current or progress.done or progress.next
            context.append(
                f"·  {sanitize(progress_text, 400) or 'Progress captured'}",
                style=GREEN,
            )
        elif progress_error:
            context.append("·  /btw progress unavailable", style=RED)
        elif target and compact:
            context.append(f"·  tmux {target}", style=BLUE)
        if (
            not compact
            and not view.history_mode
            and session_key not in view.progress_pending
            and progress is None
            and not progress_error
        ):
            context.append(
                f"·  {source_label(str(row.get('source') or ''))}", style=MUTED
            )
        meta.add_row(branch, state_cell, context)
        content.append(Padding(meta, (0, 1)))

    if not rows:
        empty = "No exited sessions" if view.history_mode else "No matching sessions"
        if view.query:
            empty += f": {view.query}"
        elif view.history_mode:
            empty += ". Resumed sessions return to Current."
        content.append(Padding(Text(f"·  {empty}", style=MUTED), (2, 2)))

    if len(rows) > len(shown):
        info = Text(
            f"{start + 1}–{start + len(shown)} / {len(rows)}  ·  "
            f"{'Exited sessions' if view.history_mode else FILTER_LABELS[view.filter_mode]}",
            style=FAINT,
            justify="right",
        )
        content.append(Padding(info, (0, 2)))
    return Group(*content)


def source_label(source: str) -> str:
    labels = {
        "claude-session": "Claude session state",
        "codex-rollout": "Codex lifecycle log",
        "claude-hook": "Claude native hook",
        "codex-hook": "Codex native hook",
        "tmux": "tmux interface",
        "process": "Process fallback",
    }
    return labels.get(source, sanitize(source or "—", 80))


def detail_line(label: str, value: str, color: str = TEXT) -> Text:
    line = Text()
    label = sanitize(label, 40)
    value = sanitize(value, 2000)
    line.append(label, style=MUTED)
    line.append(" " * max(1, 8 - cell_width(label)), style=MUTED)
    line.append(value or "—", style=color)
    return line


@functools.lru_cache(maxsize=256)
def _wrap_preview_text(text: str, width: int, max_lines: int) -> tuple[str, ...]:
    width = max(4, width)
    max_lines = max(1, max_lines)
    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        chunk = crop_cells(remaining, width)
        if not chunk:
            break
        consumed = len(chunk)
        if consumed < len(remaining):
            split_at = chunk.rfind(" ")
            if split_at >= max(2, len(chunk) // 3):
                chunk = chunk[:split_at]
                consumed = split_at + 1
        lines.append(chunk.rstrip())
        remaining = remaining[consumed:].lstrip()
    if remaining and lines:
        lines[-1] = crop_cells(lines[-1], width - 1).rstrip() + "…"
    return tuple(lines)


def wrap_preview_lines(value: Any, width: int, max_lines: int) -> list[str]:
    text = sanitize(value, 4000)
    return list(_wrap_preview_text(text, width, max_lines))


def preview_block(
    label: str,
    entry: Mapping[str, Any],
    width: int,
    max_lines: int,
    color: str,
    symbol: str,
) -> RenderableType:
    heading = Text()
    heading.append(f"{symbol} ", style=f"bold {color}")
    heading.append(label, style=f"bold {TEXT}")
    timestamp = float(entry.get("at") or 0)
    if timestamp:
        heading.append(f"  ·  {clock_time(timestamp)[:5]}", style=FAINT)
    body = Text("\n".join(wrap_preview_lines(entry.get("text"), width, max_lines)))
    return Group(heading, body)


def progress_summary_block(
    summary: ProgressSummary, width: int, height: int
) -> RenderableType:
    heading = Text()
    heading.append("◉ ", style=f"bold {GREEN}")
    heading.append("Global progress", style=f"bold {TEXT}")
    heading.append(f"  ·  {clock_time(summary.captured_at)[:5]}", style=FAINT)
    if height >= 30:
        fields = (
            ("Goal", summary.goal, TEXT),
            ("Done", summary.done, GREEN),
            ("Current", summary.current, ORANGE),
            ("Next", summary.next, BLUE),
            ("Blocked", summary.blocker, YELLOW),
        )
    elif height >= 24:
        fields = (
            ("Goal", summary.goal, TEXT),
            ("Current", summary.current, ORANGE),
            ("Next", summary.next, BLUE),
            ("Blocked", summary.blocker, YELLOW),
        )
    else:
        fields = (
            ("Current", summary.current, ORANGE),
            ("Next", summary.next, BLUE),
        )
    value_width = max(8, width - 12)
    body = [
        detail_line(
            label,
            crop_cells(value or "—", value_width),
            MUTED if not value else color,
        )
        for label, value, color in fields
    ]
    return Group(heading, *body)


def tool_display_name(name: Any) -> str:
    value = sanitize(name, 100)
    labels = {
        "exec": "Run terminal command",
        "exec_command": "Run terminal command",
        "apply_patch": "Edit files",
        "bash": "Run Bash",
        "read": "Read file",
        "edit": "Edit file",
        "write": "Write file",
        "agent": "Run sub-agent",
        "schedulewakeup": "Wait for wakeup",
        "taskcreate": "Create task",
        "taskupdate": "Update task",
        "spawn_agent": "Start sub-agent",
        "wait_agent": "Wait for sub-agent",
        "send_message": "Message sub-agent",
        "followup_task": "Continue subtask",
        "web__run": "Search the web",
    }
    return labels.get(value.lower(), value or "—")


def render_detail(view: DashboardView, width: int, height: int) -> RenderableType:
    row = view.selected
    if row is None:
        return Panel(
            Align.center(
                Text("Select a session to view details", style=MUTED), vertical="middle"
            ),
            title="Details",
            title_align="left",
            box=box.MINIMAL,
            border_style=FAINT,
        )
    if is_exited_summary(row):
        count = int(row.get("exit_count") or 0)
        latest = human_duration(
            time.time() - float(row.get("state_since") or time.time())
        )
        heading = Text()
        heading.append("○ ", style=f"bold {MUTED}")
        heading.append("Exited sessions", style=f"bold {TEXT}")
        body = Text()
        body.append(
            f"{count} retained session{'s' if count != 1 else ''}\n", style=TEXT
        )
        body.append(f"Latest exit was {latest} ago.\n\n", style=MUTED)
        body.append("Press ", style=MUTED)
        body.append("Enter", style=f"bold {ORANGE}")
        body.append(
            " to browse recent exits and resume one in a new tmux session.", style=MUTED
        )
        return Panel(
            Group(heading, Text("─" * max(8, min(width - 8, 44)), style=FAINT), body),
            title="History",
            title_align="left",
            box=box.MINIMAL,
            border_style=FAINT,
            padding=(0, 1),
        )
    state = str(row.get("state") or "unknown")
    symbol, label, color = state_visual(state, view.spinner_index)
    provider, provider_color = provider_label(str(row.get("provider") or ""))
    now = time.time()
    stalled = is_stalled(row, view.snapshot.activity_stale_seconds, now)
    activity_age = last_activity_age(row, now)
    if stalled:
        symbol, label, color = "⚠", "Possibly stalled", YELLOW

    heading = Text()
    heading.append(f"{symbol} ", style=f"bold {color}")
    heading.append(label, style=f"bold {color}")
    heading.append("\n")
    heading.append(provider, style=f"bold {provider_color}")
    heading.append("  ·  ", style=FAINT)
    heading.append(project_name(str(row.get("cwd") or "")), style=f"bold {TEXT}")

    rule = Text("─" * max(8, min(width - 8, 44)), style=FAINT)
    lines: list[RenderableType] = [heading, rule]
    duration = human_duration(now - float(row.get("state_since") or now))
    if state == "running":
        if activity_age is None:
            activity_text = "Establishing activity baseline"
        elif stalled:
            activity_text = (
                f"⚠ no update for {human_duration(activity_age)}; possibly stalled"
            )
        else:
            activity_text = f"updated {human_duration(activity_age)} ago"
        activity_text += f"  ·  running for {duration}"
        lines.append(
            detail_line("Activity", activity_text, YELLOW if stalled else GREEN)
        )
    else:
        lines.append(
            detail_line("Exited" if state == "exited" else "Duration", duration)
        )
    cwd = str(row.get("cwd") or "")
    repository_root, branch = git_context(cwd)
    lines.append(
        detail_line(
            "Path",
            shorten_middle(cwd, max(14, width - 12)) if cwd else "—",
        )
    )
    branch_text = branch
    if (
        branch
        and repository_root
        and pathlib.Path(repository_root) != pathlib.Path(cwd)
    ):
        branch_text = f"{branch}  ·  repo {pathlib.Path(repository_root).name}"
    lines.append(
        detail_line(
            "Branch",
            shorten_middle(branch_text, max(14, width - 12)) if branch_text else "—",
            PURPLE if branch_text else MUTED,
        )
    )
    lines.append(
        detail_line(
            "Last tmux" if state == "exited" else "tmux",
            tmux_location_label(row, 100) or "—",
            BLUE,
        )
    )
    socket_path = sanitize(row.get("tmux_socket") or "", 300)
    if socket_path and pathlib.Path(socket_path).name != "default" and height >= 24:
        lines.append(
            detail_line(
                "Server", shorten_middle(socket_path, max(16, width - 12)), BLUE
            )
        )
    if height >= 22:
        session_id = sanitize(row.get("session_id") or "", 300)
        lines.append(
            detail_line(
                "Session",
                shorten_middle(session_id, max(14, width - 12)) if session_id else "—",
            )
        )
    if height >= 18:
        process_text = f"PID {row.get('pid') or '—'}  ·  {source_label(str(row.get('source') or ''))}"
        lines.append(
            detail_line("Process", shorten_middle(process_text, max(14, width - 12)))
        )

    session_key = str(row.get("session_key") or "")
    preview = (
        view.context_preview
        if view.conversation_preview and view.context_session_key == session_key
        else {}
    )
    progress = view.progress_summaries.get(session_key)
    progress_error = view.progress_errors.get(session_key, "")
    progress_pending = session_key in view.progress_pending
    lines.append(rule)
    content_width = max(14, width - 6)
    progress_cost = 0
    if progress is not None:
        lines.append(progress_summary_block(progress, content_width, height))
        progress_cost = 6 if height >= 30 else 5 if height >= 24 else 3
        lines.append(rule)
    elif progress_pending:
        pending = Text()
        pending.append("◌ ", style=f"bold {BLUE}")
        pending.append("Asking /btw for global progress…", style=f"bold {TEXT}")
        lines.append(pending)
        progress_cost = 2
        lines.append(rule)
    elif progress_error:
        failure = Text()
        failure.append("× /btw progress unavailable\n", style=f"bold {RED}")
        failure.append(
            crop_cells(progress_error, max(8, content_width - 2)), style=MUTED
        )
        lines.append(failure)
        progress_cost = 3
        lines.append(rule)
    if height >= 30:
        user_lines, assistant_lines = 4, max(8, height - 20)
    elif height >= 24:
        user_lines, assistant_lines = 3, max(5, height - 20)
    elif height >= 20:
        user_lines, assistant_lines = 2, 5
    else:
        user_lines, assistant_lines = 1, 2
    assistant_lines = max(1, assistant_lines - progress_cost)

    user_entry = preview.get("user")
    assistant_entry = preview.get("assistant")
    tool_entry = preview.get("tool")
    if user_entry:
        used_user_lines = len(
            wrap_preview_lines(user_entry.get("text"), content_width, user_lines)
        )
        assistant_lines += max(0, user_lines - used_user_lines)
    else:
        assistant_lines += user_lines + 1
    if user_entry:
        lines.append(
            preview_block(
                "Latest request", user_entry, content_width, user_lines, ORANGE, "❯"
            )
        )
    if assistant_entry:
        assistant_at = float(assistant_entry.get("at") or 0)
        user_at = float(user_entry.get("at") or 0) if user_entry else 0
        assistant_label = (
            "Latest progress" if assistant_at >= user_at else "Previous reply"
        )
        lines.append(
            preview_block(
                assistant_label,
                assistant_entry,
                content_width,
                assistant_lines,
                GREEN,
                "✳",
            )
        )
    if tool_entry and height >= 24:
        tool_value = {
            "text": tool_display_name(tool_entry.get("text")),
            "at": tool_entry.get("at"),
        }
        tool_at = float(tool_entry.get("at") or 0)
        latest_message_at = max(
            float(user_entry.get("at") or 0) if user_entry else 0,
            float(assistant_entry.get("at") or 0) if assistant_entry else 0,
        )
        tool_label = (
            "Current action"
            if (
                state == "running"
                and tool_at
                and tool_at >= latest_message_at
                and now - tool_at < 120
            )
            else "Last action"
        )
        lines.append(
            preview_block(
                tool_label,
                tool_value,
                content_width,
                1,
                BLUE,
                "↳",
            )
        )
    if not view.conversation_preview:
        privacy = Text()
        privacy.append("Conversation preview hidden\n", style=f"bold {MUTED}")
        privacy.append("Press ", style=MUTED)
        privacy.append("p", style=f"bold {ORANGE}")
        privacy.append(" to show local session context", style=MUTED)
        lines.append(privacy)
    elif not any((user_entry, assistant_entry, tool_entry)):
        lines.append(Text("·  No conversation preview available", style=MUTED))
    if state == "exited" and view.history_mode:
        if height >= 20:
            lines.append(Text(""))
        available, reason = resume_availability(row)
        action = Text()
        if available:
            action.append("Enter", style=f"bold {ORANGE}")
            action.append(" to resume in a new tmux session", style=MUTED)
            action.append("  ·  ", style=FAINT)
            action.append("Esc", style=f"bold {ORANGE}")
            action.append(" to return", style=MUTED)
        else:
            action.append("Resume unavailable\n", style=f"bold {MUTED}")
            action.append(reason, style=MUTED)
        lines.append(action)
    elif row.get("tmux_target"):
        if height >= 20:
            lines.append(Text(""))
        action = Text()
        action.append("Enter", style=f"bold {ORANGE}")
        action.append(" to open this tmux session", style=MUTED)
        action.append("  ·  ", style=FAINT)
        action.append("tmux prefix + L", style=f"bold {ORANGE}")
        action.append(" to return", style=MUTED)
        lines.append(action)

    return Panel(
        Group(*lines),
        title="Exited session" if view.history_mode else "Session preview",
        title_align="left",
        box=box.MINIMAL,
        border_style=color if state in {"needs_input", "error"} else FAINT,
        padding=(0, 1),
    )


def render_help(width: int) -> RenderableType:
    keys = Table(box=None, padding=(0, 2), show_header=False)
    keys.add_column("key", style=f"bold {ORANGE}", no_wrap=True)
    keys.add_column("action", style=TEXT)
    for key, action in (
        ("↑ / k", "Previous session"),
        ("↓ / j", "Next session"),
        ("Enter", "Open, browse, or resume the selected item"),
        ("Esc", "Return from exited sessions"),
        ("tmux prefix + L", "Return to Agent Watch after opening a session"),
        ("/", "Search projects, paths, or sessions"),
        ("f", "Cycle Current / Attention / Running"),
        ("b", "Ask selected eligible session for global progress"),
        ("B", "Ask all eligible Codex / Claude sessions"),
        ("p", "Show / hide local session preview"),
        ("r", "Refresh now"),
        ("g / G", "Jump to first / last"),
        ("?", "Close this help"),
        ("q", "Close dashboard; daemon keeps running"),
    ):
        keys.add_row(key, action)
    panel = Panel(
        keys,
        title=" ✳ Keyboard shortcuts ",
        subtitle=" Esc or ? to return ",
        box=box.ROUNDED,
        border_style=ORANGE,
        width=min(62, max(36, width - 6)),
        padding=(1, 2),
    )
    return Align.center(panel, vertical="middle")


def render_footer(
    view: DashboardView, width: int, minimal: bool = False
) -> RenderableType:
    action = Text()
    action_right = Text()
    if view.searching:
        action.append("❯ ", style=f"bold {ORANGE}")
        action.append("Search sessions  ", style=MUTED)
        action.append(view.query, style=TEXT)
        action.append("█", style=ORANGE)
        action.append("    Enter confirm · Esc clear", style=FAINT)
    elif view.flash and time.time() < view.flash_until:
        action.append("● ", style=YELLOW)
        action.append(view.flash, style=TEXT)
    else:
        row = view.selected
        action.append("❯ ", style=f"bold {ORANGE}")
        if is_exited_summary(row):
            count = int(row.get("exit_count") or 0)
            action.append("Exited sessions", style=f"bold {TEXT}")
            action.append(f"  ·  {count} retained", style=MUTED)
            action_right.append("Enter to browse", style=ORANGE)
        elif row:
            session_key = str(row.get("session_key") or "")
            provider, provider_color = provider_label(str(row.get("provider") or ""))
            _symbol, label, color = state_visual(
                str(row.get("state") or "unknown"), view.spinner_index
            )
            action.append(provider, style=provider_color)
            action.append("  ·  ", style=FAINT)
            action.append(project_name(str(row.get("cwd") or "")), style=TEXT)
            action.append("  ·  ", style=FAINT)
            action.append(label, style=color)
            if session_key in view.progress_pending:
                action.append("  ·  asking /btw…", style=BLUE)
            if view.history_mode:
                available, reason = resume_availability(row)
                action_right.append(
                    "Enter to resume" if available else reason,
                    style=ORANGE if available else MUTED,
                )
            elif row.get("tmux_target") and width >= 70:
                action_right.append(f"tmux {tmux_location_label(row, 100)}", style=BLUE)
            activity_age = last_activity_age(row)
            if (
                str(row.get("state") or "") == "running"
                and activity_age is not None
                and width >= 92
            ):
                action.append("  ·  ", style=FAINT)
                action.append(
                    f"updated {human_duration(activity_age)} ago",
                    style=(
                        YELLOW
                        if is_stalled(row, view.snapshot.activity_stale_seconds)
                        else MUTED
                    ),
                )
        else:
            action.append("Select a session", style=MUTED)

    shortcuts = Text(no_wrap=True, overflow="ellipsis")
    enter_label = "resume" if view.history_mode else "open"
    if is_exited_summary(view.selected):
        enter_label = "browse"
    if width < 48:
        items = [("↵", enter_label), ("↑↓", "select"), ("f", "filter"), ("q", "quit")]
    else:
        items = [("↑↓", "select"), ("enter", enter_label), ("/", "search")]
        if view.history_mode:
            items.append(("esc", "back"))
        else:
            items.append(("f", "filter"))
    if width >= 120:
        items.extend(
            [
                ("prefix+L", "back"),
                ("b", "progress"),
                ("p", "preview"),
                ("r", "refresh"),
                ("?", "help"),
            ]
        )
    if not any(key == "q" for key, _label in items):
        items.append(("q", "quit"))
    for index, (key, label) in enumerate(items):
        if index:
            shortcuts.append("  ·  ", style=FAINT)
        shortcuts.append(key, style=f"bold {ORANGE}")
        shortcuts.append(f" {label}", style=MUTED)
    right = Text(style=FAINT, justify="right")
    if width >= 105:
        if view.history_mode:
            right.append(f"{len(exited_sessions(view.snapshot))} exited sessions")
        else:
            current_count = sum(
                row.get("state") != "exited" for row in view.snapshot.sessions
            )
            right.append(
                f"{current_count} sessions  ·  {FILTER_LABELS[view.filter_mode]}"
            )
    footer_line = Table.grid(expand=True, padding=(0, 1))
    footer_line.add_column(ratio=1)
    footer_line.add_column(justify="right", no_wrap=True)
    footer_line.add_row(shortcuts, right)
    action_line = Table.grid(expand=True, padding=(0, 1))
    action_line.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    action_line.add_column(justify="right", no_wrap=True)
    action_line.add_row(action, action_right)
    if minimal:
        return Group(
            Rule(style=FAINT),
            Padding(action_line, (0, 1)),
            Padding(footer_line, (0, 1)),
        )
    return Group(
        Rule(style=FAINT),
        Padding(action_line, (0, 1)),
        Rule(style=FAINT),
        Padding(footer_line, (0, 2)),
    )


def render_dashboard(view: DashboardView, width: int, height: int) -> RenderableType:
    width = max(36, width)
    height = max(12, height)
    layout = Layout()
    header_height = 4 if view.snapshot.error else 3
    minimal_footer = height < 16
    footer_height = 3 if minimal_footer else 4
    layout.split_column(
        Layout(name="header", size=header_height),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=footer_height),
    )
    layout["header"].update(render_header(view, width))
    layout["footer"].update(render_footer(view, width, minimal=minimal_footer))

    if view.show_help:
        layout["main"].update(render_help(width))
        return layout

    main_height = max(6, height - header_height - footer_height)
    if width >= 100:
        layout["main"].split_row(
            Layout(name="sessions", ratio=3, minimum_size=55),
            Layout(name="detail", ratio=2, minimum_size=36),
        )
        list_width = int(width * 0.6)
        layout["sessions"].update(render_sessions(view, list_width, main_height))
        layout["detail"].update(render_detail(view, width - list_width, main_height))
    else:
        layout["main"].update(render_sessions(view, width, main_height))
    return layout


def render_static(snapshot: DashboardSnapshot, width: int = 120) -> RenderableType:
    view = DashboardView(snapshot)
    rows = view.rows
    table = Table(box=box.SIMPLE, expand=True, header_style=f"bold {MUTED}")
    table.add_column("Status", width=11, no_wrap=True)
    table.add_column("Agent", width=9, no_wrap=True)
    table.add_column("Project", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("tmux", width=11, no_wrap=True)
    table.add_column("Duration", width=9, justify="right", no_wrap=True)
    table.add_column("Activity", width=13, justify="right", no_wrap=True)
    table.add_column("Source", ratio=2, no_wrap=True, overflow="ellipsis")
    now = time.time()
    for row in rows:
        if is_exited_summary(row):
            count = int(row.get("exit_count") or 0)
            table.add_row(
                Text("○ History", style=MUTED),
                "",
                Text(f"{count} exited session{'s' if count != 1 else ''}"),
                "—",
                Text(
                    human_duration(now - float(row.get("state_since") or now)),
                    style=MUTED,
                ),
                "—",
                Text("Open agent-watch ui to browse", style=MUTED),
            )
            continue
        symbol, label, color = state_visual(str(row.get("state") or "unknown"))
        stalled = is_stalled(row, snapshot.activity_stale_seconds, now)
        if stalled:
            symbol, label, color = "⚠", "Possibly stalled", YELLOW
        provider, provider_color = provider_label(str(row.get("provider") or ""))
        age = last_activity_age(row, now)
        activity = "—"
        activity_color = MUTED
        if str(row.get("state") or "") == "running":
            activity = (
                "Establishing baseline" if age is None else f"{human_duration(age)} ago"
            )
            activity_color = YELLOW if stalled else GREEN
        table.add_row(
            Text(f"{symbol} {label}", style=color),
            Text(provider, style=provider_color),
            Text(project_name(str(row.get("cwd") or ""))),
            Text(tmux_location_label(row, 18) or "—"),
            Text(human_duration(now - float(row.get("state_since") or now))),
            Text(activity, style=activity_color),
            Text(source_label(str(row.get("source") or ""))),
        )
    if not rows:
        table.add_row("·", "", "No sessions", "", "", "", "")
    return Group(render_header(view, width), Padding(table, (0, 1)))


class RawTerminal:
    def __init__(self, stream: Any = sys.stdin):
        self.stream = stream
        self.fd = stream.fileno()
        self.previous: list[Any] | None = None

    def __enter__(self) -> "RawTerminal":
        self.previous = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.restore()

    def restore(self) -> None:
        if self.previous is not None:
            previous, self.previous = self.previous, None
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(self.fd, termios.TCSANOW, previous)

    def read_key(self, timeout: float = 0.15) -> tuple[str, str]:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return "", ""
        data = os.read(self.fd, 1)
        if data == b"\x1b":
            end = time.monotonic() + 0.025
            while time.monotonic() < end:
                more, _, _ = select.select([self.fd], [], [], 0.005)
                if not more:
                    break
                data += os.read(self.fd, 16)
            mapping = {
                b"\x1b[A": "up",
                b"\x1b[B": "down",
                b"\x1b[5~": "page_up",
                b"\x1b[6~": "page_down",
                b"\x1b[H": "home",
                b"\x1b[F": "end",
                b"\x1bOH": "home",
                b"\x1bOF": "end",
            }
            return mapping.get(data, "escape"), ""
        if data in {b"\r", b"\n"}:
            return "enter", ""
        if data in {b"\x7f", b"\b"}:
            return "backspace", ""
        if data == b"\x03":
            return "quit", ""
        first = data[0]
        expected = 1
        if first & 0b11110000 == 0b11110000:
            expected = 4
        elif first & 0b11100000 == 0b11100000:
            expected = 3
        elif first & 0b11000000 == 0b11000000:
            expected = 2
        while len(data) < expected:
            more, _, _ = select.select([self.fd], [], [], 0.02)
            if not more:
                break
            data += os.read(self.fd, expected - len(data))
        with contextlib.suppress(UnicodeDecodeError):
            text = data.decode("utf-8")
            return "text", text
        return "", ""


def _command_value(value: Any, limit: int = 1000) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit or any(not character.isprintable() for character in text):
        return ""
    return text


def _tmux_base(socket_path: str) -> list[str]:
    return ["tmux", "-S", socket_path] if socket_path else ["tmux"]


def progress_probe_availability(row: Mapping[str, Any] | None) -> str:
    """Return an explanation when a session cannot be queried safely."""
    if not row or is_exited_summary(row):
        return "Select a current session first"
    provider = str(row.get("provider") or "")
    if provider not in {"codex", "claude"}:
        return "Only Codex and Claude sessions support /btw progress snapshots"
    if str(row.get("state") or "") not in {"running", "auto_wait", "ready"}:
        return (
            "A /btw progress snapshot is available only for running, auto-wait, "
            "or ready sessions"
        )
    pane_id = _command_value(row.get("pane_id") or "", 80)
    target = _command_value(row.get("tmux_target") or "", 200)
    if not pane_id.startswith("%") or not target:
        return "This session has no safe tmux pane to query"
    return ""


def _progress_prompt(marker: str) -> str:
    return (
        "/btw Summarize the overall task progress using only this conversation. "
        "Reply in the conversation's language on exactly one compact line with "
        "these fields: marker|goal|completed|current|next|blocker|END. "
        "Keep each field under 48 characters, do not use the | character inside "
        "a field, and use none when there is no blocker. The marker is "
        f"{marker}"
    )


def _clean_progress_field(value: str) -> str:
    # Full-screen TUIs can leave border glyphs between visually wrapped rows.
    # Keep the model's text while removing those display-only separators.
    value = re.sub(r"[│┃║]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return sanitize(value.strip(" \t\r\n`'\""), 240)


def _capture_progress_pane(
    base: Sequence[str], pane_id: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(base)
        + [
            "capture-pane",
            "-p",
            "-J",
            "-t",
            pane_id,
            "-S",
            "-120",
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )


def _provider_composer_state_at_start(
    base: Sequence[str], pane_id: str, provider: str
) -> str:
    """Return ``empty`` or ``draft`` when the cursor is at the composer start.

    The provider prompt glyph prevents a Home key on a wrapped continuation
    line from being mistaken for the beginning of a multiline composer.
    """
    cursor = subprocess.run(
        list(base)
        + [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{cursor_x}|#{cursor_y}",
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    try:
        cursor_x, cursor_y = (
            int(value) for value in cursor.stdout.strip().split("|", 1)
        )
    except (AttributeError, TypeError, ValueError):
        return ""
    if cursor.returncode != 0 or cursor_x != 2 or cursor_y < 0:
        return ""

    capture = subprocess.run(
        list(base) + ["capture-pane", "-e", "-p", "-t", pane_id],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    lines = capture.stdout.splitlines() if capture.returncode == 0 else []
    if cursor_y >= len(lines):
        return ""
    line = lines[cursor_y]
    plain = ANSI_RE.sub("", line).replace("\u00a0", " ")
    if provider == "codex":
        if not plain.startswith("› "):
            return ""
        return "empty" if "\x1b[2m" in line else "draft"
    if provider == "claude":
        if not plain.startswith("❯"):
            return ""
        return "empty" if plain.strip() == "❯" else "draft"
    return ""


def _progress_prompt_is_draft(capture: str, prompt: str) -> bool:
    """Return whether the exact probe prompt is still visible in the composer."""
    plain = ANSI_RE.sub("", capture)
    # Provider TUIs insert display-only newlines (and sometimes indentation) at
    # visual wrap boundaries even when tmux capture-pane uses -J.
    compact_capture = re.sub(r"\s+", "", plain)
    compact_prompt = re.sub(r"\s+", "", prompt)
    return any(prefix + compact_prompt in compact_capture for prefix in ("›", ">"))


def _clear_progress_prompt_draft(
    base: Sequence[str], pane_id: str, prompt: str
) -> None:
    """Remove only a verified probe draft without interrupting the active turn."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        capture = _capture_progress_pane(base, pane_id)
        if capture.returncode != 0 or not _progress_prompt_is_draft(
            capture.stdout, prompt
        ):
            return
        subprocess.run(
            list(base)
            + [
                "send-keys",
                "-t",
                pane_id,
                "-N",
                str(len(prompt)),
                "BSpace",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )


def parse_progress_capture(
    text: str, marker: str, provider: str
) -> ProgressSummary | None:
    """Parse the marked, pipe-delimited /btw answer from a tmux capture."""
    if not text or not marker:
        return None
    separator = r"\s*\|\s*"
    pattern = re.compile(
        re.escape(marker)
        + separator
        + r"(.*?)"
        + separator
        + r"(.*?)"
        + separator
        + r"(.*?)"
        + separator
        + r"(.*?)"
        + separator
        + r"(.*?)"
        + separator
        + r"END\b",
        re.DOTALL,
    )
    matches = list(pattern.finditer(ANSI_RE.sub("", text)))
    if not matches:
        return None
    goal, done, current, next_step, blocker = (
        _clean_progress_field(value) for value in matches[-1].groups()
    )
    if blocker.casefold() in {
        "none",
        "no",
        "n/a",
        "na",
        "无",
        "无阻塞",
        "没有",
        "—",
        "-",
    }:
        blocker = ""
    if not any((goal, done, current, next_step, blocker)):
        return None
    return ProgressSummary(
        goal=goal,
        done=done,
        current=current,
        next=next_step,
        blocker=blocker,
        provider=provider,
        captured_at=time.time(),
    )


def probe_session_progress(
    row: Mapping[str, Any], timeout: float = 30.0, poll_seconds: float = 0.25
) -> ProgressProbeResult:
    """Ask an eligible hidden Codex/Claude pane for a progress summary.

    Running and auto-wait panes are eligible. For ready panes and panes active
    in another tmux client, the cursor is first moved to a verified provider
    composer start. An existing single-line draft becomes part of the temporary
    question. The saved pane identity is validated. The provider answer is
    captured from its temporary side UI and is never retained in the monitor
    database or provider transcript.
    """
    session_key = str(row.get("session_key") or "")
    provider = str(row.get("provider") or "")
    unavailable = progress_probe_availability(row)
    if unavailable:
        return ProgressProbeResult(session_key=session_key, error=unavailable)

    pane_id = _command_value(row.get("pane_id") or "", 80)
    target = _command_value(row.get("tmux_target") or "", 200)
    socket_path = _command_value(row.get("tmux_socket") or "", 1000)
    base = _tmux_base(socket_path)
    try:
        check = subprocess.run(
            base
            + [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{session_name}:#{window_index}.#{pane_index}|#{pane_dead}|#{pane_in_mode}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        fields = check.stdout.strip().split("|") if check.returncode == 0 else []
        if len(fields) != 3 or fields[0] != target or fields[1] != "0":
            return ProgressProbeResult(
                session_key=session_key,
                error="The tmux location is stale; refresh and try again",
            )
        if fields[2] != "0":
            return ProgressProbeResult(
                session_key=session_key,
                error="The target pane is in tmux copy mode; leave it before querying",
            )

        clients = subprocess.run(
            base + ["list-clients", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if clients.returncode != 0:
            return ProgressProbeResult(
                session_key=session_key,
                error="Unable to verify whether the target pane is being viewed",
            )
        active = pane_id in {line.strip() for line in clients.stdout.splitlines()}
        state = str(row.get("state") or "")
        composer_state = ""
        if active or state == "ready":
            position = subprocess.run(
                base + ["send-keys", "-t", pane_id, "Home"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if position.returncode == 0:
                composer_state = _provider_composer_state_at_start(
                    base, pane_id, provider
                )
            if not composer_state:
                subject = "active pane" if active else "ready session"
                return ProgressProbeResult(
                    session_key=session_key,
                    error=f"The {subject}'s provider composer could not be positioned safely",
                )

        marker = "AWP" + secrets.token_hex(6).upper()
        prompt = _progress_prompt(marker)
        if composer_state == "draft":
            prompt += " "
        deadline = time.monotonic() + max(1.0, timeout)
        send = subprocess.run(
            base
            + [
                "send-keys",
                "-t",
                pane_id,
                "-l",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if send.returncode != 0:
            return ProgressProbeResult(
                session_key=session_key,
                error=sanitize(send.stderr, 200) or "Unable to send /btw to the pane",
            )

        # Codex can drop Enter when tmux sends a long literal prompt and the key
        # in the same input burst. Wait until the TUI has rendered this probe's
        # unique marker before sending Enter as a separate terminal event.
        rendered = False
        render_deadline = min(deadline, time.monotonic() + 3.0)
        while time.monotonic() < render_deadline:
            capture = _capture_progress_pane(base, pane_id)
            if capture.returncode != 0:
                return ProgressProbeResult(
                    session_key=session_key,
                    error="The target pane disappeared while preparing /btw",
                )
            if marker in ANSI_RE.sub("", capture.stdout):
                rendered = True
                break
            time.sleep(max(0.05, min(0.1, poll_seconds)))
        if not rendered:
            return ProgressProbeResult(
                session_key=session_key,
                error="The /btw prompt did not appear in the target pane",
            )

        # Give the provider one terminal event cycle after its rendered frame.
        time.sleep(max(0.05, min(0.1, poll_seconds)))
        submit = subprocess.run(
            base + ["send-keys", "-t", pane_id, "Enter"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if submit.returncode != 0:
            _clear_progress_prompt_draft(base, pane_id, prompt)
            return ProgressProbeResult(
                session_key=session_key,
                error=sanitize(submit.stderr, 200) or "Unable to submit /btw",
            )

        while time.monotonic() < deadline:
            capture = _capture_progress_pane(base, pane_id)
            if capture.returncode != 0:
                return ProgressProbeResult(
                    session_key=session_key,
                    error="The target pane disappeared while waiting for /btw",
                )
            summary = parse_progress_capture(capture.stdout, marker, provider)
            if summary is not None:
                # Current Codex uses a persistent side thread and documents
                # Ctrl+C to return to the main thread. Claude uses Space to
                # dismiss its temporary answer. The marker proves the answer is
                # present before either provider-specific key is sent.
                dismiss_key = "C-c" if provider == "codex" else "Space"
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        base + ["send-keys", "-t", pane_id, dismiss_key],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False,
                    )
                return ProgressProbeResult(
                    session_key=session_key, summary=summary
                )
            time.sleep(max(0.05, poll_seconds))
    except (OSError, subprocess.SubprocessError) as exc:
        return ProgressProbeResult(
            session_key=session_key,
            error=sanitize(exc, 200) or "Unable to query session progress",
        )
    _clear_progress_prompt_draft(base, pane_id, prompt)
    return ProgressProbeResult(
        session_key=session_key,
        error="No /btw progress reply arrived before the timeout",
    )


class ProgressProbeManager:
    """Run bounded progress probes without blocking dashboard rendering."""

    def __init__(self, max_workers: int = 3):
        self.jobs: queue.Queue[tuple[dict[str, Any], bool] | None] = queue.Queue()
        self.results: queue.SimpleQueue[ProgressProbeResult] = queue.SimpleQueue()
        self.closed = threading.Event()
        self.workers: list[threading.Thread] = []
        for index in range(max(1, max_workers)):
            worker = threading.Thread(
                target=self._worker,
                name=f"agent-watch-progress-{index + 1}",
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)

    def _worker(self) -> None:
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            row, bulk = job
            try:
                result = probe_session_progress(row)
            except Exception as exc:  # keep one malformed session from killing a worker
                result = ProgressProbeResult(
                    session_key=str(row.get("session_key") or ""),
                    error=sanitize(exc, 200) or "Unexpected progress query failure",
                )
            self.results.put(dataclasses.replace(result, bulk=bulk))

    def submit(self, row: Mapping[str, Any], bulk: bool = False) -> None:
        if not self.closed.is_set():
            self.jobs.put((dict(row), bulk))

    def poll(self) -> list[ProgressProbeResult]:
        results: list[ProgressProbeResult] = []
        while True:
            try:
                results.append(self.results.get_nowait())
            except queue.Empty:
                return results

    def close(self) -> None:
        self.closed.set()
        for _worker in self.workers:
            self.jobs.put(None)


def _current_tmux_socket() -> str:
    value = os.environ.get("TMUX", "")
    return value.rsplit(",", 2)[0] if value.count(",") >= 2 else ""


def switch_to_session(row: Mapping[str, Any]) -> tuple[bool, str]:
    if str(row.get("state") or "") == "exited":
        return (
            False,
            "This process has exited; the tmux field shows only its last location",
        )
    target = _command_value(row.get("tmux_target") or "", 200)
    pane_id = _command_value(row.get("pane_id") or "", 80)
    target_socket = _command_value(row.get("tmux_socket") or "", 1000)
    if not target:
        return False, "This session has no tmux location to open"
    locator = pane_id if pane_id.startswith("%") else target
    try:
        source_socket = _current_tmux_socket() if os.environ.get("TMUX") else ""
        effective_socket = target_socket or source_socket
        base = _tmux_base(effective_socket)
        # Pane ids are more stable than indexes; also verify the current tuple so
        # stale rows fail closed when the pane moved or disappeared.
        if pane_id.startswith("%"):
            check = subprocess.run(
                base
                + [
                    "display-message",
                    "-p",
                    "-t",
                    pane_id,
                    "#{session_name}:#{window_index}.#{pane_index}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if check.returncode != 0 or sanitize(check.stdout, 200) != target:
                return (
                    False,
                    "The tmux location is stale; wait for a monitor refresh and try again",
                )
        if os.environ.get("TMUX"):
            if target_socket and os.path.realpath(target_socket) != os.path.realpath(
                source_socket
            ):
                command = shlex.join(
                    ["tmux", "-S", target_socket, "attach", "-t", locator]
                )
                return (
                    False,
                    f"Target is on another tmux server; run this in a terminal: {command}",
                )
            source_base = _tmux_base(source_socket)
            source_pane = _command_value(os.environ.get("TMUX_PANE", ""), 80)
            clients = subprocess.run(
                source_base + ["list-clients", "-F", "#{client_tty}|#{pane_id}"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            candidates: list[str] = []
            if clients.returncode == 0 and source_pane:
                for line in clients.stdout.splitlines():
                    tty_name, separator, active_pane = line.partition("|")
                    if (
                        separator
                        and active_pane == source_pane
                        and _command_value(tty_name, 300)
                    ):
                        candidates.append(tty_name)
            if len(candidates) != 1:
                return (
                    False,
                    "Multiple clients are viewing this dashboard; cannot choose which one to switch",
                )
            run = subprocess.run(
                source_base + ["switch-client", "-c", candidates[0], "-t", locator],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if run.returncode == 0:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        source_base
                        + [
                            "display-message",
                            "-c",
                            candidates[0],
                            "-d",
                            "5000",
                            "Agent Watch: press tmux prefix + L to return",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False,
                    )
        else:
            run = subprocess.run(base + ["attach-session", "-t", locator], check=False)
        if run.returncode == 0:
            return True, ""
        return False, sanitize(
            getattr(run, "stderr", "") or "Unable to open tmux session", 200
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, sanitize(exc, 200)


def dashboard_pane_visible() -> bool:
    """Best-effort check used to pause animation while the tmux window is hidden."""
    pane = os.environ.get("TMUX_PANE", "")
    if not os.environ.get("TMUX") or not pane:
        return True
    try:
        run = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_attached} #{window_active} #{pane_active}",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        if run.returncode != 0:
            return True
        values = run.stdout.strip().split()
        return len(values) == 3 and all(value not in {"0", ""} for value in values)
    except (OSError, subprocess.SubprocessError):
        return True


def handle_key(view: DashboardView, key: str, text: str, page_size: int) -> str:
    """Mutate view and return a dashboard action name, or an empty string."""
    if view.searching:
        if key == "escape":
            view.query = ""
            view.searching = False
            view._ensure_selection()
        elif key == "enter":
            view.searching = False
        elif key == "backspace":
            view.query = view.query[:-1]
            view._ensure_selection()
        elif key == "text" and text.isprintable():
            view.query += text
            view._ensure_selection()
        return ""

    if view.show_help:
        if key in {"escape", "enter"} or (key == "text" and text in {"?", "q"}):
            view.show_help = False
        return ""

    if view.history_mode and key in {"escape", "backspace"}:
        if view.query:
            view.query = ""
            view._ensure_selection()
        else:
            view.close_exited_sessions()
        return ""
    if key == "quit" or (key == "text" and text == "q"):
        return "quit"
    if key == "up" or (key == "text" and text == "k"):
        view.move(-1)
    elif key == "down" or (key == "text" and text == "j"):
        view.move(1)
    elif key == "page_up":
        view.move(-max(1, page_size))
    elif key == "page_down":
        view.move(max(1, page_size))
    elif key == "home" or (key == "text" and text == "g"):
        view.jump(0)
    elif key == "end" or (key == "text" and text == "G"):
        view.jump(max(0, len(view.rows) - 1))
    elif key == "enter":
        if is_exited_summary(view.selected):
            view.open_exited_sessions()
        elif view.history_mode:
            return "resume"
        else:
            return "attach"
    elif key == "text" and text == "/":
        view.searching = True
    elif key == "text" and text == "f":
        view.cycle_filter()
    elif key == "text" and text == "b" and not view.history_mode:
        return "progress"
    elif key == "text" and text == "B" and not view.history_mode:
        return "progress_all"
    elif key == "text" and text == "p":
        view.conversation_preview = not view.conversation_preview
        if not view.conversation_preview:
            view.set_context("", {})
            view.set_flash("Session preview hidden")
        else:
            view.set_flash("Session preview shown; content is read locally only")
    elif key == "text" and text == "r":
        return "refresh"
    elif key == "text" and text == "?":
        view.show_help = True
    elif key == "escape" and view.query:
        view.query = ""
        view._ensure_selection()
    return ""


def run_dashboard(
    state_dir: pathlib.Path,
    refresh_seconds: float = 1.0,
    activity_stale_seconds: float = 600.0,
    conversation_preview: bool = False,
    heartbeat_max_age: float = 60.0,
) -> int:
    if not RICH_AVAILABLE:
        print("agent-watch ui requires the 'rich' Python package", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("agent-watch ui requires an interactive TTY", file=sys.stderr)
        return 2

    console = Console(
        highlight=False,
        soft_wrap=False,
        _environ=rich_console_environment(os.environ),
    )
    snapshot = load_snapshot(
        state_dir,
        heartbeat_max_age=heartbeat_max_age,
        activity_stale_seconds=activity_stale_seconds,
    )
    view = DashboardView(snapshot, conversation_preview=conversation_preview)
    context_loader = ConversationPreviewLoader(refresh_seconds=1.0)
    progress_manager = ProgressProbeManager(max_workers=3)
    quit_requested = False
    signal_state: dict[str, int | bool] = {"terminate": 0, "suspend": False}

    def handle_signal(signum: int, _frame: Any) -> None:
        if hasattr(signal, "SIGTSTP") and signum == signal.SIGTSTP:
            signal_state["suspend"] = True
        else:
            signal_state["terminate"] = signum

    handled_signals = [signal.SIGINT, signal.SIGTERM, signal.SIGQUIT]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    if hasattr(signal, "SIGTSTP"):
        handled_signals.append(signal.SIGTSTP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}
    for signum in handled_signals:
        signal.signal(signum, handle_signal)

    try:
        while not quit_requested and not signal_state["terminate"]:
            attach_row: dict[str, Any] | None = None
            resume_row: dict[str, Any] | None = None
            force_refresh = False
            last_load = 0.0
            last_render = 0.0
            last_frame = -1
            last_size = (0, 0)
            visible = True
            last_visibility_check = 0.0
            with RawTerminal() as terminal:
                with Live(
                    render_dashboard(view, console.width, console.height),
                    console=console,
                    screen=True,
                    auto_refresh=False,
                    transient=False,
                    redirect_stdout=False,
                    redirect_stderr=False,
                ) as live:
                    while True:
                        if signal_state["terminate"] or signal_state["suspend"]:
                            break
                        now = time.monotonic()
                        if now - last_visibility_check >= 1.0:
                            visible = dashboard_pane_visible()
                            last_visibility_check = now
                        data_changed = False
                        if force_refresh or now - last_load >= refresh_seconds:
                            view.update(
                                load_snapshot(
                                    state_dir,
                                    heartbeat_max_age=heartbeat_max_age,
                                    activity_stale_seconds=activity_stale_seconds,
                                )
                            )
                            last_load = now
                            force_refresh = False
                            data_changed = True
                        for result in progress_manager.poll():
                            view.finish_progress(result)
                            data_changed = True
                            if result.error and (
                                not result.bulk
                                or result.session_key == view.selected_key
                            ):
                                view.set_flash(result.error, seconds=5.0)
                        if (
                            visible
                            and console.width >= 100
                            and view.conversation_preview
                        ):
                            selected = view.selected
                            selected_key = (
                                str(selected.get("session_key") or "")
                                if selected
                                else ""
                            )
                            if view.set_context(
                                selected_key, context_loader.load(selected)
                            ):
                                data_changed = True
                        elif (
                            console.width < 100 or not view.conversation_preview
                        ) and view.context_session_key:
                            if view.set_context("", {}):
                                data_changed = True
                        frame_rate = 1
                        frame = int(now * frame_rate) % len(SPINNER)
                        size = (console.width, console.height)
                        should_render = (
                            data_changed
                            or size != last_size
                            or last_render == 0.0
                            or (visible and frame != last_frame)
                        )
                        if should_render:
                            view.spinner_index = frame
                            live.update(
                                render_dashboard(view, size[0], size[1]),
                                refresh=True,
                            )
                            last_render = now
                            last_frame = frame
                            last_size = size
                        key, text = terminal.read_key(0.24 if visible else 0.5)
                        if signal_state["terminate"] or signal_state["suspend"]:
                            break
                        if not key:
                            continue
                        action = handle_key(
                            view,
                            key,
                            text,
                            page_size=max(2, console.height - 12),
                        )
                        last_render = 0.0
                        if action == "quit":
                            quit_requested = True
                            break
                        if action == "attach":
                            attach_row = view.selected
                            if attach_row is None:
                                view.set_flash("No session selected")
                            else:
                                break
                        if action == "resume":
                            resume_row = view.selected
                            if resume_row is None:
                                view.set_flash("No exited session selected")
                            else:
                                break
                        if action == "refresh":
                            force_refresh = True
                        if action == "progress":
                            progress_row = view.selected
                            unavailable = progress_probe_availability(progress_row)
                            if unavailable:
                                view.set_flash(unavailable, seconds=5.0)
                            elif progress_row is not None:
                                session_key = str(
                                    progress_row.get("session_key") or ""
                                )
                                if view.begin_progress(session_key):
                                    progress_manager.submit(progress_row)
                                else:
                                    view.set_flash(
                                        "A progress query is already running"
                                    )
                        if action == "progress_all":
                            submitted = 0
                            for progress_row in visible_sessions(view.snapshot):
                                if progress_probe_availability(progress_row):
                                    continue
                                session_key = str(
                                    progress_row.get("session_key") or ""
                                )
                                if view.begin_progress(session_key):
                                    progress_manager.submit(progress_row, bulk=True)
                                    submitted += 1
                            if submitted:
                                view.set_flash(
                                    f"Asking {submitted} eligible session"
                                    f"{'s' if submitted != 1 else ''} via /btw"
                                )
                            else:
                                view.set_flash(
                                    "No eligible sessions to query"
                                )
            if signal_state["terminate"]:
                break
            if signal_state["suspend"]:
                signal_state["suspend"] = False
                # SIGTSTP may be discarded for an orphaned process group. The
                # original TSTP has already been handled and the terminal is
                # restored, so SIGSTOP reliably hands control back to the shell.
                os.kill(os.getpid(), signal.SIGSTOP)
                continue
            if quit_requested:
                break
            if attach_row is not None:
                ok, message = switch_to_session(attach_row)
                if not ok:
                    view.set_flash(message or "Unable to open tmux session")
            if resume_row is not None:
                ok, message = resume_in_new_tmux(resume_row, state_dir=state_dir)
                if not ok:
                    view.set_flash(message or "Unable to resume this session")
        signum = int(signal_state["terminate"] or 0)
        return 128 + signum if signum else 0
    finally:
        progress_manager.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def print_static(snapshot: DashboardSnapshot, console: Console | None = None) -> None:
    if not RICH_AVAILABLE:
        raise RuntimeError("rich is unavailable")
    console = console or Console(highlight=False)
    console.print(render_static(snapshot, console.width))
