"""Optional persistence for ephemeral-container session history.

Active provider files and Agent Watch's WAL database stay on local storage.  A
background worker copies only provider transcripts and a consistent SQLite
snapshot to operator-configured persistent storage.  Authentication files and
provider settings are deliberately outside the backup set.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import math
import os
import pathlib
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Mapping
from typing import Any


FORMAT_VERSION = 1
TEMP_PREFIX = ".agent-watch-tmp-"
LOCK_NAME = ".agent-watch.lock"
MANIFEST_NAME = "manifest.json"
ENV_PERSIST_DIR = "AGENT_WATCH_PERSIST_DIR"


class PersistenceError(RuntimeError):
    """The configured persistence store is unsafe or unavailable."""


class PersistenceBusy(PersistenceError):
    """Another process is currently using the persistence store."""


@dataclasses.dataclass(frozen=True)
class PersistenceSettings:
    directory: pathlib.Path
    interval_seconds: float
    restore_on_start: bool
    backup_on_shutdown: bool


@dataclasses.dataclass
class SyncStats:
    files_copied: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_copied: int = 0
    database_copied: bool = False

    def summary(self, action: str) -> str:
        database = "database copied" if self.database_copied else "database skipped"
        return (
            f"{action}: {self.files_copied} files copied, "
            f"{self.files_skipped} unchanged, {self.files_failed} failed, "
            f"{self.bytes_copied} bytes, {database}"
        )


@dataclasses.dataclass(frozen=True)
class BackupOutcome:
    stats: SyncStats | None = None
    error: str = ""
    timed_out: bool = False


def settings_from_config(
    config: Mapping[str, Any],
    *,
    directory_override: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> PersistenceSettings | None:
    """Resolve persistence settings without embedding an installation path.

    ``AGENT_WATCH_PERSIST_DIR`` overrides TOML and enables persistence by
    itself, which is convenient when an ephemeral container receives its
    configuration from the orchestrator.
    """

    raw = config.get("persistence", {})
    if not isinstance(raw, Mapping):
        raise ValueError("persistence must be a TOML table")
    environment_directory = os.environ.get(ENV_PERSIST_DIR, "").strip()
    configured_directory = str(raw.get("directory", "")).strip()
    chosen = (
        os.fspath(directory_override)
        if directory_override is not None
        else environment_directory or configured_directory
    )
    enabled = force or bool(raw.get("enabled", False)) or bool(environment_directory)
    if not enabled:
        return None
    if not chosen:
        raise ValueError(
            f"persistence.directory or {ENV_PERSIST_DIR} is required when persistence is enabled"
        )
    directory = pathlib.Path(chosen).expanduser()
    if not directory.is_absolute():
        raise ValueError("persistence.directory must be an absolute path")
    if directory == pathlib.Path(directory.anchor):
        raise ValueError("persistence.directory must not be a filesystem root")
    interval = raw.get("interval_seconds", 300.0)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(float(interval))
        or float(interval) <= 0
    ):
        raise ValueError("persistence.interval_seconds must be a positive number")
    return PersistenceSettings(
        directory=directory.absolute(),
        interval_seconds=max(1.0, float(interval)),
        restore_on_start=bool(raw.get("restore_on_start", True)),
        backup_on_shutdown=bool(raw.get("backup_on_shutdown", True)),
    )


def _check_owned(metadata: os.stat_result, path: pathlib.Path) -> None:
    if metadata.st_uid != os.getuid():
        raise PersistenceError(f"path must be owned by uid {os.getuid()}: {path}")


def _ensure_private_directory(path: pathlib.Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise PersistenceError(f"unable to create persistence directory {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PersistenceError(f"persistence path is not a real directory: {path}")
    _check_owned(metadata, path)
    try:
        path.chmod(0o700)
        if stat.S_IMODE(path.lstat().st_mode) & 0o077:
            raise PersistenceError(f"persistence directory is not private: {path}")
    except OSError as exc:
        raise PersistenceError(f"unable to secure persistence directory {path}: {exc}") from exc


def _ensure_private_subdirectory(root: pathlib.Path, relative: pathlib.PurePath) -> pathlib.Path:
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise PersistenceError(f"unsafe relative persistence path: {relative}")
        current = current / part
        _ensure_private_directory(current)
    return current


@contextlib.contextmanager
def _store_lock(root: pathlib.Path):
    _ensure_private_directory(root)
    lock_path = root / LOCK_NAME
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PersistenceError(f"persistence lock is not a regular file: {lock_path}")
        _check_owned(metadata, lock_path)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PersistenceBusy("another history backup or restore is already running") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _iter_regular_files(root: pathlib.Path):
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(TEMP_PREFIX):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = pathlib.Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid():
                yield path, metadata, path.relative_to(root)


def _destination_is_current(
    destination: pathlib.Path,
    source: os.stat_result,
    expected_size: int,
) -> bool:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PersistenceError(f"refusing non-regular persistence file: {destination}")
    _check_owned(metadata, destination)
    # Some distributed filesystems expose only whole-second mtimes even when
    # the local source has nanosecond precision. Provider transcripts are
    # append-only, so size plus the same mtime second is a safe incremental
    # identity check without re-reading every unchanged file for a hash.
    same_mtime = (
        metadata.st_mtime_ns == source.st_mtime_ns
        or metadata.st_mtime_ns // 1_000_000_000
        == source.st_mtime_ns // 1_000_000_000
    )
    return metadata.st_size == expected_size and same_mtime


def _last_complete_jsonl_size(fd: int, size: int) -> int:
    """Return the prefix ending after the last newline without unbounded reads."""

    position = size
    while position > 0:
        start = max(0, position - 1024 * 1024)
        chunk = os.pread(fd, position - start, start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


def _prefix_matches(source_fd: int, destination_fd: int, size: int) -> bool:
    sample_size = min(4096, size)
    if sample_size <= 0:
        return True
    offsets = {0, max(0, size - sample_size)}
    for offset in offsets:
        source_sample = os.pread(source_fd, sample_size, offset)
        destination_sample = os.pread(destination_fd, sample_size, offset)
        if source_sample != destination_sample:
            return False
    return True


def _append_verified_growth(
    source_fd: int,
    source_metadata: os.stat_result,
    destination: pathlib.Path,
    copy_size: int,
) -> tuple[bool, int]:
    """Append a verified source prefix; a crash leaves a resumable prefix."""

    flags = (
        os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    destination_fd = os.open(destination, flags)
    try:
        metadata = os.fstat(destination_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PersistenceError(f"refusing non-regular destination file: {destination}")
        _check_owned(metadata, destination)
        if metadata.st_size > copy_size:
            return False, 0
        if not _prefix_matches(source_fd, destination_fd, metadata.st_size):
            return False, 0
        if metadata.st_size == copy_size:
            return True, 0
        os.lseek(source_fd, metadata.st_size, os.SEEK_SET)
        remaining = copy_size - metadata.st_size
        copied = 0
        while remaining > 0:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise PersistenceError(f"source changed while backing up: {destination.name}")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
                copied += written
                remaining -= written
        os.fsync(destination_fd)
        os.utime(
            destination,
            ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
            follow_symlinks=False,
        )
        return True, copied
    finally:
        os.close(destination_fd)


def _atomic_copy(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    skip_existing: bool = False,
    append_jsonl_growth: bool = False,
    complete_jsonl_only: bool = False,
) -> tuple[bool, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    temp_path: pathlib.Path | None = None
    try:
        source_metadata = os.fstat(source_fd)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise PersistenceError(f"refusing non-regular source file: {source}")
        _check_owned(source_metadata, source)
        copy_size = (
            _last_complete_jsonl_size(source_fd, source_metadata.st_size)
            if complete_jsonl_only
            else source_metadata.st_size
        )
        if copy_size <= 0:
            return False, 0
        try:
            destination_metadata = destination.lstat()
        except FileNotFoundError:
            destination_metadata = None
        if destination_metadata is not None:
            if stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISREG(
                destination_metadata.st_mode
            ):
                raise PersistenceError(f"refusing non-regular destination file: {destination}")
            _check_owned(destination_metadata, destination)
            if skip_existing or _destination_is_current(
                destination, source_metadata, copy_size
            ):
                return False, 0
            if append_jsonl_growth:
                handled, appended = _append_verified_growth(
                    source_fd,
                    source_metadata,
                    destination,
                    copy_size,
                )
                if handled:
                    return appended > 0, appended
        _ensure_private_directory(destination.parent)
        temp_fd, raw_temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=destination.parent)
        temp_path = pathlib.Path(raw_temp)
        copied = 0
        try:
            os.fchmod(temp_fd, 0o600)
            remaining = copy_size
            os.lseek(source_fd, 0, os.SEEK_SET)
            while remaining > 0:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise PersistenceError(f"source changed while backing up: {source.name}")
                view = memoryview(chunk)
                while view:
                    written = os.write(temp_fd, view)
                    view = view[written:]
                    copied += written
                    remaining -= written
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        os.utime(
            temp_path,
            ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
            follow_symlinks=False,
        )
        os.replace(temp_path, destination)
        temp_path = None
        return True, copied
    finally:
        os.close(source_fd)
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _sync_tree(
    source: pathlib.Path,
    destination: pathlib.Path,
    stats: SyncStats,
    *,
    restore_missing_only: bool,
) -> None:
    for path, _metadata, relative in _iter_regular_files(source):
        try:
            parent = _ensure_private_subdirectory(destination, relative.parent)
            is_jsonl = path.suffix.lower() == ".jsonl"
            copied, byte_count = _atomic_copy(
                path,
                parent / relative.name,
                skip_existing=restore_missing_only,
                append_jsonl_growth=not restore_missing_only and is_jsonl,
                complete_jsonl_only=is_jsonl,
            )
            if copied:
                stats.files_copied += 1
                stats.bytes_copied += byte_count
            else:
                stats.files_skipped += 1
        except (OSError, PersistenceError):
            stats.files_failed += 1


def _sqlite_quick_check(path: pathlib.Path) -> None:
    uri = f"{path.absolute().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise PersistenceError(f"SQLite integrity check failed for {path}")
    finally:
        connection.close()


def _backup_sqlite(source: pathlib.Path, destination: pathlib.Path) -> bool:
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PersistenceError(f"refusing non-regular SQLite source: {source}")
    _check_owned(metadata, source)
    _ensure_private_directory(destination.parent)
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise PersistenceError(f"refusing non-regular SQLite destination: {destination}")
        _check_owned(existing, destination)
    fd, raw_temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=destination.parent)
    os.close(fd)
    temp_path = pathlib.Path(raw_temp)
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_uri = f"{source.absolute().as_uri()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=10.0)
        target_connection = sqlite3.connect(temp_path, timeout=10.0)
        source_connection.backup(target_connection)
        result = target_connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise PersistenceError("SQLite backup integrity check failed")
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None
        temp_path.chmod(0o600)
        sync_fd = os.open(temp_path, os.O_RDONLY)
        try:
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.replace(temp_path, destination)
        return True
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def _restore_sqlite(source: pathlib.Path, destination: pathlib.Path) -> bool:
    try:
        destination_metadata = destination.lstat()
    except FileNotFoundError:
        destination_metadata = None
    if destination_metadata is not None and destination_metadata.st_size > 0:
        if stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISREG(
            destination_metadata.st_mode
        ):
            raise PersistenceError(f"refusing non-regular local SQLite file: {destination}")
        return False
    try:
        source_metadata = source.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
        raise PersistenceError(f"refusing non-regular SQLite backup: {source}")
    _check_owned(source_metadata, source)
    _sqlite_quick_check(source)
    copied, _byte_count = _atomic_copy(source, destination, skip_existing=False)
    return copied


def _provider_paths(home: pathlib.Path, root: pathlib.Path):
    return (
        (home / ".codex" / "sessions", root / "providers" / "codex" / "sessions"),
        (home / ".claude" / "projects", root / "providers" / "claude" / "projects"),
    )


def _write_manifest(root: pathlib.Path, stats: SyncStats) -> None:
    payload = {
        "format_version": FORMAT_VERSION,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files_copied": stats.files_copied,
        "files_skipped": stats.files_skipped,
        "files_failed": stats.files_failed,
        "bytes_copied": stats.bytes_copied,
        "database_copied": stats.database_copied,
    }
    destination = root / MANIFEST_NAME
    fd, raw_temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=root)
    temp_path = pathlib.Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def backup_history(
    root: pathlib.Path,
    *,
    home: pathlib.Path,
    state_dir: pathlib.Path,
) -> SyncStats:
    """Incrementally copy transcript trees and an online SQLite snapshot."""

    root = root.absolute()
    stats = SyncStats()
    with _store_lock(root):
        for source, destination in _provider_paths(home, root):
            _ensure_private_subdirectory(root, destination.relative_to(root))
            _sync_tree(source, destination, stats, restore_missing_only=False)
        database_destination = root / "agent-watch" / "state.sqlite3"
        stats.database_copied = _backup_sqlite(
            state_dir / "state.sqlite3", database_destination
        )
        _write_manifest(root, stats)
    return stats


def restore_history(
    root: pathlib.Path,
    *,
    home: pathlib.Path,
    state_dir: pathlib.Path,
) -> SyncStats:
    """Restore only missing local files, preserving any newer live state."""

    root = root.absolute()
    if not root.exists():
        return SyncStats()
    stats = SyncStats()
    with _store_lock(root):
        for destination, source in _provider_paths(home, root):
            if not source.exists():
                continue
            _ensure_private_directory(destination)
            _sync_tree(source, destination, stats, restore_missing_only=True)
        stats.database_copied = _restore_sqlite(
            root / "agent-watch" / "state.sqlite3", state_dir / "state.sqlite3"
        )
    return stats


class BackupWorker:
    """Run slow-store backups without blocking the daemon's monitor loop."""

    def __init__(
        self,
        settings: PersistenceSettings,
        *,
        home: pathlib.Path,
        state_dir: pathlib.Path,
    ) -> None:
        self.settings = settings
        self.home = home
        self.state_dir = state_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._outcome: BackupOutcome | None = None
        self._last_started = 0.0

    def _run(self) -> None:
        try:
            stats = backup_history(
                self.settings.directory,
                home=self.home,
                state_dir=self.state_dir,
            )
            outcome = BackupOutcome(stats=stats)
        except Exception as exc:  # the daemon reports this without stopping monitoring
            outcome = BackupOutcome(error=str(exc))
        with self._lock:
            self._outcome = outcome

    def start_if_due(self, *, force: bool = False, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not force and current - self._last_started < self.settings.interval_seconds:
                return False
            self._last_started = current
            self._thread = threading.Thread(
                target=self._run,
                name="agent-watch-history-backup",
                daemon=True,
            )
            self._thread.start()
            return True

    def poll(self) -> BackupOutcome | None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return None
            outcome = self._outcome
            self._outcome = None
            return outcome

    def shutdown(self, *, final_backup: bool, timeout: float = 20.0) -> BackupOutcome | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                return BackupOutcome(
                    error="history backup did not finish before shutdown",
                    timed_out=True,
                )
            return self.poll()
        previous = self.poll()
        if final_backup:
            self.start_if_due(force=True)
            with self._lock:
                thread = self._thread
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    return BackupOutcome(
                        error="final history backup did not finish before shutdown",
                        timed_out=True,
                    )
                return self.poll()
        return previous
