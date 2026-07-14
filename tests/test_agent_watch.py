import contextlib
import http.server
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_watch import core as aw


class AgentWatchTests(unittest.TestCase):
    def test_core_notification_copy_is_english(self):
        row = {
            "provider": "codex",
            "tmux_target": "work:1.0",
            "tmux_socket": "/tmp/tmux-test/default",
            "pane_id": "%1",
            "cwd": "/work/project",
            "name": "project",
            "state": "needs_input",
            "message": "private prompt",
            "pid": 123,
        }
        title, body, payload = aw.format_notification([row], aw.DEFAULT_CONFIG)
        self.assertEqual(title, "Codex · Needs your response or approval")
        self.assertIn("Host:", body)
        self.assertIn("Project: project", body)
        self.assertEqual(
            payload["events"][0]["state_label"],
            "Needs your response or approval",
        )
        visible_copy = (
            title
            + body
            + "".join(aw.STATE_LABELS.values())
            + "".join(aw.SHORT_LABELS.values())
        )
        self.assertFalse(any("\u4e00" <= character <= "\u9fff" for character in visible_copy))

    def test_plain_status_copy_is_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = Namespace(state_dir=tmp, json=False, full=False)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(aw.status_command(args, aw.DEFAULT_CONFIG), 0)
        rendered = output.getvalue()
        self.assertIn("daemon: stopped", rendered)
        self.assertIn("No monitored sessions yet.", rendered)
        self.assertFalse(any("\u4e00" <= character <= "\u9fff" for character in rendered))

    def test_codex_rollout_discovery_uses_platform_open_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = pathlib.Path(tmp) / "rollout-test.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"source": "cli", "id": "mac-session"},
                    }
                )
                + "\n"
            )
            aw.CODEX_ROLLOUT_CACHE.clear()
            with mock.patch.object(aw, "open_process_files", return_value=[rollout]):
                found = aw.find_main_codex_rollout(123, "start")
            self.assertEqual(found, (rollout, "mac-session"))

    def test_claude_session_accepts_platform_start_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            session_dir = home / ".claude" / "sessions"
            session_dir.mkdir(parents=True)
            payload = {
                "pid": 123,
                "procStart": "Wed Jul  8 10:56:27 2026",
                "sessionId": "claude-session",
            }
            (session_dir / "123.json").write_text(json.dumps(payload))
            with (
                mock.patch.object(aw, "HOME", home),
                mock.patch.object(
                    aw,
                    "process_details",
                    return_value=("running", "Wed Jul  8 10:56:27 2026"),
                ),
            ):
                self.assertEqual(aw.load_claude_session(123), payload)
            with (
                mock.patch.object(aw, "HOME", home),
                mock.patch.object(
                    aw,
                    "process_details",
                    return_value=("running", "Wed Jul  8 10:57:00 2026"),
                ),
            ):
                self.assertIsNone(aw.load_claude_session(123))

    def test_macos_desktop_notification_uses_argv_not_script_interpolation(self):
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(aw.sys, "platform", "darwin"),
            mock.patch.object(aw.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(aw.subprocess, "run", return_value=completed) as run,
        ):
            result = aw.send_desktop_notification(
                'Agent "Watch"', 'body with "quotes"', 3.0
            )
        self.assertTrue(result)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(command[-2:], ['Agent "Watch"', 'body with "quotes"'])
        self.assertNotIn('Agent "Watch"', command[2])
        self.assertNotIn('body with "quotes"', command[2])

    def test_replayed_attention_hook_is_deduplicated(self):
        payload = {
            "hook_event_name": "Notification",
            "notification_type": "agent_needs_input",
            "session_id": "same-session",
            "message": "Please answer",
        }
        with mock.patch.object(aw, "utc_now", return_value=1_000_001.0):
            first = aw.hook_to_observation("claude", payload)
            second = aw.hook_to_observation("claude", payload)
        self.assertIsNotNone(first)
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.event_id, second.event_id)

    def test_user_prompt_resolves_hook_attention_and_background_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            needs_input = aw.hook_to_observation(
                "claude",
                {
                    "hook_event_name": "Notification",
                    "notification_type": "agent_needs_input",
                    "session_id": "resolve-session",
                    "message": "Question",
                    "timestamp": "1",
                },
            )
            completed = aw.hook_to_observation(
                "claude",
                {
                    "hook_event_name": "Notification",
                    "notification_type": "agent_completed",
                    "session_id": "resolve-session",
                    "message": "Done",
                    "timestamp": "2",
                },
            )
            prompt = aw.hook_to_observation(
                "claude",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "resolve-session",
                    "prompt": "Continue",
                    "timestamp": "3",
                },
            )
            for obs in (needs_input, completed):
                self.assertIsNotNone(obs)
                aw.apply_hook_observation(db, obs)
            aw.apply_hook_observation(db, prompt)
            stale = db.conn.execute(
                """SELECT state FROM sessions
                   WHERE session_id='resolve-session' AND session_key!=?""",
                (prompt.key,),
            ).fetchall()
            self.assertEqual({row["state"] for row in stale}, {"resolved"})
            db.close()

    def test_pane_activity_ignores_spinner_and_elapsed_time(self):
        first = aw.pane_activity_id("build\n⠋ Working (12m 3s)\n")
        second = aw.pane_activity_id("build\n⠙ Working (13m 8s)\n")
        changed = aw.pane_activity_id("build finished\n⠙ Working (13m 8s)\n")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_pane_location_requires_the_process_terminal(self):
        pane = aw.Pane(
            pane_id="%4",
            session="3",
            window="0",
            index="0",
            pid=100,
            tty="/dev/pts/9",
            command="codex",
            cwd="/repo",
            title="repo",
            dead=False,
            socket_path="/tmp/tmux-0/default",
        )
        inherited = aw.ProcessInfo(
            pid=200,
            provider="claude",
            start_time="1",
            state="S",
            cwd="/root",
            tty="",
            tmux_socket="/tmp/tmux-0/default",
            tmux_pane="%4",
        )
        wrong_terminal = aw.dataclasses.replace(inherited, tty="/dev/pts/11")
        attached = aw.dataclasses.replace(inherited, tty="/dev/pts/9")

        self.assertIsNone(aw.pane_for_process(inherited, [pane]))
        self.assertIsNone(aw.pane_for_process(wrong_terminal, [pane]))
        self.assertIs(aw.pane_for_process(attached, [pane]), pane)

    def test_activity_timestamp_persists_until_real_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            db = aw.StateDB(path)
            first = aw.Observation(
                key="codex:activity",
                provider="codex",
                session_id="activity",
                pid=1,
                proc_start="1",
                pane_id="%1",
                tmux_target="work:0.0",
                tmux_socket="/tmp/tmux-test",
                cwd="/repo",
                name="repo",
                state="running",
                event_id="turn",
                source="test",
                pane_activity_id="pane-a",
                artifact_activity_id="file-a",
                observed_at=1000,
            )
            db.upsert(first)
            db.upsert(aw.dataclasses.replace(first, observed_at=1100))
            row = db.conn.execute(
                "SELECT * FROM sessions WHERE session_key=?", (first.key,)
            ).fetchone()
            self.assertEqual(row["last_activity_at"], 1000)
            self.assertEqual(row["tmux_socket"], "/tmp/tmux-test")
            db.close()

            reopened = aw.StateDB(path)
            reopened.upsert(
                aw.dataclasses.replace(
                    first, pane_activity_id="pane-b", observed_at=1200
                )
            )
            row = reopened.conn.execute(
                "SELECT * FROM sessions WHERE session_key=?", (first.key,)
            ).fetchone()
            self.assertEqual(row["last_activity_at"], 1200)
            self.assertEqual(row["artifact_activity_id"], "file-a")
            reopened.close()

    def test_existing_database_is_migrated_for_activity_and_tmux_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            connection = aw.sqlite3.connect(path / "state.sqlite3")
            connection.execute(
                "CREATE TABLE sessions (session_key TEXT PRIMARY KEY, provider TEXT NOT NULL, "
                "session_id TEXT NOT NULL DEFAULT '', pid INTEGER, proc_start TEXT NOT NULL DEFAULT '', "
                "pane_id TEXT NOT NULL DEFAULT '', tmux_target TEXT NOT NULL DEFAULT '', "
                "cwd TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, "
                "state_since REAL NOT NULL, last_seen REAL NOT NULL, event_id TEXT NOT NULL DEFAULT '', "
                "source TEXT NOT NULL DEFAULT '', raw_status TEXT NOT NULL DEFAULT '', "
                "message TEXT NOT NULL DEFAULT '', notified_event_id TEXT NOT NULL DEFAULT '', "
                "notified_at REAL, first_seen REAL NOT NULL)"
            )
            connection.commit()
            connection.close()
            db = aw.StateDB(path)
            columns = {
                row[1] for row in db.conn.execute("PRAGMA table_info(sessions)")
            }
            self.assertTrue(
                {
                    "tmux_socket",
                    "last_activity_at",
                    "pane_activity_id",
                    "artifact_activity_id",
                }.issubset(columns)
            )
            db.close()

    def test_claude_status_mapping(self):
        self.assertEqual(aw.claude_status_to_state("shell"), "running")
        self.assertEqual(aw.claude_status_to_state("busy"), "running")
        self.assertEqual(aw.claude_status_to_state("idle"), "ready")
        self.assertEqual(aw.claude_status_to_state("permission"), "needs_input")
        self.assertEqual(aw.claude_status_to_state("surprise"), "unknown")

    def test_codex_lifecycle_uses_latest_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "rollout.jsonl"
            events = [
                {"type": "event_msg", "timestamp": "1", "payload": {"type": "task_complete", "turn_id": "a"}},
                {"type": "event_msg", "timestamp": "2", "payload": {"type": "task_started", "turn_id": "b"}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in events) + "\n")
            self.assertEqual(aw.codex_last_lifecycle(path), ("running", "b", "task_started"))
            with path.open("a") as fh:
                fh.write(json.dumps({"type": "event_msg", "timestamp": "3", "payload": {"type": "task_complete", "turn_id": "b"}}) + "\n")
            self.assertEqual(aw.codex_last_lifecycle(path), ("ready", "b", "task_complete"))

    def test_tmux_prompt_detection(self):
        hit, event_id = aw.pane_input_prompt("work output\nWould you like to run the following command?\nYes, allow once")
        self.assertTrue(hit)
        self.assertTrue(event_id)
        hit, _ = aw.pane_input_prompt("normal output\nWorking (12m 3s)\n")
        self.assertFalse(hit)

    def test_hook_mapping(self):
        obs = aw.hook_to_observation(
            "claude",
            {
                "session_id": "s1",
                "cwd": "/work/repo",
                "hook_event_name": "PermissionRequest",
                "tool_name": "AskUserQuestion",
            },
        )
        self.assertIsNotNone(obs)
        self.assertEqual(obs.state, "needs_input")
        self.assertTrue(obs.key.startswith("claude:s1:attention:"))

        obs = aw.hook_to_observation(
            "codex",
            {"type": "agent-turn-complete", "thread-id": "t1", "turn-id": "turn1", "cwd": "/x"},
        )
        self.assertIsNotNone(obs)
        self.assertEqual(obs.state, "ready")
        self.assertEqual(obs.key, "codex:t1")

    def test_stop_with_background_task_stays_running(self):
        obs = aw.hook_to_observation(
            "claude",
            {
                "session_id": "s1",
                "hook_event_name": "Stop",
                "background_tasks": [{"status": "running"}],
            },
        )
        self.assertIsNotNone(obs)
        self.assertEqual(obs.state, "auto_wait")

    def test_permission_fingerprint_includes_tool_input(self):
        base = {
            "session_id": "s1",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "turn_id": "turn",
        }
        fixed_uuid = mock.Mock(hex="fixed-invocation")
        with mock.patch.object(aw.uuid, "uuid4", return_value=fixed_uuid):
            first = aw.hook_to_observation("claude", {**base, "tool_input": {"command": "one"}})
            second = aw.hook_to_observation("claude", {**base, "tool_input": {"command": "two"}})
        self.assertNotEqual(first.event_id, second.event_id)

    def test_background_completion_does_not_replace_main_session(self):
        obs = aw.hook_to_observation(
            "claude",
            {
                "session_id": "main",
                "hook_event_name": "Notification",
                "notification_type": "agent_completed",
                "message": "reviewer finished",
            },
        )
        self.assertEqual(obs.state, "ready")
        self.assertIn("claude:main:background:", obs.key)

    def test_ready_is_debounced_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="codex:s",
                provider="codex",
                session_id="s",
                pid=1,
                proc_start="1",
                pane_id="%1",
                tmux_target="1:0.0",
                cwd="/repo",
                name="repo",
                state="ready",
                event_id="turn",
                source="test",
                observed_at=time.time(),
            )
            db.upsert(obs)
            config = aw.deep_merge(aw.DEFAULT_CONFIG, {"monitor": {"ready_delay_seconds": 60}})
            self.assertEqual(db.due(config), [])
            db.conn.execute("UPDATE sessions SET state_since=?", (time.time() - 120,))
            db.conn.commit()
            due = db.due(config)
            self.assertEqual(len(due), 1)
            db.mark_notified(due)
            self.assertEqual(db.due(config), [])
            db.close()

    def test_failed_delivery_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="claude:s",
                provider="claude",
                session_id="s",
                pid=2,
                proc_start="2",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="question",
                source="test",
            )
            db.upsert(obs)
            config = aw.deep_merge(aw.DEFAULT_CONFIG, {})
            due = db.due(config)
            self.assertEqual(len(due), 1)
            db.mark_delivery_attempt(due)
            self.assertEqual(db.due(config), [])
            db.close()

    def test_outbox_cas_does_not_mark_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            first = aw.Observation(
                key="codex:s",
                provider="codex",
                session_id="s",
                pid=1,
                proc_start="1",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="ready",
                event_id="A",
                source="test",
                observed_at=time.time() - 100,
            )
            db.upsert(first)
            db.enqueue_session_now(first.key)
            claimed = db.claim_outbox()
            self.assertEqual(len(claimed), 1)
            second = aw.dataclasses.replace(first, event_id="B", observed_at=time.time())
            db.upsert(second)
            db.finish_outbox(claimed, {"console": True}, True)
            row = db.conn.execute("SELECT * FROM sessions WHERE session_key=?", (first.key,)).fetchone()
            self.assertEqual(row["event_id"], "B")
            self.assertEqual(row["notified_event_id"], "")
            db.close()

    def test_outbox_claim_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            first = aw.StateDB(path)
            obs = aw.Observation(
                key="claude:s",
                provider="claude",
                session_id="s",
                pid=2,
                proc_start="2",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="Q",
                source="test",
            )
            first.upsert(obs)
            first.enqueue_session_now(obs.key)
            second = aw.StateDB(path)
            self.assertEqual(len(first.claim_outbox()), 1)
            self.assertEqual(second.claim_outbox(), [])
            first.close()
            second.close()

    def test_immediate_hook_has_only_one_outbox_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="codex:s:attention:q",
                provider="codex",
                session_id="s",
                pid=None,
                proc_start="",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="Q",
                source="codex-hook",
                raw_status="PermissionRequest",
            )
            db.upsert(obs)
            db.enqueue_session_now(obs.key)
            db.enqueue_due(aw.DEFAULT_CONFIG)
            self.assertEqual(db.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
            db.close()

    def test_state_change_cancels_stale_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            first = aw.Observation(
                key="codex:s",
                provider="codex",
                session_id="s",
                pid=1,
                proc_start="1",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="ready",
                event_id="A",
                source="test",
            )
            db.upsert(first)
            db.enqueue_session_now(first.key)
            db.upsert(aw.dataclasses.replace(first, state="running", event_id="B"))
            self.assertEqual(db.claim_outbox(), [])
            item = db.conn.execute("SELECT * FROM outbox").fetchone()
            self.assertIsNotNone(item["sent_at"])
            self.assertIn("superseded", item["delivered_json"])
            db.close()

    def test_remote_failure_is_not_masked_by_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="claude:s",
                provider="claude",
                session_id="s",
                pid=2,
                proc_start="2",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="Q",
                source="test",
            )
            db.upsert(obs)
            db.enqueue_session_now(obs.key)
            config = aw.deep_merge(
                aw.DEFAULT_CONFIG,
                {"notifications": {"webhook": {"url": "https://example.invalid/hook"}}},
            )
            with mock.patch.object(
                aw,
                "send_notifications",
                return_value=({"title": "x"}, {"console": True, "webhook": (False, "HTTP 500")}),
            ), contextlib.redirect_stderr(io.StringIO()):
                aw.notify_due(db, config)
            outbox = db.conn.execute("SELECT * FROM outbox").fetchone()
            session = db.conn.execute("SELECT * FROM sessions").fetchone()
            self.assertIsNone(outbox["sent_at"])
            self.assertEqual(outbox["attempts"], 1)
            self.assertEqual(session["notified_event_id"], "")
            db.close()

    def test_ui_only_configuration_does_not_retry_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            obs = aw.Observation(
                key="codex:ui-only",
                provider="codex",
                session_id="ui-only",
                pid=None,
                proc_start="",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="question",
                source="test",
            )
            db.upsert(obs)
            db.enqueue_session_now(obs.key)
            config = aw.deep_merge(
                aw.DEFAULT_CONFIG,
                {"notifications": {"console": False, "tmux": False, "desktop": False}},
            )
            with mock.patch.object(aw, "send_notifications", return_value=({}, {})):
                aw.notify_due(db, config)
            item = db.conn.execute("SELECT sent_at FROM outbox").fetchone()
            self.assertIsNotNone(item["sent_at"])
            db.close()

    def test_partial_retry_is_grouped_away_from_new_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))

            def observation(key, event):
                return aw.Observation(
                    key=key,
                    provider="claude",
                    session_id=key,
                    pid=None,
                    proc_start="",
                    pane_id="",
                    tmux_target="",
                    cwd="/repo",
                    name="repo",
                    state="needs_input",
                    event_id=event,
                    source="claude-hook",
                    raw_status="PermissionRequest",
                )

            old = observation("claude:old", "old")
            db.upsert(old)
            db.enqueue_session_now(old.key)
            claimed = db.claim_outbox()
            db.finish_outbox(
                claimed,
                {"console": True, "telegram": True, "ntfy": False},
                False,
            )
            db.conn.execute("UPDATE outbox SET available_at=0")
            new = observation("claude:new", "new")
            db.upsert(new)
            db.enqueue_session_now(new.key)
            db.conn.commit()
            config = aw.deep_merge(
                aw.DEFAULT_CONFIG,
                {
                    "notifications": {
                        "ntfy": {"url": "https://ntfy.invalid/topic"},
                        "telegram": {"bot_token": "token", "chat_id": "chat"},
                    }
                },
            )
            calls = []

            def fake_send(
                rows,
                _config,
                skip_channels=None,
                state_dir=aw.DEFAULT_STATE_DIR,
            ):
                del state_dir
                skip = set(skip_channels or set())
                calls.append((sorted(row["session_key"] for row in rows), skip))
                if "telegram" in skip:
                    return {"title": "retry"}, {"ntfy": True}
                return {"title": "new"}, {"console": True, "telegram": True, "ntfy": True}

            with mock.patch.object(aw, "send_notifications", side_effect=fake_send):
                aw.notify_due(db, config)
            self.assertEqual(len(calls), 2)
            retry_call = next(call for call in calls if call[0] == ["claude:old"])
            new_call = next(call for call in calls if call[0] == ["claude:new"])
            self.assertIn("telegram", retry_call[1])
            self.assertNotIn("telegram", new_call[1])
            db.close()

    def test_auto_wait_hook_beats_coarse_idle_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            hook = aw.Observation(
                key="claude:s",
                provider="claude",
                session_id="s",
                pid=None,
                proc_start="",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="auto_wait",
                event_id="cron",
                source="claude-hook",
                raw_status="Stop",
            )
            idle = aw.dataclasses.replace(
                hook,
                pid=2,
                proc_start="2",
                state="ready",
                event_id="idle",
                source="claude-session",
                raw_status="idle",
            )
            db.upsert(hook)
            db.upsert(idle)
            row = db.conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(row["state"], "auto_wait")
            self.assertEqual(row["event_id"], "cron")
            db.close()

    def test_internal_progress_hook_discards_codex_side_session_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            side = aw.Observation(
                key="codex:side-session",
                provider="codex",
                session_id="side-session",
                pid=None,
                proc_start="",
                pane_id="%1",
                tmux_target="1:0.0",
                cwd="/repo",
                name="repo",
                state="running",
                event_id="hook:turn_started:side",
                source="codex-hook",
                raw_status="turn_started",
            )
            attention = aw.dataclasses.replace(
                side,
                key="codex:side-session:attention:test",
                state="needs_input",
                event_id="hook:permission_request:test",
                raw_status="PermissionRequest",
            )
            db.upsert(side)
            db.upsert(attention)
            db.enqueue_session_now(attention.key)
            reply = aw.dataclasses.replace(
                side,
                state="ready",
                event_id="hook:stop:reply",
                raw_status="Stop",
                message=(
                    "AWP0123456789AB|ship feature|parser done|render UI|"
                    "run tests|none|END"
                ),
            )
            aw.apply_hook_observation(db, reply)
            self.assertEqual(db.sessions(), [])
            self.assertEqual(
                db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
            )
            db.close()

    def test_internal_progress_purge_removes_only_exact_codex_probe_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            base = aw.Observation(
                key="codex:old-side",
                provider="codex",
                session_id="old-side",
                pid=None,
                proc_start="",
                pane_id="%1",
                tmux_target="1:0.0",
                cwd="/repo",
                name="repo",
                state="ready",
                event_id="hook:stop:old",
                source="codex-hook",
                raw_status="Stop",
                message=(
                    "AWPABCDEF012345|goal|done|current|next|none|END"
                ),
            )
            normal = aw.dataclasses.replace(
                base,
                key="codex:normal",
                session_id="normal",
                message="AWPABCDEF012345 is ordinary user-visible text",
            )
            claude = aw.dataclasses.replace(
                base,
                key="claude:same-shape",
                provider="claude",
                session_id="same-shape",
                source="claude-hook",
            )
            db.upsert(base)
            db.upsert(normal)
            db.upsert(claude)
            self.assertEqual(db.purge_internal_progress_sessions(), 1)
            self.assertEqual(
                {row["session_key"] for row in db.sessions()},
                {"codex:normal", "claude:same-shape"},
            )
            db.close()

    def test_canonical_session_removes_pid_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = aw.StateDB(pathlib.Path(tmp))
            alias = aw.Observation(
                key="codex:pid-10-20",
                provider="codex",
                session_id="pid-10-20",
                pid=10,
                proc_start="20",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="running",
                event_id="old",
                source="process",
            )
            canonical = aw.dataclasses.replace(
                alias, key="codex:real", session_id="real", event_id="new"
            )
            db.upsert(alias)
            db.upsert(canonical)
            keys = [row[0] for row in db.conn.execute("SELECT session_key FROM sessions")]
            self.assertEqual(keys, ["codex:real"])
            db.close()

    def test_install_hook_merge_preserves_existing(self):
        settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo ok"}]}]}}
        self.assertTrue(aw.add_hook(settings, "Stop", "claude"))
        self.assertFalse(aw.add_hook(settings, "Stop", "claude"))
        self.assertIn("SessionStart", settings["hooks"])
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)

    def test_install_hook_reconciles_existing_handler(self):
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "wrong",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/agent-watch hook --source claude",
                                "timeout": 99,
                                "async": True,
                            }
                        ],
                    }
                ]
            }
        }
        self.assertTrue(aw.add_hook(settings, "Stop", "claude", None, False))
        group = settings["hooks"]["Stop"][0]
        self.assertNotIn("matcher", group)
        self.assertNotIn("async", group["hooks"][0])
        self.assertEqual(group["hooks"][0]["timeout"], 10)

    def test_http_redirect_is_not_followed(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            final_hits = 0

            def do_POST(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                else:
                    type(self).final_hits += 1
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ok, detail = aw.http_json(
                f"http://127.0.0.1:{server.server_port}/start",
                {"test": True},
                2,
                {"Authorization": "Bearer secret"},
            )
            self.assertFalse(ok)
            self.assertEqual(detail, "HTTP 302")
            self.assertEqual(Handler.final_hits, 0)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_test_notification_does_not_consume_pending_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            db = aw.StateDB(state)
            obs = aw.Observation(
                key="codex:real",
                provider="codex",
                session_id="real",
                pid=1,
                proc_start="1",
                pane_id="",
                tmux_target="",
                cwd="/repo",
                name="repo",
                state="needs_input",
                event_id="pending",
                source="test",
            )
            db.upsert(obs)
            db.close()
            args = Namespace(state_dir=str(state), provider="codex", kind="needs_input")
            with mock.patch.object(
                aw, "send_notifications", return_value=({"title": "test"}, {"console": True})
            ), contextlib.redirect_stdout(io.StringIO()):
                aw.test_notification(args, aw.DEFAULT_CONFIG)
            db = aw.StateDB(state)
            row = db.conn.execute("SELECT * FROM sessions WHERE session_key='codex:real'").fetchone()
            self.assertEqual(row["notified_event_id"], "")
            self.assertEqual(row["event_id"], "pending")
            db.close()


if __name__ == "__main__":
    unittest.main()
