"""Safely resume exited agent sessions in a fresh tmux session.

This module deliberately treats the tmux coordinates stored on a database row as
historical data.  A resumed agent always gets its own, deterministically named
session on the tmux server that is hosting the dashboard.  That makes retries
idempotent and avoids sending a user back to a stale pane.

The public helpers accept mapping-like rows (including ``sqlite3.Row``).  They
validate all values that can reach a command, invoke subprocesses without a
shell, and put the one command string tmux itself requires through
``shlex.join``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any


__all__ = [
    "resume_availability",
    "resume_command",
    "resume_tmux_name",
    "resume_in_new_tmux",
]


_PROVIDERS = frozenset({"codex", "claude"})
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_TEMPORARY_ID_PREFIXES = ("pid-", "pane-", "hook-")
_PANE_ID_RE = re.compile(r"%[0-9]+\Z")
_TMUX_NAME_PREFIX = "agent-watch-resume"
_COMMAND_TIMEOUT = 3.0
_CONTEXT_MAX_LINE = 1024 * 1024

# ``attach-session`` intentionally owns the terminal until the user detaches.
# It still receives a finite timeout, as every subprocess in this module must;
# seven days is a guard against a permanently wedged child, not a UI timeout.
_ATTACH_TIMEOUT = 7 * 24 * 60 * 60.0


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    """Read a value from a mapping or a mapping-like sqlite row."""

    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        getter = getattr(row, "get", None)
        if callable(getter):
            return getter(key)
        return None


def _clean_text(value: Any, *, maximum: int) -> str:
    """Return a bounded printable string, or an empty string when unsafe."""

    if not isinstance(value, str) or not value or len(value) > maximum:
        return ""
    if any(not character.isprintable() for character in value):
        return ""
    return value


def _absolute_executable(name: str) -> str | None:
    """Resolve ``PATH`` entries before tmux changes to the saved cwd."""

    candidate = shutil.which(name)
    if not candidate:
        return None
    try:
        resolved = pathlib.Path(candidate).resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            return None
        return str(resolved)
    except OSError:
        return None


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """Validate and return ``(provider, session_id, error_message)``."""

    provider = _clean_text(_row_value(row, "provider"), maximum=16)
    if provider not in _PROVIDERS:
        return "", "", "Only Codex and Claude sessions can be resumed"

    session_id = _clean_text(_row_value(row, "session_id"), maximum=200)
    if not session_id:
        return "", "", "This session has no stable session ID"
    if session_id.casefold().startswith(_TEMPORARY_ID_PREFIXES):
        return "", "", "Temporary pid-/pane-/hook- session IDs cannot be resumed"
    if _SESSION_ID_RE.fullmatch(session_id) is None:
        return "", "", "The saved session ID contains unsupported characters"
    return provider, session_id, ""


def _cwd(row: Mapping[str, Any]) -> tuple[str, str]:
    """Validate the saved working directory without rewriting it."""

    cwd = _clean_text(_row_value(row, "cwd"), maximum=4096)
    if not cwd:
        return "", "This session has no valid saved working directory"
    try:
        is_directory = os.path.isdir(cwd)
    except OSError:
        is_directory = False
    if not is_directory:
        return "", "The saved working directory is no longer available"
    return cwd, ""


def _safe_history_file(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
    """Return an owned regular history file contained by *root*."""

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


def _codex_artifact_matches(path: pathlib.Path, session_id: str) -> bool:
    try:
        with path.open("rb") as handle:
            raw = handle.readline(_CONTEXT_MAX_LINE + 1)
        if len(raw) > _CONTEXT_MAX_LINE:
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


def _claude_artifact_matches(path: pathlib.Path, session_id: str) -> bool:
    try:
        consumed = 0
        with path.open("rb") as handle:
            for _index in range(32):
                raw = handle.readline(_CONTEXT_MAX_LINE + 1)
                if not raw or len(raw) > _CONTEXT_MAX_LINE:
                    break
                consumed += len(raw)
                if consumed > _CONTEXT_MAX_LINE:
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


def _session_artifact_available(
    provider: str, session_id: str, home: pathlib.Path | None = None
) -> bool:
    """Check that the provider still has the conversation needed to resume."""

    user_home = (home or pathlib.Path.home()).resolve()
    if provider == "codex":
        root = (user_home / ".codex" / "sessions").resolve()
        pattern = f"*/*/*/rollout-*{session_id}.jsonl"
        matcher = _codex_artifact_matches
    else:
        root = (user_home / ".claude" / "projects").resolve()
        pattern = f"*/{session_id}.jsonl"
        matcher = _claude_artifact_matches
    try:
        candidates = list(root.glob(pattern))
    except OSError:
        return False
    for candidate in candidates:
        safe = _safe_history_file(candidate, root)
        if safe is not None and matcher(safe, session_id):
            return True
    return False


def resume_availability(
    row: Mapping[str, Any], *, home: pathlib.Path | None = None
) -> tuple[bool, str]:
    """Report whether *row* has everything required for a safe resume.

    Availability requires a supported provider, a stable command-safe session
    ID, an owned conversation artifact, an existing directory in ``cwd``, and
    tmux and the provider CLI in ``PATH``. The returned reason is empty on
    success and suitable for display to the user on failure. This function does
    not run any commands.
    """

    state = _clean_text(_row_value(row, "state"), maximum=32)
    if state != "exited":
        return False, "Only exited sessions can be resumed"
    provider, session_id, error = _identity(row)
    if error:
        return False, error
    _saved_cwd, error = _cwd(row)
    if error:
        return False, error
    if not _session_artifact_available(provider, session_id, home):
        return False, "Conversation data is no longer available"
    if _absolute_executable("tmux") is None:
        return False, "tmux is not available in PATH"
    if _absolute_executable(provider) is None:
        label = "Codex" if provider == "codex" else "Claude"
        return False, f"{label} CLI is not available in PATH"
    return True, ""


def resume_command(row: Mapping[str, Any]) -> list[str]:
    """Build the provider resume argv for *row*.

    Codex uses ``codex resume SESSION_ID`` and Claude uses
    ``claude --resume SESSION_ID``.  A :class:`ValueError` is raised for an
    unsupported provider, a temporary ID, or any ID that is unsafe to pass as
    an argument.  Executable and cwd availability are intentionally left to
    :func:`resume_availability`.
    """

    provider, session_id, error = _identity(row)
    if error:
        raise ValueError(error)
    if provider == "codex":
        return ["codex", "resume", session_id]
    return ["claude", "--resume", session_id]


def resume_tmux_name(row: Mapping[str, Any]) -> str:
    """Return the deterministic, tmux-safe session name for *row*.

    The name is derived only from the provider and stable session ID.  It never
    embeds a filesystem path or raw session ID, and therefore remains stable if
    display metadata changes.  Invalid identities raise :class:`ValueError`.
    """

    provider, session_id, error = _identity(row)
    if error:
        raise ValueError(error)
    digest = hashlib.sha256(
        f"{provider}\0{session_id}".encode("utf-8", errors="strict")
    ).hexdigest()[:16]
    return f"{_TMUX_NAME_PREFIX}-{provider}-{digest}"


def _tmux_context() -> tuple[list[str], bool, str]:
    """Return tmux base argv, whether the dashboard is in tmux, and an error."""

    raw = os.environ.get("TMUX", "")
    if not raw:
        return ["tmux"], False, ""
    value = _clean_text(raw, maximum=4096)
    parts = value.rsplit(",", 2) if value else []
    if (
        len(parts) != 3
        or not parts[0]
        or not os.path.isabs(parts[0])
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        return [], True, "Cannot identify the dashboard's current tmux server"
    return ["tmux", "-S", parts[0]], True, ""


def _run(
    argv: list[str],
    *,
    timeout: float = _COMMAND_TIMEOUT,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded subprocess with an argv and no intermediary shell."""

    return subprocess.run(
        argv,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _diagnostic(value: Any, fallback: str) -> str:
    """Make a subprocess diagnostic safe and compact enough for the TUI."""

    if not isinstance(value, str):
        return fallback
    value = " ".join(value.splitlines()).strip()
    value = "".join(character for character in value if character.isprintable())
    return value[:240] or fallback


def _session_is_active(provider: str, session_id: str) -> bool:
    """Detect an already-running provider session across all tmux servers."""

    try:
        from .core import (
            exact_agent_processes,
            find_main_codex_rollout,
            load_claude_session,
        )

        for process in exact_agent_processes():
            if process.provider != provider:
                continue
            if provider == "codex":
                rollout = find_main_codex_rollout(process.pid, process.start_time)
                if rollout is not None and rollout[1] == session_id:
                    return True
            else:
                session = load_claude_session(process.pid)
                if session is not None and str(session.get("sessionId") or "") == session_id:
                    return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False


def _resume_lock_path(
    row: Mapping[str, Any], state_dir: pathlib.Path | None
) -> tuple[pathlib.Path | None, str]:
    """Create a private per-session lock file used across tmux servers."""

    try:
        if state_dir is None:
            configured = os.environ.get("AGENT_WATCH_STATE_DIR", "")
            state_dir = (
                pathlib.Path(configured).expanduser()
                if configured
                else pathlib.Path.home() / ".local" / "state" / "agent-watch"
            )
        lock_dir = pathlib.Path(state_dir).expanduser().absolute() / "resume-locks"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_dir.chmod(0o700)
        name = resume_tmux_name(row).removeprefix(f"{_TMUX_NAME_PREFIX}-")
        path = lock_dir / f"{name}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return path, ""
    except (OSError, ValueError) as exc:
        return None, _diagnostic(str(exc), "Unable to create the resume lock")


def _resume_lock_busy(path: pathlib.Path) -> bool:
    """Return whether another resume process currently holds *path*."""

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def exec_with_lock(lock_path: pathlib.Path, command: list[str]) -> int:
    """Hold a private flock across ``exec`` and run *command*.

    This replaces the Linux-only ``flock(1)`` utility while retaining the same
    cross-dashboard exclusion on Linux and macOS. The descriptor must be marked
    inheritable because Python makes newly opened descriptors close-on-exec by
    default.
    """

    if not command or not os.path.isabs(command[0]):
        raise ValueError("the locked command must use an absolute executable path")
    executable = pathlib.Path(command[0])
    try:
        executable_info = executable.stat()
    except OSError as exc:
        raise ValueError("the locked command executable is unavailable") from exc
    if not stat.S_ISREG(executable_info.st_mode) or not os.access(
        executable, os.X_OK
    ):
        raise ValueError("the locked command executable is not executable")

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("the resume lock is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("the resume lock must be private and owned by this user")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        os.set_inheritable(descriptor, True)
        os.execv(command[0], command)
        return 70  # pragma: no cover - os.execv does not return on success
    finally:
        os.close(descriptor)


def _locked_command_main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 2:
        print(
            "usage: python -m agent_watch.resume LOCK_PATH COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 64
    try:
        return exec_with_lock(pathlib.Path(values[0]), values[1:])
    except (OSError, ValueError) as exc:
        print(
            _diagnostic(str(exc), "Unable to start the locked resume command"),
            file=sys.stderr,
        )
        return 70


def _resume_session_status(
    base: list[str], target: str, provider: str
) -> tuple[str, str]:
    """Return missing/live/starting/dead/conflict for a named resume session."""

    if not _session_exists(base, target):
        return "missing", ""
    result = _run(
        base
        + [
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_dead}|#{pane_dead_status}|#{pane_current_command}",
        ]
    )
    if result.returncode != 0:
        return "conflict", _diagnostic(
            result.stderr, "Cannot inspect the existing resume tmux session"
        )
    saw_dead = False
    for line in result.stdout.splitlines():
        dead, separator, remainder = line.partition("|")
        dead_status, second_separator, command = remainder.partition("|")
        if not separator or not second_separator:
            continue
        if dead == "1":
            saw_dead = True
            continue
        if command in {provider, "flock"}:
            return "live", ""
        if command in {"sh", "bash", "dash"} or command.casefold().startswith(
            "python"
        ):
            return "starting", ""
    if saw_dead:
        return "dead", "The previous resume process has exited"
    return "conflict", "The resume tmux session name is already in use"


def _kill_resume_session(base: list[str], target: str) -> None:
    _run(base + ["kill-session", "-t", target])


def _current_client(base: list[str]) -> tuple[str, str]:
    """Resolve the sole tmux client whose active pane is this dashboard pane."""

    pane_id = _clean_text(os.environ.get("TMUX_PANE", ""), maximum=32)
    if _PANE_ID_RE.fullmatch(pane_id) is None:
        return "", "Cannot identify the dashboard's current tmux pane"

    result = _run(base + ["list-clients", "-F", "#{client_tty}|#{pane_id}"])
    if result.returncode != 0:
        return "", _diagnostic(
            result.stderr, "Cannot inspect clients on the dashboard's tmux server"
        )

    candidates: list[str] = []
    for line in result.stdout.splitlines():
        client_tty, separator, active_pane = line.partition("|")
        clean_client = _clean_text(client_tty, maximum=300)
        if (
            separator
            and active_pane == pane_id
            and clean_client
            and os.path.isabs(clean_client)
        ):
            candidates.append(clean_client)
    if len(candidates) != 1:
        return (
            "",
            "Cannot choose the current tmux client unambiguously; "
            "make sure only one client is viewing Agent Watch",
        )
    return candidates[0], ""


def _session_exists(base: list[str], target: str) -> bool:
    """Return whether an exact tmux session target currently exists."""

    result = _run(base + ["has-session", "-t", target])
    return result.returncode == 0


def resume_in_new_tmux(
    row: Mapping[str, Any],
    *,
    state_dir: pathlib.Path | None = None,
    home: pathlib.Path | None = None,
) -> tuple[bool, str]:
    """Resume *row* in a dedicated session and enter it.

    When Agent Watch itself is inside tmux, the current server is parsed from
    ``TMUX`` and the exact client displaying ``TMUX_PANE`` is resolved before
    any session is created.  More than one matching client fails closed.  When
    outside tmux, the default server is used and the resulting session is
    attached directly.

    A live session with the deterministic name is reused, while dead panes are
    removed. A per-session advisory lock held by the provider process prevents
    two dashboards on different tmux servers from launching the same
    conversation. Otherwise tmux receives the saved cwd and a shell-command
    produced only with :func:`shlex.join`. Historical ``tmux_socket``,
    ``tmux_target``, and ``pane_id`` row fields are never read.
    """

    available, reason = resume_availability(row, home=home)
    if not available:
        return False, reason

    try:
        provider, session_id, error = _identity(row)
        if error:
            return False, error
        if _session_is_active(provider, session_id):
            return (
                False,
                "This session is already running; refresh Agent Watch to open it",
            )
        cwd, error = _cwd(row)
        if error:
            return False, error
        command = resume_command(row)
        executable = _absolute_executable(command[0])
        if executable is None:
            return False, f"{command[0]} is no longer available in PATH"
        command[0] = executable
        lock_path, error = _resume_lock_path(row, state_dir)
        if lock_path is None:
            return False, error
        lock_busy = _resume_lock_busy(lock_path)
        locked_command = [
            sys.executable,
            "-m",
            "agent_watch.resume",
            str(lock_path),
            *command,
        ]
        name = resume_tmux_name(row)
        base, inside_tmux, error = _tmux_context()
        if error:
            return False, error

        client = ""
        if inside_tmux:
            # Resolve before creation so an ambiguous dashboard never leaves an
            # unexpected detached session behind.
            client, error = _current_client(base)
            if error:
                return False, error

        target = f"={name}"
        session_status, status_reason = _resume_session_status(
            base, target, provider
        )
        if lock_busy:
            if session_status not in {"live", "starting"}:
                return (
                    False,
                    "This session is already being resumed in another tmux session",
                )
        elif session_status == "dead":
            _kill_resume_session(base, target)
            session_status = "missing"
        elif session_status in {"live", "starting", "conflict"}:
            return False, status_reason or "The resume tmux session is already in use"

        if session_status == "missing":
            create = _run(
                base
                + [
                    "new-session",
                    "-d",
                    "-s",
                    name,
                    "-c",
                    cwd,
                    shlex.join(locked_command),
                ]
            )
            if create.returncode != 0:
                session_status, status_reason = _resume_session_status(
                    base, target, provider
                )
                if session_status not in {"live", "starting"}:
                    return False, _diagnostic(
                        create.stderr, "Unable to create the resume tmux session"
                    )

            # Catch missing artifacts, lock contention, and other provider
            # failures before claiming that resume succeeded. A shell or the
            # small Python lock wrapper may be visible before it execs the
            # provider.
            for attempt in range(4):
                session_status, status_reason = _resume_session_status(
                    base, target, provider
                )
                if session_status == "live":
                    if _resume_lock_busy(lock_path):
                        break
                    if attempt < 3:
                        time.sleep(0.05)
                        continue
                    return False, "The provider did not acquire the resume lock"
                if session_status == "starting" and attempt < 3:
                    time.sleep(0.05)
                    continue
                if session_status in {"missing", "dead"}:
                    if session_status == "dead":
                        _kill_resume_session(base, target)
                    return False, status_reason or "The provider could not resume this session"
                if session_status == "conflict":
                    return False, status_reason

        if inside_tmux:
            enter = _run(base + ["switch-client", "-c", client, "-t", target])
        else:
            enter = _run(
                base + ["attach-session", "-t", target],
                timeout=_ATTACH_TIMEOUT,
                capture_output=False,
            )
        if enter.returncode == 0:
            if inside_tmux:
                # Best effort only: the resume succeeded even if this hint is
                # unsupported by an older tmux build.
                _run(
                    base
                    + [
                        "display-message",
                        "-c",
                        client,
                        "-d",
                        "5000",
                        "Agent Watch: press tmux prefix + L to return",
                    ]
                )
            return True, ""
        return False, _diagnostic(
            enter.stderr, "Unable to enter the resume tmux session"
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, _diagnostic(str(exc), "Unable to resume this session")


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(_locked_command_main())
