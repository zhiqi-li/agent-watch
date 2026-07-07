"""Security and privacy regression tests for the public release.

These tests intentionally exercise public behavior.  They prefer the packaged
``agent_watch.core`` module while retaining a temporary fallback for the
pre-package, single-file layout used during the refactor.
"""

from __future__ import annotations

import importlib
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))
sys.path.insert(1 if SRC.is_dir() else 0, str(ROOT))

try:
    aw = importlib.import_module("agent_watch.core")
except ModuleNotFoundError:
    aw = importlib.import_module("agent_watch")


PRIVATE_MODE = 0o600
DAY = 24 * 60 * 60


def write_private_config(path: pathlib.Path, content: str = "") -> None:
    path.write_text(content or "[monitor]\ninterval_seconds = 5\n")
    path.chmod(PRIVATE_MODE)


def notification_row() -> dict[str, object]:
    return {
        "session_key": "codex:release-security",
        "provider": "codex",
        "session_id": "release-security",
        "pid": 1234,
        "proc_start": "99",
        "pane_id": "%private-pane",
        "tmux_target": "work:3.1",
        "tmux_socket": "/home/private-user/.tmux/private.sock",
        "cwd": "/home/private-user/client-secret-project",
        "name": "client-secret-project",
        "state": "needs_input",
        "state_since": time.time() - 30,
        "event_id": "event-1",
        "source": "codex-hook",
        "raw_status": "PermissionRequest",
        "message": "private prompt text",
    }


def observation(key: str, state: str, observed_at: float):
    return aw.Observation(
        key=key,
        provider="codex",
        session_id=key.split(":", 1)[-1],
        pid=None,
        proc_start="",
        pane_id="",
        tmux_target="",
        cwd="/work/release-security",
        name="release-security",
        state=state,
        event_id=f"event:{key}",
        source="test",
        observed_at=observed_at,
    )


class ConfigFileSecurityTests(unittest.TestCase):
    def test_private_owned_regular_config_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            write_private_config(path)
            config = aw.load_config(path)
            self.assertEqual(config["monitor"]["interval_seconds"], 5)

    def test_quoted_false_privacy_switches_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            for key in (
                "include_cwd",
                "include_message_preview",
                "include_tmux_socket",
                "allow_insecure_http",
            ):
                with self.subTest(key=key):
                    write_private_config(
                        path,
                        f'[notifications]\n{key} = "false"\n',
                    )
                    with self.assertRaises(ValueError):
                        aw.load_config(path)

    def test_non_finite_numeric_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            for value in ("nan", "inf", "-inf"):
                with self.subTest(value=value):
                    write_private_config(
                        path,
                        f"[monitor]\ninterval_seconds = {value}\n",
                    )
                    with self.assertRaises(ValueError):
                        aw.load_config(path)

    def test_config_symlink_is_rejected_even_when_target_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target = root / "real.toml"
            link = root / "config.toml"
            write_private_config(target)
            link.symlink_to(target)

            with self.assertRaises((PermissionError, ValueError)):
                aw.load_config(link)

    def test_group_or_world_accessible_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            write_private_config(path)
            for insecure_mode in (0o640, 0o604, 0o666):
                with self.subTest(mode=oct(insecure_mode)):
                    path.chmod(insecure_mode)
                    with self.assertRaises((PermissionError, ValueError)):
                        aw.load_config(path)

    def test_config_owned_by_another_uid_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            write_private_config(path)
            foreign_uid = os.geteuid() + 1
            with mock.patch.object(aw.os, "geteuid", return_value=foreign_uid), mock.patch.object(
                aw.os, "getuid", return_value=foreign_uid
            ):
                with self.assertRaises((PermissionError, ValueError)):
                    aw.load_config(path)


class RemoteNotificationPrivacyTests(unittest.TestCase):
    def test_default_remote_event_and_body_omit_local_tmux_identifiers(self):
        row = notification_row()
        config = aw.deep_merge(aw.DEFAULT_CONFIG, {})

        public = aw.row_public(row, config)
        self.assertNotIn("tmux_socket", public)
        self.assertNotIn("pane_id", public)
        self.assertNotIn("name", public)
        self.assertEqual(public.get("tmux_target"), "work:3.1")

        _title, body, payload = aw.format_notification([row], config)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(row["tmux_socket"]), serialized)
        self.assertNotIn(str(row["pane_id"]), serialized)
        self.assertNotIn("tmux -S", body)
        self.assertNotIn("attach", body.lower())

    def test_tmux_socket_can_only_be_included_by_explicit_opt_in(self):
        row = notification_row()
        config = aw.deep_merge(
            aw.DEFAULT_CONFIG,
            {"notifications": {"include_tmux_socket": True}},
        )
        public = aw.row_public(row, config)
        self.assertEqual(public.get("tmux_socket"), row["tmux_socket"])
        self.assertEqual(public.get("pane_id"), row["pane_id"])


class StateStorageSecurityTests(unittest.TestCase):
    def test_clear_history_removes_database_spool_and_error_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            db = aw.StateDB(state)
            db.upsert(observation("codex:clear", "exited", time.time()))
            db.close()
            spool = state / "spool"
            spool.mkdir(mode=0o700)
            (spool / "event.json").write_text("{}")
            (state / "hook-errors.log").write_text("private error\n")
            args = type("Args", (), {"yes": True, "state_dir": str(state)})()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(aw.clear_history_command(args), 0)
            db = aw.StateDB(state)
            self.assertEqual(db.conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            db.close()
            self.assertFalse((spool / "event.json").exists())
            self.assertFalse((state / "hook-errors.log").exists())

    def test_immediate_hook_snapshot_honors_explicit_privacy_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="codex:opt-in",
                provider="codex",
                session_id="opt-in",
                pid=None,
                proc_start="",
                pane_id="",
                tmux_target="",
                cwd="/private/full/path",
                name="path",
                state="needs_input",
                event_id="event",
                source="codex-hook",
                message="explicit preview",
            )
            db.upsert(obs)
            config = aw.deep_merge(
                aw.DEFAULT_CONFIG,
                {
                    "notifications": {
                        "include_cwd": True,
                        "include_message_preview": True,
                    }
                },
            )
            db.enqueue_session_now(obs.key, config)
            snapshot = json.loads(
                db.conn.execute("SELECT snapshot_json FROM outbox").fetchone()[0]
            )
            self.assertEqual(snapshot["cwd"], obs.cwd)
            self.assertEqual(snapshot["message"], obs.message)
            db.close()

    def test_database_wal_and_shared_memory_are_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_umask = os.umask(0)
            db = None
            try:
                db = aw.StateDB(pathlib.Path(tmp) / "state")
                db.set_meta("permission-test", "1")
                paths = (
                    db.path,
                    pathlib.Path(f"{db.path}-wal"),
                    pathlib.Path(f"{db.path}-shm"),
                )
                for path in paths:
                    with self.subTest(path=path.name):
                        self.assertTrue(path.exists(), f"expected SQLite sidecar {path.name}")
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), PRIVATE_MODE)
            finally:
                if db is not None:
                    db.close()
                os.umask(old_umask)

    def test_retention_prunes_only_expired_history_and_terminal_sessions(self):
        now = 2_000_000_000.0
        old = now - 31 * DAY
        recent = now - DAY
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            try:
                db.upsert(observation("codex:old-exit", "exited", old))
                db.upsert(observation("codex:recent-exit", "exited", recent))
                db.upsert(observation("codex:old-running", "running", old))

                for created_at, marker in ((old, "old"), (recent, "recent")):
                    db.conn.execute(
                        """INSERT INTO notifications(
                               created_at, kind, session_key, provider,
                               payload_json, delivered_json
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            created_at,
                            "test",
                            f"codex:{marker}",
                            "codex",
                            json.dumps({"marker": marker}),
                            "{}",
                        ),
                    )

                outbox_rows = (
                    ("sent-old", old, old, "codex:old-exit", old),
                    ("sent-recent", recent, recent, "codex:recent-exit", recent),
                    ("pending-old", old, old, "codex:old-running", None),
                )
                db.conn.executemany(
                    """INSERT INTO outbox(
                           event_key, created_at, available_at, session_key,
                           event_id, kind, snapshot_json, sent_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    [
                        (
                            event_key,
                            created_at,
                            available_at,
                            session_key,
                            event_key,
                            "test",
                            "{}",
                            sent_at,
                        )
                        for event_key, created_at, available_at, session_key, sent_at in outbox_rows
                    ],
                )
                db.conn.commit()

                db.prune(retention_days=30, now=now)

                session_keys = {
                    row[0] for row in db.conn.execute("SELECT session_key FROM sessions")
                }
                self.assertNotIn("codex:old-exit", session_keys)
                self.assertIn("codex:recent-exit", session_keys)
                self.assertIn("codex:old-running", session_keys)

                notification_markers = {
                    json.loads(row[0])["marker"]
                    for row in db.conn.execute("SELECT payload_json FROM notifications")
                }
                self.assertEqual(notification_markers, {"recent"})

                outbox_keys = {
                    row[0] for row in db.conn.execute("SELECT event_key FROM outbox")
                }
                self.assertNotIn("sent-old", outbox_keys)
                self.assertIn("sent-recent", outbox_keys)
                self.assertIn("pending-old", outbox_keys)
            finally:
                db.close()


class HookOwnershipTests(unittest.TestCase):
    def test_installed_hook_pins_custom_config_and_state_paths(self):
        command = aw.hook_handler_command(
            "codex",
            state_dir="/tmp/custom state",
            config_path="/tmp/custom config.toml",
        )
        handler = {"type": "command", "command": command}
        self.assertTrue(aw.is_owned_hook_handler(handler, "codex"))
        self.assertIn("--state-dir '/tmp/custom state'", command)
        self.assertIn("--config '/tmp/custom config.toml'", command)

    def test_add_hook_does_not_reconcile_an_unowned_lookalike(self):
        unrelated = {
            "type": "command",
            "command": "/opt/another-tool hook --source claude",
            "timeout": 99,
        }
        settings = {"hooks": {"Stop": [{"hooks": [dict(unrelated)]}]}}

        self.assertTrue(aw.add_hook(settings, "Stop", "claude"))
        handlers = [
            handler
            for group in settings["hooks"]["Stop"]
            for handler in group.get("hooks", [])
        ]
        self.assertIn(unrelated, handlers)
        owned = [item for item in handlers if aw.is_owned_hook_handler(item, "claude")]
        self.assertEqual(len(owned), 1)

    def test_uninstall_removes_owned_handler_but_preserves_name_collisions(self):
        settings: dict[str, object] = {}
        aw.add_hook(settings, "Stop", "claude")
        collision = {
            "type": "command",
            "command": "/usr/local/bin/agent-watch-helper --cleanup",
        }
        settings["hooks"]["Stop"][0]["hooks"].append(collision)

        self.assertTrue(aw.remove_agent_watch_handlers(settings))
        remaining = settings["hooks"]["Stop"][0]["hooks"]
        self.assertEqual(remaining, [collision])
        self.assertFalse(aw.is_owned_hook_handler(collision))


class ConversationPreviewConfigTests(unittest.TestCase):
    def test_conversation_preview_is_private_by_default(self):
        self.assertIn("ui", aw.DEFAULT_CONFIG)
        self.assertIs(aw.DEFAULT_CONFIG["ui"]["conversation_preview"], False)

    def test_conversation_preview_requires_explicit_config_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            write_private_config(path, "[ui]\nconversation_preview = true\n")
            config = aw.load_config(path)
            self.assertIs(config["ui"]["conversation_preview"], True)


class NotificationUrlValidationTests(unittest.TestCase):
    def test_https_is_accepted(self):
        aw.validate_notification_url("https://notify.example.test/hook")

    def test_non_http_schemes_and_missing_hosts_are_rejected(self):
        for url in (
            "file:///etc/passwd",
            "ftp://notify.example.test/message",
            "javascript:alert(1)",
            "notify.example.test/no-scheme",
            "https:///missing-host",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    aw.validate_notification_url(url)

    def test_plain_http_requires_explicit_opt_in(self):
        for url in (
            "http://localhost:8080/hook",
            "http://127.0.0.1:8080/hook",
            "http://[::1]:8080/hook",
            "http://notify.example.test/hook",
        ):
            with self.subTest(url=url, allowed=False):
                with self.assertRaises(ValueError):
                    aw.validate_notification_url(url)
            with self.subTest(url=url, allowed=True):
                aw.validate_notification_url(url, allow_insecure_http=True)

    def test_load_config_applies_url_policy_to_notification_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            write_private_config(
                path,
                '[notifications.webhook]\nurl = "http://notify.example.test/hook"\n',
            )
            with self.assertRaises(ValueError):
                aw.load_config(path)

            write_private_config(
                path,
                '[notifications]\nallow_insecure_http = true\n'
                '[notifications.webhook]\nurl = "http://notify.example.test/hook"\n',
            )
            config = aw.load_config(path)
            self.assertTrue(config["notifications"]["allow_insecure_http"])


if __name__ == "__main__":
    unittest.main()
