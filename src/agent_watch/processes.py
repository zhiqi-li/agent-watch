"""Cross-platform process discovery for Agent Watch.

Linux keeps the original direct ``/proc`` implementation.  macOS uses psutil,
which exposes the same-user process metadata that Agent Watch needs without
depending on private Darwin APIs or parsing localized command output.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import stat
import sys
import time
from typing import Any, Iterable, Sequence


@dataclasses.dataclass(slots=True)
class ProcessInfo:
    pid: int
    provider: str
    start_time: str
    state: str
    cwd: str
    tty: str
    tmux_socket: str = ""
    tmux_pane: str = ""


def _clean_environment_value(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        return ""
    if any(not character.isprintable() for character in value):
        return ""
    return value


def _tmux_location(environment: dict[str, str]) -> tuple[str, str]:
    value = _clean_environment_value(environment.get("TMUX", ""), 4096)
    socket_path = value.rsplit(",", 2)[0] if value.count(",") >= 2 else ""
    pane_id = _clean_environment_value(environment.get("TMUX_PANE", ""), 40)
    return socket_path, pane_id


def _linux_process_details(
    pid: int, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> tuple[str, str] | None:
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except (OSError, UnicodeError):
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 2 :].split()
    if len(fields) < 20:
        return None
    return fields[0], fields[19]  # process state, kernel starttime ticks


def _linux_process_tty(
    pid: int, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> str:
    for descriptor in (0, 1, 2):
        try:
            target = os.readlink(proc_root / str(pid) / "fd" / str(descriptor))
        except OSError:
            continue
        if target.startswith("/dev/pts/") or target.startswith("/dev/tty"):
            return target
    return ""


def _linux_process_environment(
    pid: int, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> dict[str, str]:
    try:
        data = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for item in data.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        if key in {b"TMUX", b"TMUX_PANE"}:
            values[key.decode()] = value.decode("utf-8", "replace")
    return values


def _linux_process_command_line(
    pid: int, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> tuple[str, ...]:
    try:
        data = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(
        item.decode("utf-8", "replace") for item in data.split(b"\0") if item
    )


def _is_claude_control_process(
    provider: str, command_line: Sequence[str]
) -> bool:
    """Return whether a process is Claude infrastructure, not a user session."""

    if provider != "claude" or len(command_line) < 2:
        return False
    subcommand = command_line[1]
    return subcommand in {"bg-pty-host", "bg-spare"} or (
        subcommand == "daemon"
        and len(command_line) >= 3
        and command_line[2] == "run"
    )


def _linux_agent_processes(
    proc_root: pathlib.Path = pathlib.Path("/proc"), uid: int | None = None
) -> list[ProcessInfo]:
    result: list[ProcessInfo] = []
    owner = os.getuid() if uid is None else uid
    try:
        entries: Iterable[pathlib.Path] = list(proc_root.iterdir())
    except OSError as exc:
        raise RuntimeError(
            f"cannot inspect Linux process table at {proc_root}: {exc}"
        ) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != owner:
                continue
            provider = (entry / "comm").read_text().strip()
        except (OSError, UnicodeError):
            continue
        if provider not in {"codex", "claude"}:
            continue
        details = _linux_process_details(pid, proc_root)
        if details is None or details[0] == "Z":
            continue
        if _is_claude_control_process(
            provider, _linux_process_command_line(pid, proc_root)
        ):
            continue
        state, start_time = details
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        tmux_socket, tmux_pane = _tmux_location(
            _linux_process_environment(pid, proc_root)
        )
        result.append(
            ProcessInfo(
                pid=pid,
                provider=provider,
                start_time=start_time,
                state=state,
                cwd=cwd,
                tty=_linux_process_tty(pid, proc_root),
                tmux_socket=tmux_socket,
                tmux_pane=tmux_pane,
            )
        )
    return result


def _linux_open_process_files(
    pid: int, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    descriptor_dir = proc_root / str(pid) / "fd"
    try:
        descriptors = descriptor_dir.iterdir()
    except OSError:
        return []
    for descriptor in descriptors:
        try:
            target = pathlib.Path(os.readlink(descriptor))
            if stat.S_ISREG(target.stat().st_mode):
                paths.append(target)
        except OSError:
            continue
    return paths


def _load_psutil() -> Any:
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - packaging installs the marker dependency
        raise RuntimeError(
            "macOS process discovery requires psutil; reinstall agent-watch"
        ) from exc
    return psutil


def _psutil_errors(psutil_module: Any) -> tuple[type[BaseException], ...]:
    return tuple(
        value
        for value in (
            getattr(psutil_module, "NoSuchProcess", None),
            getattr(psutil_module, "AccessDenied", None),
            getattr(psutil_module, "ZombieProcess", None),
        )
        if isinstance(value, type) and issubclass(value, BaseException)
    )


def _darwin_process_details(pid: int, psutil_module: Any) -> tuple[str, str] | None:
    try:
        process = psutil_module.Process(pid)
        return str(process.status()), time.ctime(process.create_time())
    except _psutil_errors(psutil_module):
        return None


def _darwin_open_process_files(pid: int, psutil_module: Any) -> list[pathlib.Path]:
    try:
        return [
            pathlib.Path(item.path)
            for item in psutil_module.Process(pid).open_files()
            if isinstance(getattr(item, "path", None), str)
        ]
    except _psutil_errors(psutil_module):
        return []


def _darwin_agent_processes(
    psutil_module: Any, uid: int | None = None
) -> list[ProcessInfo]:
    result: list[ProcessInfo] = []
    owner = os.getuid() if uid is None else uid
    errors = _psutil_errors(psutil_module)
    attributes = [
        "pid",
        "uids",
        "name",
        "status",
        "create_time",
        "cwd",
        "terminal",
        "cmdline",
    ]
    for process in psutil_module.process_iter(attributes):
        try:
            info = process.info
            uids = info.get("uids")
            if uids is None or int(uids.real) != owner:
                continue
            provider = str(info.get("name") or "")
            if provider not in {"codex", "claude"}:
                continue
            command_line = info.get("cmdline")
            if not isinstance(command_line, (list, tuple)):
                command_line = ()
            if _is_claude_control_process(provider, command_line):
                continue
            state = str(info.get("status") or "")
            if state == str(getattr(psutil_module, "STATUS_ZOMBIE", "zombie")):
                continue
            created = info.get("create_time")
            if not isinstance(created, (int, float)):
                continue
            try:
                environment = process.environ()
            except errors:
                environment = {}
            tmux_socket, tmux_pane = _tmux_location(environment)
            result.append(
                ProcessInfo(
                    pid=int(info["pid"]),
                    provider=provider,
                    start_time=time.ctime(float(created)),
                    state=state,
                    cwd=str(info.get("cwd") or ""),
                    tty=str(info.get("terminal") or ""),
                    tmux_socket=tmux_socket,
                    tmux_pane=tmux_pane,
                )
            )
        except errors + (KeyError, TypeError, ValueError):
            continue
    return result


def process_details(pid: int) -> tuple[str, str] | None:
    """Return process state and a provider-compatible stable start identity."""

    if sys.platform == "darwin":
        return _darwin_process_details(pid, _load_psutil())
    if sys.platform.startswith("linux"):
        return _linux_process_details(pid)
    return None


def open_process_files(pid: int) -> list[pathlib.Path]:
    """Return regular file paths currently opened by *pid*."""

    if sys.platform == "darwin":
        return _darwin_open_process_files(pid, _load_psutil())
    if not sys.platform.startswith("linux"):
        return []
    return _linux_open_process_files(pid)


def exact_agent_processes() -> list[ProcessInfo]:
    """Discover current-user Codex and Claude processes on supported systems."""

    if sys.platform == "darwin":
        return _darwin_agent_processes(_load_psutil())
    if sys.platform.startswith("linux"):
        return _linux_agent_processes()
    raise RuntimeError(f"unsupported operating system: {sys.platform}")
