import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_watch import core as aw
from agent_watch import dashboard as ui


def make_row(key, state, project, provider="codex", when=None):
    now = time.time() if when is None else when
    return {
        "session_key": key,
        "provider": provider,
        "session_id": key,
        "pid": 1,
        "proc_start": "1",
        "pane_id": "%1",
        "tmux_target": "1:0.0",
        "tmux_socket": "/tmp/tmux-0/default",
        "cwd": f"/work/{project}",
        "name": project,
        "state": state,
        "state_since": now,
        "last_seen": now,
        "last_activity_at": now,
        "event_id": key,
        "source": "codex-rollout",
        "raw_status": state,
        "message": "",
    }


class DashboardTests(unittest.TestCase):
    def test_readme_screenshot_is_reproducible_synthetic_data(self):
        committed = ROOT / "docs" / "agent-watch-demo.svg"
        with tempfile.TemporaryDirectory() as tmp:
            generated = pathlib.Path(tmp) / "demo.svg"
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "render-demo.py"), str(generated)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(generated.read_bytes(), committed.read_bytes())
        text = committed.read_text()
        self.assertIn("checkout-api", text)
        self.assertNotIn("/root", text)
        self.assertNotIn("zhiqi-li", text)
        self.assertNotIn("cdnjs.cloudflare.com", text)
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in text))

    def test_snapshot_distinguishes_live_daemon_from_failed_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            db = aw.StateDB(state)
            db.set_meta("heartbeat", "123")
            db.set_meta("last_success", "1 session")
            db.set_meta("last_error", "provider parser failed")
            snapshot = ui.load_snapshot(state, heartbeat_max_age=60)
            self.assertTrue(snapshot.daemon_alive)
            self.assertEqual(snapshot.last_scan_error, "provider parser failed")
            db.set_meta("last_success", "1 session")
            snapshot = ui.load_snapshot(state, heartbeat_max_age=60)
            self.assertEqual(snapshot.last_scan_error, "")
            db.close()

    def test_codex_preview_excludes_reasoning_and_tool_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "rollout.jsonl"
            records = [
                {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "user_message", "message": "VISIBLE_USER"}},
                {"type": "response_item", "timestamp": "2026-01-01T00:00:01Z", "payload": {"type": "reasoning", "summary": "UNIQUE_SECRET_REASONING"}},
                {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z", "payload": {"type": "custom_tool_call", "name": "exec", "input": "UNIQUE_SECRET_COMMAND"}},
                {"type": "response_item", "timestamp": "2026-01-01T00:00:03Z", "payload": {"type": "custom_tool_call_output", "output": "UNIQUE_SECRET_OUTPUT"}},
                {"type": "event_msg", "timestamp": "2026-01-01T00:00:04Z", "payload": {"type": "agent_message", "message": "VISIBLE_ASSISTANT"}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            preview = ui.extract_codex_preview(path)
            serialized = json.dumps(preview)
            self.assertEqual(preview["user"]["text"], "VISIBLE_USER")
            self.assertEqual(preview["assistant"]["text"], "VISIBLE_ASSISTANT")
            self.assertEqual(preview["tool"]["text"], "exec")
            self.assertNotIn("UNIQUE_SECRET", serialized)

    def test_claude_preview_excludes_tool_results_thinking_and_sidechains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "session.jsonl"
            session_id = "session-123"
            records = [
                {"type": "user", "sessionId": session_id, "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "VISIBLE_USER"}},
                {"type": "user", "sessionId": session_id, "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "user", "content": [{"type": "tool_result", "content": "UNIQUE_SECRET_RESULT"}]}},
                {"type": "assistant", "sessionId": session_id, "isSidechain": True, "timestamp": "2026-01-01T00:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "UNIQUE_SECRET_SIDECHAIN"}]}},
                {"type": "assistant", "sessionId": session_id, "timestamp": "2026-01-01T00:00:03Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "UNIQUE_SECRET_THINKING"}, {"type": "text", "text": "VISIBLE_ASSISTANT"}, {"type": "tool_use", "name": "Bash", "input": {"command": "UNIQUE_SECRET_COMMAND"}}]}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            preview = ui.extract_claude_preview(path, session_id)
            serialized = json.dumps(preview)
            self.assertEqual(preview["user"]["text"], "VISIBLE_USER")
            self.assertEqual(preview["assistant"]["text"], "VISIBLE_ASSISTANT")
            self.assertEqual(preview["tool"]["text"], "Bash")
            self.assertNotIn("UNIQUE_SECRET", serialized)

    def test_context_markdown_cleanup_keeps_values_and_drops_table_noise(self):
        cleaned = ui.clean_context_text(
            "### Results\n| model | score |\n|---|---:|\n| Muon | **77.04%** |\n"
            "See [report](/secret/very/long/path)."
        )
        self.assertIn("Results", cleaned)
        self.assertIn("Muon · 77.04%", cleaned)
        self.assertIn("report", cleaned)
        self.assertNotIn("---", cleaned)
        self.assertNotIn("/secret/very/long/path", cleaned)

    def test_preview_loader_rejects_symlink_outside_history_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            root = home / ".codex" / "sessions" / "2026" / "01" / "01"
            root.mkdir(parents=True)
            session_id = "abc-session"
            outside = pathlib.Path(tmp) / f"rollout-{session_id}.jsonl"
            outside.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": session_id}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "SHOULD_NOT_LOAD"}})
                + "\n"
            )
            (root / f"rollout-{session_id}.jsonl").symlink_to(outside)
            loader = ui.ConversationPreviewLoader(home=home)
            preview = loader.load(
                {"provider": "codex", "session_id": session_id, "pid": None}
            )
            self.assertEqual(preview, {})

    def test_preview_loader_reads_valid_rollout_once_then_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            root = home / ".codex" / "sessions" / "2026" / "01" / "01"
            root.mkdir(parents=True)
            session_id = "valid-session"
            path = root / f"rollout-{session_id}.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {"type": "session_meta", "payload": {"id": session_id}},
                        {"type": "event_msg", "payload": {"type": "user_message", "message": "VISIBLE"}},
                    )
                )
                + "\n"
            )
            before = path.stat().st_mtime_ns
            loader = ui.ConversationPreviewLoader(home=home, refresh_seconds=60)
            row = {"provider": "codex", "session_id": session_id, "pid": None}
            with mock.patch.object(
                ui,
                "extract_codex_preview",
                wraps=ui.extract_codex_preview,
            ) as extract:
                first = loader.load(row)
                second = loader.load(row)
            self.assertEqual(first["user"]["text"], "VISIBLE")
            self.assertEqual(second, first)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(path.stat().st_mtime_ns, before)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_context_preview_render_strips_terminal_controls(self):
        from rich.console import Console

        row = make_row("context", "running", "context")
        snapshot = ui.DashboardSnapshot(sessions=[row], daemon_alive=True)
        view = ui.DashboardView(snapshot)
        view.set_context(
            "context",
            {
                "user": {"text": "hello\x1b[31m red\x1b[0m\u202e", "at": time.time()},
                "assistant": {"text": "[bold red]literal[/]", "at": time.time()},
            },
        )
        output = io.StringIO()
        console = Console(
            file=output, width=56, height=34, force_terminal=False, color_system=None
        )
        console.print(ui.render_detail(view, 56, 34))
        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("[bold red]literal[/]", rendered)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_context_preview_reserves_action_and_does_not_mislabel_old_tool(self):
        from rich.console import Console

        now = time.time()
        row = make_row("context", "running", "context")
        snapshot = ui.DashboardSnapshot(sessions=[row], daemon_alive=True)
        view = ui.DashboardView(snapshot)
        view.set_context(
            "context",
            {
                "user": {"text": "new request", "at": now},
                "assistant": {"text": "long progress " * 500, "at": now + 1},
                "tool": {"text": "Bash", "at": now - 30},
            },
        )
        output = io.StringIO()
        console = Console(
            file=output, width=56, height=35, force_terminal=False, color_system=None
        )
        console.print(ui.render_detail(view, 56, 35))
        rendered = output.getvalue()
        self.assertIn("Last action", rendered)
        self.assertNotIn("Current action", rendered)
        self.assertIn("Enter to open this tmux session", rendered)
        self.assertIn("tmux prefix + L", rendered)
        self.assertIn("to return", rendered)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_help_explains_how_to_return_from_tmux_session(self):
        from rich.console import Console

        output = io.StringIO()
        console = Console(
            file=output, width=90, force_terminal=False, color_system=None
        )
        console.print(ui.render_help(90))
        self.assertIn("tmux prefix + L", output.getvalue())
        self.assertIn("Return to Agent Watch", output.getvalue())

    def test_sanitize_removes_terminal_controls(self):
        value = ui.sanitize("safe\x1b[31m red\x1b[0m\x1b]0;title\x07\u202erev\x7f")
        self.assertNotIn("\x1b", value)
        self.assertNotIn("\u202e", value)
        self.assertTrue(all(character.isprintable() for character in value))

    def test_stalled_running_session_is_promoted_and_rendered(self):
        from rich.console import Console

        now = time.time()
        stalled = make_row("stalled", "running", "stalled", when=now - 1800)
        stalled["last_activity_at"] = now - 1200
        snapshot = ui.DashboardSnapshot(
            sessions=[stalled, make_row("ready", "ready", "ready")],
            daemon_alive=True,
            activity_stale_seconds=600,
        )
        self.assertTrue(ui.is_stalled(stalled, 600, now))
        self.assertIn(stalled, ui.visible_sessions(snapshot, "attention"))
        output = io.StringIO()
        console = Console(
            file=output, width=140, height=40, force_terminal=False, color_system=None
        )
        console.print(ui.render_dashboard(ui.DashboardView(snapshot), 140, 40))
        self.assertIn("no update", output.getvalue())
        self.assertIn("possibly stalled", output.getvalue())

    def test_switch_targets_unique_tmux_client_and_exact_pane(self):
        row = make_row("agent", "running", "agent")
        completed = ui.subprocess.CompletedProcess
        results = [
            completed([], 0, stdout="1:0.0\n", stderr=""),
            completed([], 0, stdout="/dev/pts/7|%dash\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed([], 0, stdout="", stderr=""),
        ]
        with mock.patch.dict(
            ui.os.environ,
            {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
            clear=False,
        ), mock.patch.object(ui.subprocess, "run", side_effect=results) as run:
            ok, message = ui.switch_to_session(row)
        self.assertTrue(ok, message)
        self.assertEqual(
            run.call_args_list[-2].args[0],
            [
                "tmux",
                "-S",
                "/tmp/tmux-0/default",
                "switch-client",
                "-c",
                "/dev/pts/7",
                "-t",
                "%1",
            ],
        )
        self.assertEqual(
            run.call_args_list[-1].args[0],
            [
                "tmux",
                "-S",
                "/tmp/tmux-0/default",
                "display-message",
                "-c",
                "/dev/pts/7",
                "-d",
                "5000",
                "Agent Watch: press tmux prefix + L to return",
            ],
        )

    def test_switch_fails_closed_for_ambiguous_clients(self):
        row = make_row("agent", "running", "agent")
        completed = ui.subprocess.CompletedProcess
        results = [
            completed([], 0, stdout="1:0.0\n", stderr=""),
            completed([], 0, stdout="/dev/pts/7|%dash\n/dev/pts/8|%dash\n", stderr=""),
        ]
        with mock.patch.dict(
            ui.os.environ,
            {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
            clear=False,
        ), mock.patch.object(ui.subprocess, "run", side_effect=results):
            ok, message = ui.switch_to_session(row)
        self.assertFalse(ok)
        self.assertIn("Multiple clients", message)

    def test_switch_rejects_cross_socket_with_quoted_command(self):
        row = make_row("agent", "running", "agent")
        row["tmux_socket"] = "/tmp/custom socket"
        completed = ui.subprocess.CompletedProcess
        with mock.patch.dict(
            ui.os.environ,
            {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
            clear=False,
        ), mock.patch.object(
            ui.subprocess,
            "run",
            return_value=completed([], 0, stdout="1:0.0\n", stderr=""),
        ) as run:
            ok, message = ui.switch_to_session(row)
        self.assertFalse(ok)
        self.assertIn("another tmux server", message)
        self.assertIn("'/tmp/custom socket'", message)
        self.assertEqual(run.call_count, 1)

    def test_switch_outside_tmux_attaches_exact_pane(self):
        row = make_row("agent", "running", "agent")
        completed = ui.subprocess.CompletedProcess
        results = [
            completed([], 0, stdout="1:0.0\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
        ]
        environment = dict(ui.os.environ)
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        with mock.patch.dict(ui.os.environ, environment, clear=True), mock.patch.object(
            ui.subprocess, "run", side_effect=results
        ) as run:
            ok, message = ui.switch_to_session(row)
        self.assertTrue(ok, message)
        self.assertEqual(
            run.call_args_list[-1].args[0],
            [
                "tmux",
                "-S",
                "/tmp/tmux-0/default",
                "attach-session",
                "-t",
                "%1",
            ],
        )

    def test_switch_rejects_exited_session_last_location(self):
        row = make_row("gone", "exited", "gone")
        with mock.patch.object(ui.subprocess, "run") as run:
            ok, message = ui.switch_to_session(row)
        self.assertFalse(ok)
        self.assertIn("last location", message)
        run.assert_not_called()

    def test_priority_and_filtering(self):
        snapshot = ui.DashboardSnapshot(
            sessions=[
                make_row("run", "running", "run"),
                make_row("ready", "ready", "ready"),
                make_row("input", "needs_input", "input"),
                make_row("error", "error", "error"),
            ]
        )
        ordered = ui.visible_sessions(snapshot)
        self.assertEqual([row["session_key"] for row in ordered], ["input", "error", "ready", "run"])
        attention = ui.visible_sessions(snapshot, "attention")
        self.assertEqual([row["session_key"] for row in attention], ["input", "error", "ready"])
        searched = ui.visible_sessions(snapshot, query="READY")
        self.assertEqual([row["session_key"] for row in searched], ["ready"])

    def test_selection_survives_refresh_and_reorder(self):
        first = ui.DashboardSnapshot(
            sessions=[make_row("a", "running", "a"), make_row("b", "ready", "b")]
        )
        view = ui.DashboardView(first)
        view.selected_key = "a"
        view._ensure_selection()
        second = ui.DashboardSnapshot(
            sessions=[make_row("a", "needs_input", "a"), make_row("b", "ready", "b")]
        )
        view.update(second)
        self.assertEqual(view.selected["session_key"], "a")

    def test_cjk_cell_width_and_crop(self):
        self.assertEqual(ui.cell_width("ab한"), 4)
        self.assertEqual(ui.crop_cells("ab한글", 4), "ab한")
        shortened = ui.shorten_middle("매우긴프로젝트이름-with-tail", 12)
        self.assertLessEqual(ui.cell_width(shortened), 12)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_render_full_and_narrow_layouts(self):
        from rich.console import Console

        snapshot = ui.DashboardSnapshot(
            sessions=[
                make_row("input", "needs_input", "needs-reply-project", "claude"),
                make_row("run", "running", "running-project"),
            ],
            daemon_alive=True,
            daemon_pid="123",
        )
        for width, height in ((140, 43), (90, 30), (60, 24), (40, 12)):
            view = ui.DashboardView(snapshot)
            output = io.StringIO()
            console = Console(
                file=output,
                width=width,
                height=height,
                force_terminal=False,
                color_system=None,
            )
            console.print(ui.render_dashboard(view, width, height))
            rendered = output.getvalue()
            self.assertIn("agent-watch", rendered)
            self.assertIn("needs-reply-project", rendered)
            self.assertIn("tmux 1:0.0", rendered)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_session_columns_stay_vertically_aligned(self):
        from rich.console import Console

        now = time.time()
        rows = [
            make_row("a", "ready", "short", when=now - 9),
            make_row("b", "ready", "a-much-longer-project", when=now - 3600),
            make_row("c", "running", "middle", when=now - 65),
        ]
        no_tmux = make_row(
            "d", "running", "extremely-long-project-name-that-overflows", when=now - 65
        )
        no_tmux.update({"pane_id": "", "tmux_target": "", "tmux_socket": ""})
        rows.append(no_tmux)
        snapshot = ui.DashboardSnapshot(sessions=rows, daemon_alive=True)
        for width in (84, 60):
            output = io.StringIO()
            console = Console(
                file=output,
                width=width,
                height=24,
                force_terminal=False,
                color_system=None,
            )
            console.print(ui.render_sessions(ui.DashboardView(snapshot), width, 24))
            tmux_lines = [
                line for line in output.getvalue().splitlines() if "tmux 1:0.0" in line
            ]
            self.assertEqual(len(tmux_lines), 3)
            self.assertEqual(len({line.index("tmux") for line in tmux_lines}), 1)
            activity_lines = [
                line for line in output.getvalue().splitlines() if "updated" in line
            ]
            self.assertEqual(len(activity_lines), 2)
            self.assertEqual(len({line.index("updated") for line in activity_lines}), 1)
            self.assertTrue(
                all(line[line.index("updated") - 1].isspace() for line in activity_lines)
            )

    def test_key_handling(self):
        snapshot = ui.DashboardSnapshot(
            sessions=[make_row("a", "needs_input", "a"), make_row("b", "running", "b")]
        )
        view = ui.DashboardView(snapshot)
        ui.handle_key(view, "down", "", 10)
        self.assertEqual(view.selected["session_key"], "b")
        ui.handle_key(view, "text", "/", 10)
        self.assertTrue(view.searching)
        ui.handle_key(view, "text", "a", 10)
        self.assertEqual(view.query, "a")
        ui.handle_key(view, "escape", "", 10)
        self.assertFalse(view.searching)
        self.assertEqual(view.query, "")
        self.assertEqual(ui.handle_key(view, "text", "q", 10), "quit")

    def test_missing_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "does-not-exist"
            snapshot = ui.load_snapshot(state)
            self.assertTrue(snapshot.error)
            self.assertFalse(state.exists())

    def test_load_snapshot_from_monitor_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            db = aw.StateDB(state)
            obs = aw.Observation(
                key="claude:s",
                provider="claude",
                session_id="s",
                pid=2,
                proc_start="2",
                pane_id="%2",
                tmux_target="2:0.0",
                cwd="/work/demo",
                name="demo",
                state="ready",
                event_id="done",
                source="claude-session",
            )
            db.upsert(obs)
            db.set_meta("heartbeat", "999")
            db.close()
            before = (state / "state.sqlite3").stat().st_mtime_ns
            snapshot = ui.load_snapshot(state)
            after = (state / "state.sqlite3").stat().st_mtime_ns
            self.assertEqual(len(snapshot.sessions), 1)
            self.assertTrue(snapshot.daemon_alive)
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
