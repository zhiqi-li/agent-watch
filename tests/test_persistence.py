from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_watch import core
from agent_watch import persistence


class PersistenceTests(unittest.TestCase):
    def test_disabled_by_default_and_environment_directory_enables_it(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(persistence.settings_from_config(core.DEFAULT_CONFIG))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {persistence.ENV_PERSIST_DIR: tmp},
            clear=True,
        ):
            settings = persistence.settings_from_config(core.DEFAULT_CONFIG)
            self.assertIsNotNone(settings)
            self.assertEqual(settings.directory, pathlib.Path(tmp))

    def test_relative_or_root_persistence_directory_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                persistence.settings_from_config(
                    core.DEFAULT_CONFIG,
                    directory_override="relative/history",
                    force=True,
                )
            with self.assertRaises(ValueError):
                persistence.settings_from_config(
                    core.DEFAULT_CONFIG,
                    directory_override="/",
                    force=True,
                )

    def test_backup_and_restore_transcripts_and_consistent_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            live_home = root / "live-home"
            state_dir = root / "live-state"
            store = root / "slow-store"
            codex = live_home / ".codex" / "sessions" / "2026" / "07" / "13"
            claude = live_home / ".claude" / "projects" / "-work-demo"
            codex.mkdir(parents=True)
            claude.mkdir(parents=True)
            codex_file = codex / "rollout-synthetic.jsonl"
            claude_file = claude / "synthetic-session.jsonl"
            codex_file.write_text('{"type":"session_meta","id":"synthetic"}\n')
            claude_file.write_text('{"type":"user","sessionId":"synthetic"}\n')
            (live_home / ".codex" / "auth.json").write_text("synthetic auth fixture")
            (live_home / ".claude" / "settings.json").write_text(
                "synthetic settings fixture"
            )

            database = core.StateDB(state_dir)
            database.set_meta("synthetic", "preserved")
            # Keep the WAL connection open: backup must use SQLite's online
            # backup API instead of copying the live database files directly.
            stats = persistence.backup_history(
                store,
                home=live_home,
                state_dir=state_dir,
            )
            self.assertEqual(stats.files_copied, 2)
            self.assertTrue(stats.database_copied)
            self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
            for directory in (path for path in store.rglob("*") if path.is_dir()):
                with self.subTest(directory=directory.relative_to(store)):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(
                    (store / "providers/codex/sessions/2026/07/13/rollout-synthetic.jsonl")
                    .stat()
                    .st_mode
                ),
                0o600,
            )
            self.assertFalse((store / "providers/codex/auth.json").exists())
            self.assertFalse((store / "providers/claude/settings.json").exists())
            manifest = json.loads((store / persistence.MANIFEST_NAME).read_text())
            self.assertEqual(manifest["format_version"], persistence.FORMAT_VERSION)
            self.assertNotIn(str(live_home), json.dumps(manifest))
            database.close()

            restored_home = root / "restored-home"
            restored_state = root / "restored-state"
            restored = persistence.restore_history(
                store,
                home=restored_home,
                state_dir=restored_state,
            )
            self.assertEqual(restored.files_copied, 2)
            self.assertTrue(restored.database_copied)
            self.assertEqual(
                (
                    restored_home
                    / ".codex/sessions/2026/07/13/rollout-synthetic.jsonl"
                ).read_text(),
                codex_file.read_text(),
            )
            self.assertEqual(
                (
                    restored_home
                    / ".claude/projects/-work-demo/synthetic-session.jsonl"
                ).read_text(),
                claude_file.read_text(),
            )
            connection = sqlite3.connect(restored_state / "state.sqlite3")
            try:
                value = connection.execute(
                    "SELECT value FROM meta WHERE key='synthetic'"
                ).fetchone()
                self.assertEqual(value, ("preserved",))
            finally:
                connection.close()

    def test_incremental_backup_updates_changed_files_and_restore_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = root / "home"
            state = root / "state"
            store = root / "store"
            session = home / ".codex/sessions/2026/07/13/session.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("first\n")
            database = core.StateDB(state)
            database.close()
            first = persistence.backup_history(store, home=home, state_dir=state)
            self.assertEqual(first.files_copied, 1)
            backed_up = store / "providers/codex/sessions/2026/07/13/session.jsonl"
            source_mtime = session.stat().st_mtime_ns
            os.utime(
                backed_up,
                ns=(source_mtime, source_mtime // 1_000_000_000 * 1_000_000_000),
            )
            second = persistence.backup_history(store, home=home, state_dir=state)
            self.assertEqual(second.files_copied, 0)
            self.assertEqual(second.files_skipped, 1)

            with session.open("a") as output:
                output.write("second\n")
            changed = persistence.backup_history(store, home=home, state_dir=state)
            self.assertEqual(changed.files_copied, 1)
            self.assertEqual(changed.bytes_copied, len("second\n"))
            self.assertEqual(backed_up.read_text(), "first\nsecond\n")

            with session.open("a") as output:
                output.write("third\n")
            with backed_up.open("ab") as output:
                output.write(b"thi")
            resumed = persistence.backup_history(store, home=home, state_dir=state)
            self.assertEqual(resumed.bytes_copied, len("rd\n"))
            self.assertEqual(backed_up.read_text(), "first\nsecond\nthird\n")

            with backed_up.open("ab") as output:
                output.write(b"incomplete record")
            trimmed_home = root / "trimmed-restore"
            persistence.restore_history(
                store,
                home=trimmed_home,
                state_dir=root / "trimmed-state",
            )
            self.assertEqual(
                (
                    trimmed_home / ".codex/sessions/2026/07/13/session.jsonl"
                ).read_text(),
                "first\nsecond\nthird\n",
            )

            restored_home = root / "restored"
            local = restored_home / ".codex/sessions/2026/07/13/session.jsonl"
            local.parent.mkdir(parents=True)
            local.write_text("newer local data\n")
            restored = persistence.restore_history(
                store,
                home=restored_home,
                state_dir=root / "restored-state",
            )
            self.assertGreaterEqual(restored.files_skipped, 1)
            self.assertEqual(local.read_text(), "newer local data\n")

    def test_symlinked_transcript_is_not_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = root / "home"
            state = root / "state"
            store = root / "store"
            sessions = home / ".codex/sessions"
            sessions.mkdir(parents=True)
            secret = root / "secret.txt"
            secret.write_text("must not be copied")
            (sessions / "linked.jsonl").symlink_to(secret)
            database = core.StateDB(state)
            database.close()
            stats = persistence.backup_history(store, home=home, state_dir=state)
            self.assertEqual(stats.files_copied, 0)
            self.assertFalse((store / "providers/codex/sessions/linked.jsonl").exists())

    def test_background_worker_does_not_block_monitor_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            settings = persistence.PersistenceSettings(
                directory=root / "store",
                interval_seconds=300,
                restore_on_start=True,
                backup_on_shutdown=True,
            )
            entered = threading.Event()
            release = threading.Event()

            def slow_backup(*_args, **_kwargs):
                entered.set()
                release.wait(2)
                return persistence.SyncStats()

            worker = persistence.BackupWorker(
                settings,
                home=root / "home",
                state_dir=root / "state",
            )
            with mock.patch.object(persistence, "backup_history", side_effect=slow_backup):
                started = time.monotonic()
                self.assertTrue(worker.start_if_due(force=True))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(1))
                self.assertFalse(worker.start_if_due(force=True))
                release.set()
                outcome = worker.shutdown(final_backup=False, timeout=2)
            self.assertIsNotNone(outcome)
            self.assertFalse(outcome.error)


if __name__ == "__main__":
    unittest.main()
