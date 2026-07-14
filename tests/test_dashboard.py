import contextlib
import importlib.util
import io
import json
import pathlib
import shlex
import stat
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
from agent_watch import resume


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


def write_resume_artifact(home, row):
    session_id = row["session_id"]
    if row["provider"] == "codex":
        path = (
            pathlib.Path(home)
            / ".codex"
            / "sessions"
            / "2026"
            / "07"
            / "07"
            / f"rollout-test-{session_id}.jsonl"
        )
        item = {"type": "session_meta", "payload": {"id": session_id}}
    else:
        path = pathlib.Path(home) / ".claude" / "projects" / "-work-demo" / f"{session_id}.jsonl"
        item = {"type": "user", "sessionId": session_id}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item) + "\n")
    return path


class DashboardTests(unittest.TestCase):
    def test_rich_environment_repairs_dumb_term_from_terminal_emulator(self):
        environ = ui.rich_console_environment(
            {"TERM": "dumb", "TERM_PROGRAM": "vscode", "NO_COLOR": "1"}
        )
        self.assertEqual(environ["TERM"], "xterm-256color")
        self.assertEqual(environ["NO_COLOR"], "1")

    def test_rich_environment_preserves_real_dumb_terminal(self):
        environ = ui.rich_console_environment({"TERM": "dumb"})
        self.assertEqual(environ["TERM"], "dumb")

    def test_rich_environment_preserves_existing_terminal_type(self):
        environ = ui.rich_console_environment(
            {"TERM": "screen-256color", "TERM_PROGRAM": "vscode"}
        )
        self.assertEqual(environ["TERM"], "screen-256color")

    def test_readme_screenshot_is_reproducible_synthetic_data(self):
        committed = ROOT / "docs" / "agent-watch-demo.svg"
        with tempfile.TemporaryDirectory() as tmp:
            generated = pathlib.Path(tmp) / "demo.svg"
            run = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "render-demo.py"),
                    str(generated),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(generated.read_bytes(), committed.read_bytes())
        text = committed.read_text()
        self.assertIn("checkout-api", text)
        self.assertIn("Exited&#160;sessions", text)
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
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"type": "user_message", "message": "VISIBLE_USER"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "reasoning",
                        "summary": "UNIQUE_SECRET_REASONING",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "input": "UNIQUE_SECRET_COMMAND",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "output": "UNIQUE_SECRET_OUTPUT",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:04Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "VISIBLE_ASSISTANT",
                    },
                },
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
                {
                    "type": "user",
                    "sessionId": session_id,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "VISIBLE_USER"},
                },
                {
                    "type": "user",
                    "sessionId": session_id,
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "UNIQUE_SECRET_RESULT"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "isSidechain": True,
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "UNIQUE_SECRET_SIDECHAIN"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "UNIQUE_SECRET_THINKING"},
                            {"type": "text", "text": "VISIBLE_ASSISTANT"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "UNIQUE_SECRET_COMMAND"},
                            },
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            preview = ui.extract_claude_preview(path, session_id)
            serialized = json.dumps(preview)
            self.assertEqual(preview["user"]["text"], "VISIBLE_USER")
            self.assertEqual(preview["assistant"]["text"], "VISIBLE_ASSISTANT")
            self.assertEqual(preview["tool"]["text"], "Bash")
            self.assertNotIn("UNIQUE_SECRET", serialized)

    def test_progress_capture_is_provider_neutral_and_cleans_wrapped_borders(self):
        marker = "AWPABC123"
        capture = (
            f"prompt ends with marker {marker}\n"
            f"│ {marker} | 统一展示任务进度 | 完成解析层 │ | 编写 UI | "
            "运行完整测试 | none | END │\n"
        )
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                summary = ui.parse_progress_capture(capture, marker, provider)
                self.assertIsNotNone(summary)
                assert summary is not None
                self.assertEqual(summary.provider, provider)
                self.assertEqual(summary.goal, "统一展示任务进度")
                self.assertEqual(summary.done, "完成解析层")
                self.assertEqual(summary.current, "编写 UI")
                self.assertEqual(summary.next, "运行完整测试")
                self.assertEqual(summary.blocker, "")

    def test_progress_probe_supports_attached_drafts_with_same_btw_protocol(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        for provider in ("codex", "claude"):
            row = make_row(f"{provider}-progress", "running", provider, provider)
            composer = (
                "\x1b[1m›\x1b[0m DRAFTMARK"
                if provider == "codex"
                else "\x1b[39m❯\u00a0DRAFTMARK"
            )
            results = [
                completed([], 0, stdout="1:0.0|0|0\n", stderr=""),
                completed([], 0, stdout="%1\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="2|1\n", stderr=""),
                completed([], 0, stdout=f"header\n{composer}\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed(
                    [],
                    0,
                    stdout=(
                        f"› {ui._progress_prompt(marker)} DRAFTMARK\n"
                        "  tab to queue message\n"
                    ),
                    stderr="",
                ),
                completed([], 0, stdout="", stderr=""),
                completed(
                    [],
                    0,
                    stdout=(
                        f"{marker}|ship feature|parser done|render UI|run tests|none|END\n"
                    ),
                    stderr="",
                ),
                completed(
                    [],
                    0,
                    stdout=(
                        f"Side from main thread · Ctrl+C to return\n{marker}\n"
                        if provider == "codex"
                        else f"Press Space to dismiss\n{marker}\n"
                    ),
                    stderr="",
                ),
                completed([], 0, stdout="", stderr=""),
            ]
            with (
                self.subTest(provider=provider),
                mock.patch.object(ui.secrets, "token_hex", return_value="0123456789ab"),
                mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
            ):
                result = ui.probe_session_progress(row, timeout=1, poll_seconds=0.01)
            self.assertEqual(result.error, "")
            self.assertIsNotNone(result.summary)
            assert result.summary is not None
            self.assertEqual(result.summary.provider, provider)
            self.assertEqual(result.summary.current, "render UI")
            position_command = run.call_args_list[2].args[0]
            self.assertEqual(position_command[-1], "Home")
            send_command = run.call_args_list[5].args[0]
            self.assertIn("send-keys", send_command)
            prompt = send_command[send_command.index("-l") + 1]
            self.assertTrue(prompt.startswith("/btw "))
            self.assertTrue(prompt.endswith(" "))
            self.assertIn(marker, prompt)
            self.assertNotIn(";", send_command)
            submit_command = run.call_args_list[7].args[0]
            self.assertEqual(submit_command[-1], "Enter")
            expected_dismiss = "C-c" if provider == "codex" else "Space"
            dismiss_calls = [
                call
                for call in run.call_args_list
                if call.args[0][-1] == expected_dismiss
            ]
            self.assertEqual(len(dismiss_calls), 1)

    def test_codex_side_dismissal_waits_for_hint_and_sends_only_once(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        results = [
            completed([], 0, stdout=f"side loading\n{marker}\n", stderr=""),
            completed(
                [],
                0,
                stdout=(
                    "Side from main thread · main finished · Ctrl+C to return\n"
                    f"{marker}|goal|done|now|next|none|END\n"
                ),
                stderr="",
            ),
            completed([], 0, stdout="", stderr=""),
        ]
        with (
            mock.patch.object(ui.time, "sleep"),
            mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
        ):
            self.assertTrue(
                ui._dismiss_progress_side_ui(["tmux"], "%1", marker, "codex")
            )
        dismiss_calls = [
            call for call in run.call_args_list if call.args[0][-1] == "C-c"
        ]
        self.assertEqual(len(dismiss_calls), 1)

    def test_stale_marker_without_exit_hint_never_sends_ctrl_c(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        results = [
            completed([], 0, stdout=f"main conversation\n{marker}\n", stderr="")
            for _attempt in range(5)
        ]
        with (
            mock.patch.object(ui.time, "sleep"),
            mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
        ):
            self.assertFalse(
                ui._dismiss_progress_side_ui(["tmux"], "%1", marker, "codex")
            )
        self.assertFalse(
            any(call.args[0][-1] == "C-c" for call in run.call_args_list)
        )

    def test_late_progress_cleanup_waits_for_hint_and_sends_only_once(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        results = [
            completed([], 0, stdout=f"side loading\n{marker}\n", stderr=""),
            completed(
                [],
                0,
                stdout=f"Side from main thread · Ctrl+C to return\n{marker}\n",
                stderr="",
            ),
            completed(
                [],
                0,
                stdout=f"Side from main thread · Ctrl+C to return\n{marker}\n",
                stderr="",
            ),
            completed([], 0, stdout="", stderr=""),
        ]
        with mock.patch.object(ui.subprocess, "run", side_effect=results) as run:
            self.assertTrue(
                ui._watch_late_progress_side_cleanup(
                    ["tmux"], "%1", marker, "codex", timeout=1, poll_seconds=0.01
                )
            )
        dismiss_calls = [
            call for call in run.call_args_list if call.args[0][-1] == "C-c"
        ]
        self.assertEqual(len(dismiss_calls), 1)

    def test_late_cleanup_never_uses_a_stale_side_hint_from_history(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        history = completed(
            [],
            0,
            stdout=f"Side from main thread · Ctrl+C to return\n{marker}\n",
            stderr="",
        )
        main = completed([], 0, stdout=f"main conversation\n{marker}\n", stderr="")
        with (
            mock.patch.object(ui.time, "sleep"),
            mock.patch.object(
                ui.subprocess, "run", side_effect=[history, main, main, main, main, main]
            ) as run,
        ):
            self.assertFalse(
                ui._watch_late_progress_side_cleanup(
                    ["tmux"], "%1", marker, "codex", timeout=1
                )
            )
        self.assertFalse(
            any(call.args[0][-1] == "C-c" for call in run.call_args_list)
        )

    def test_progress_timeout_starts_bounded_late_cleanup(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        row = make_row("codex-progress", "running", "codex")
        results = [
            completed([], 0, stdout="1:0.0|0|0\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed([], 0, stdout="", stderr=""),
        ]
        rendered = completed([], 0, stdout=f"› prompt {marker}\n", stderr="")
        with (
            mock.patch.object(ui.secrets, "token_hex", return_value="0123456789ab"),
            mock.patch.object(ui.time, "monotonic", side_effect=[0, 0, 0.1, 2]),
            mock.patch.object(ui.subprocess, "run", side_effect=results),
            mock.patch.object(ui, "_capture_progress_pane", return_value=rendered),
            mock.patch.object(ui, "_clear_progress_prompt_draft"),
            mock.patch.object(ui, "_start_late_progress_side_cleanup") as cleanup,
        ):
            result = ui.probe_session_progress(row, timeout=1, poll_seconds=0.01)
        self.assertIn("before the timeout", result.error)
        cleanup.assert_called_once_with(
            ["tmux", "-S", "/tmp/tmux-0/default"], "%1", marker, "codex"
        )

    def test_late_progress_cleanup_thread_is_bounded_daemon(self):
        with mock.patch.object(ui.threading, "Thread") as thread:
            ui._start_late_progress_side_cleanup(
                ["tmux"], "%1", "AWP0123456789AB", "codex"
            )
        thread.assert_called_once_with(
            target=ui._watch_late_progress_side_cleanup,
            args=(("tmux",), "%1", "AWP0123456789AB", "codex"),
            name="agent-watch-progress-cleanup",
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_progress_probe_clears_verified_draft_when_submit_fails(self):
        completed = subprocess.CompletedProcess
        marker = "AWP0123456789AB"
        prompt = ui._progress_prompt(marker)
        wrapped_prompt = prompt.replace("current|next", "current|\n  next")
        row = make_row("codex-progress", "running", "codex")
        results = [
            completed([], 0, stdout="1:0.0|0|0\n", stderr=""),
            completed([], 0, stdout="%dashboard\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed(
                [],
                0,
                stdout=f"› {wrapped_prompt}\n  tab to queue message\n",
                stderr="",
            ),
            completed([], 1, stdout="", stderr="submit failed"),
            completed(
                [],
                0,
                stdout=f"› {wrapped_prompt}\n  tab to queue message\n",
                stderr="",
            ),
            completed([], 0, stdout="", stderr=""),
        ]
        with (
            mock.patch.object(ui.secrets, "token_hex", return_value="0123456789ab"),
            mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
        ):
            result = ui.probe_session_progress(row, timeout=1, poll_seconds=0.01)
        self.assertEqual(result.error, "submit failed")
        cleanup_command = run.call_args_list[-1].args[0]
        self.assertEqual(cleanup_command[-1], "BSpace")
        self.assertEqual(
            cleanup_command[cleanup_command.index("-N") + 1], str(len(prompt))
        )

    def test_progress_probe_refuses_active_pane_without_composer_start(self):
        row = make_row("visible-progress", "running", "visible")
        completed = subprocess.CompletedProcess
        results = [
            completed([], 0, stdout="1:0.0|0|0\n", stderr=""),
            completed([], 0, stdout="%1\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed([], 0, stdout="0|1\n", stderr=""),
        ]
        with mock.patch.object(ui.subprocess, "run", side_effect=results) as run:
            result = ui.probe_session_progress(row, timeout=1)
        self.assertIn("active pane", result.error)
        self.assertIn("could not be positioned safely", result.error)
        self.assertEqual(run.call_count, 4)
        self.assertFalse(
            any("-l" in call.args[0] for call in run.call_args_list)
        )

    def test_progress_probe_allows_active_and_ready_supported_sessions(self):
        for state in ("running", "auto_wait", "ready"):
            with self.subTest(state=state):
                self.assertEqual(
                    ui.progress_probe_availability(
                        make_row(f"claude-{state}", state, "demo", "claude")
                    ),
                    "",
                )
        for state in ("needs_input", "error"):
            with self.subTest(state=state):
                self.assertIn(
                    "running, auto-wait, or ready",
                    ui.progress_probe_availability(
                        make_row(f"codex-{state}", state, "demo")
                    ),
                )
        unsupported = make_row("other", "running", "demo", "other")
        self.assertIn("Only Codex and Claude", ui.progress_probe_availability(unsupported))

    def test_composer_state_distinguishes_placeholders_from_drafts(self):
        completed = subprocess.CompletedProcess
        cases = (
            (
                "codex-empty",
                "codex",
                "\x1b[1m›\x1b[0m \x1b[2mImprove documentation\x1b[0m",
                "empty",
            ),
            ("codex-draft-at-home", "codex", "\x1b[1m›\x1b[0m DRAFTMARK", "draft"),
            ("claude-empty", "claude", "\x1b[39m❯\u00a0", "empty"),
            ("claude-draft-at-home", "claude", "\x1b[39m❯\u00a0DRAFTMARK", "draft"),
        )
        for name, provider, composer_line, expected in cases:
            with self.subTest(name=name):
                results = [
                    completed([], 0, stdout="2|1\n", stderr=""),
                    completed([], 0, stdout=f"header\n{composer_line}\n", stderr=""),
                ]
                with mock.patch.object(ui.subprocess, "run", side_effect=results):
                    self.assertEqual(
                        ui._provider_composer_state_at_start(["tmux"], "%1", provider),
                        expected,
                    )

    def test_progress_probe_refuses_ready_session_without_composer_start(self):
        row = make_row("ready-no-composer", "ready", "demo")
        completed = subprocess.CompletedProcess
        results = [
            completed([], 0, stdout="1:0.0|0|0\n", stderr=""),
            completed([], 0, stdout="%dashboard\n", stderr=""),
            completed([], 0, stdout="", stderr=""),
            completed([], 0, stdout="0|1\n", stderr=""),
        ]
        with mock.patch.object(ui.subprocess, "run", side_effect=results) as run:
            result = ui.probe_session_progress(row, timeout=1)
        self.assertIn("could not be positioned safely", result.error)
        self.assertFalse(
            any("-l" in call.args[0] for call in run.call_args_list)
        )

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
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "SHOULD_NOT_LOAD",
                        },
                    }
                )
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
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "VISIBLE"},
                        },
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

    def test_git_context_is_cached_and_reports_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.CompletedProcess(
                [], 0, stdout=f"{tmp}\nfeature/session-details\n", stderr=""
            )
            ui.GIT_CONTEXT_CACHE.clear()
            with mock.patch.object(
                ui.subprocess, "run", return_value=completed
            ) as run:
                first = ui.git_context(tmp, refresh_seconds=60)
                second = ui.git_context(tmp, refresh_seconds=60)
            self.assertEqual(first, (tmp, "feature/session-details"))
            self.assertEqual(second, first)
            self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_session_detail_shows_path_branch_and_identity(self):
        from rich.console import Console

        row = make_row("session-identity", "running", "shared-name")
        snapshot = ui.DashboardSnapshot(sessions=[row], daemon_alive=True)
        output = io.StringIO()
        console = Console(
            file=output, width=64, height=32, force_terminal=False, color_system=None
        )
        with mock.patch.object(
            ui,
            "git_context",
            return_value=("/work/shared-name", "feature/distinguish-tasks"),
        ):
            console.print(ui.render_detail(ui.DashboardView(snapshot), 64, 32))
        rendered = output.getvalue()
        self.assertIn("Path", rendered)
        self.assertIn("/work/shared-name", rendered)
        self.assertIn("Branch", rendered)
        self.assertIn("feature/distinguish-tasks", rendered)
        self.assertIn("Session", rendered)
        self.assertIn("session-identity", rendered)
        self.assertIn("PID 1", rendered)

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
    def test_progress_summary_appears_in_list_and_detail(self):
        from rich.console import Console

        row = make_row("progress", "running", "progress")
        view = ui.DashboardView(ui.DashboardSnapshot(sessions=[row]))
        view.progress_summaries["progress"] = ui.ProgressSummary(
            goal="Ship unified progress",
            done="Provider parser complete",
            current="Rendering dashboard",
            next="Run tests",
            blocker="",
            provider="codex",
            captured_at=time.time(),
        )
        output = io.StringIO()
        console = Console(
            file=output, width=140, height=38, force_terminal=False, color_system=None
        )
        console.print(ui.render_dashboard(view, 140, 38))
        rendered = output.getvalue()
        self.assertIn("Global progress", rendered)
        self.assertIn("Ship unified progress", rendered)
        self.assertIn("Provider parser complete", rendered)
        self.assertIn("Rendering dashboard", rendered)
        self.assertIn("Run tests", rendered)

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
        with (
            mock.patch.dict(
                ui.os.environ,
                {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
                clear=False,
            ),
            mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
        ):
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
        with (
            mock.patch.dict(
                ui.os.environ,
                {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
                clear=False,
            ),
            mock.patch.object(ui.subprocess, "run", side_effect=results),
        ):
            ok, message = ui.switch_to_session(row)
        self.assertFalse(ok)
        self.assertIn("Multiple clients", message)

    def test_switch_rejects_cross_socket_with_quoted_command(self):
        row = make_row("agent", "running", "agent")
        row["tmux_socket"] = "/tmp/custom socket"
        completed = ui.subprocess.CompletedProcess
        with (
            mock.patch.dict(
                ui.os.environ,
                {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%dash"},
                clear=False,
            ),
            mock.patch.object(
                ui.subprocess,
                "run",
                return_value=completed([], 0, stdout="1:0.0\n", stderr=""),
            ) as run,
        ):
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
        with (
            mock.patch.dict(ui.os.environ, environment, clear=True),
            mock.patch.object(ui.subprocess, "run", side_effect=results) as run,
        ):
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
                make_row("gone", "exited", "gone"),
            ]
        )
        ordered = ui.visible_sessions(snapshot)
        self.assertEqual(
            [row["session_key"] for row in ordered], ["input", "error", "ready", "run"]
        )
        attention = ui.visible_sessions(snapshot, "attention")
        self.assertEqual(
            [row["session_key"] for row in attention], ["input", "error", "ready"]
        )
        searched = ui.visible_sessions(snapshot, query="READY")
        self.assertEqual([row["session_key"] for row in searched], ["ready"])
        view = ui.DashboardView(snapshot)
        self.assertEqual(view.rows[-1]["session_key"], ui.EXITED_SUMMARY_KEY)
        self.assertEqual(view.rows[-1]["exit_count"], 1)

    def test_exited_history_navigation_and_newest_first(self):
        now = time.time()
        snapshot = ui.DashboardSnapshot(
            sessions=[
                make_row("run", "running", "run", when=now),
                make_row("old", "exited", "old", when=now - 200),
                make_row("new", "exited", "new", when=now - 10),
            ]
        )
        view = ui.DashboardView(snapshot)
        self.assertEqual(
            [row["session_key"] for row in view.rows],
            ["run", ui.EXITED_SUMMARY_KEY],
        )
        view.jump(len(view.rows) - 1)
        self.assertEqual(ui.handle_key(view, "enter", "", 10), "")
        self.assertTrue(view.history_mode)
        self.assertEqual([row["session_key"] for row in view.rows], ["new", "old"])
        self.assertEqual(ui.handle_key(view, "enter", "", 10), "resume")
        self.assertEqual(ui.handle_key(view, "escape", "", 10), "")
        self.assertFalse(view.history_mode)
        self.assertEqual(view.selected["session_key"], ui.EXITED_SUMMARY_KEY)

    @unittest.skipUnless(ui.RICH_AVAILABLE, "rich unavailable")
    def test_main_render_collapses_exited_sessions(self):
        from rich.console import Console

        snapshot = ui.DashboardSnapshot(
            sessions=[
                make_row("run", "running", "active-project"),
                make_row("gone", "exited", "hidden-exited-project"),
            ],
            daemon_alive=True,
        )
        output = io.StringIO()
        console = Console(
            file=output, width=120, height=32, force_terminal=False, color_system=None
        )
        console.print(ui.render_dashboard(ui.DashboardView(snapshot), 120, 32))
        rendered = output.getvalue()
        self.assertIn("Exited sessions", rendered)
        self.assertIn("1 retained", rendered)
        self.assertNotIn("hidden-exited-project", rendered)

    def test_resume_commands_and_temporary_ids(self):
        codex = make_row("abc-session", "exited", "demo")
        claude = make_row("def-session", "exited", "demo", provider="claude")
        self.assertEqual(
            resume.resume_command(codex), ["codex", "resume", "abc-session"]
        )
        self.assertEqual(
            resume.resume_command(claude), ["claude", "--resume", "def-session"]
        )
        temporary = dict(codex, session_id="pid-10-20")
        available, reason = resume.resume_availability(temporary)
        self.assertFalse(available)
        self.assertIn("Temporary", reason)
        with self.assertRaises(ValueError):
            resume.resume_command(temporary)
        with mock.patch.object(resume.subprocess, "run") as run:
            ok, message = resume.resume_in_new_tmux(temporary)
        self.assertFalse(ok)
        self.assertIn("Temporary", message)
        run.assert_not_called()

    def test_resume_resolves_relative_path_entry_before_changing_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dashboard_cwd = root / "dashboard"
            target_cwd = root / "target"
            trusted = dashboard_cwd / "bin" / "codex"
            untrusted = target_cwd / "bin" / "codex"
            trusted.parent.mkdir(parents=True)
            untrusted.parent.mkdir(parents=True)
            trusted.write_text("#!/bin/sh\nexit 0\n")
            untrusted.write_text("#!/bin/sh\nexit 99\n")
            trusted.chmod(0o700)
            untrusted.chmod(0o700)
            with contextlib.chdir(dashboard_cwd), mock.patch.dict(
                resume.os.environ, {"PATH": "bin"}, clear=False
            ):
                resolved = resume._absolute_executable("codex")
            self.assertEqual(resolved, str(trusted.resolve()))
            self.assertNotEqual(resolved, str(untrusted.resolve()))

    def test_resume_creates_new_tmux_session_and_ignores_old_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            state = root / "state"
            cwd.mkdir()
            row = make_row("abc-session", "exited", "demo")
            row.update(
                {
                    "cwd": str(cwd),
                    "tmux_socket": "/tmp/stale-server",
                    "tmux_target": "stale:9.9",
                    "pane_id": "%999",
                }
            )
            write_resume_artifact(home, row)
            completed = subprocess.CompletedProcess
            results = [
                completed([], 0, stdout="/dev/pts/7|%42\n", stderr=""),
                completed([], 1, stdout="", stderr="missing"),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="0|0|flock\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
            ]
            with (
                mock.patch.dict(
                    resume.os.environ,
                    {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%42"},
                    clear=False,
                ),
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=False),
                mock.patch.object(
                    resume, "_resume_lock_busy", side_effect=[False, True]
                ),
                mock.patch.object(resume.subprocess, "run", side_effect=results) as run,
            ):
                ok, message = resume.resume_in_new_tmux(
                    row, state_dir=state, home=home
                )
            self.assertTrue(ok, message)
            name = resume.resume_tmux_name(row)
            lock_dir = state / "resume-locks"
            lock_file = lock_dir / f"codex-{name.rsplit('-', 1)[-1]}.lock"
            self.assertEqual(stat.S_IMODE(lock_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)
            create_calls = [
                call for call in run.call_args_list if "new-session" in call.args[0]
            ]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(
                create_calls[0].args[0],
                [
                    "tmux",
                    "-S",
                    "/tmp/tmux-0/default",
                    "new-session",
                    "-d",
                    "-s",
                    name,
                    "-c",
                    str(cwd),
                    shlex.join(
                        [
                            resume.sys.executable,
                            "-m",
                            "agent_watch.resume",
                            str(
                                lock_file
                            ),
                            "/usr/bin/codex",
                            "resume",
                            "abc-session",
                        ]
                    ),
                ],
            )
            all_commands = repr([call.args[0] for call in run.call_args_list])
            self.assertNotIn("stale-server", all_commands)
            self.assertNotIn("stale:9.9", all_commands)
            self.assertNotIn("%999", all_commands)
            switch_calls = [
                call for call in run.call_args_list if "switch-client" in call.args[0]
            ]
            self.assertEqual(len(switch_calls), 1)
            self.assertEqual(
                switch_calls[0].args[0],
                [
                    "tmux",
                    "-S",
                    "/tmp/tmux-0/default",
                    "switch-client",
                    "-c",
                    "/dev/pts/7",
                    "-t",
                    f"={name}",
                ],
            )
            self.assertIn("prefix + L", run.call_args_list[-1].args[0][-1])

    def test_resume_lock_wrapper_holds_lock_across_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = pathlib.Path(tmp) / "resume.lock"
            lock_path.write_text("")
            lock_path.chmod(0o600)
            observed = {}

            def fake_exec(executable, command):
                observed["executable"] = executable
                observed["command"] = command
                observed["busy"] = resume._resume_lock_busy(lock_path)
                raise OSError("stop before replacing the test process")

            with mock.patch.object(resume.os, "execv", side_effect=fake_exec):
                with self.assertRaises(OSError):
                    resume.exec_with_lock(
                        lock_path,
                        [resume.sys.executable, "-c", "raise SystemExit(0)"],
                    )
            self.assertTrue(observed["busy"])
            self.assertEqual(observed["executable"], resume.sys.executable)
            self.assertEqual(observed["command"][1:3], ["-c", "raise SystemExit(0)"])

    def test_resume_reuses_existing_named_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            state = root / "state"
            cwd.mkdir()
            row = make_row("existing-session", "exited", "demo", provider="claude")
            row["cwd"] = str(cwd)
            write_resume_artifact(home, row)
            completed = subprocess.CompletedProcess
            results = [
                completed([], 0, stdout="/dev/pts/9|%21\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="0|0|flock\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
            ]
            with (
                mock.patch.dict(
                    resume.os.environ,
                    {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%21"},
                    clear=False,
                ),
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=False),
                mock.patch.object(resume, "_resume_lock_busy", return_value=True),
                mock.patch.object(resume.subprocess, "run", side_effect=results) as run,
            ):
                ok, message = resume.resume_in_new_tmux(
                    row, state_dir=state, home=home
                )
            self.assertTrue(ok, message)
            self.assertFalse(
                any("new-session" in call.args[0] for call in run.call_args_list)
            )
            switch = next(
                call.args[0]
                for call in run.call_args_list
                if "switch-client" in call.args[0]
            )
            self.assertEqual(switch[-1], f"={resume.resume_tmux_name(row)}")

    def test_resume_requires_owned_conversation_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            cwd.mkdir()
            home.mkdir()
            row = make_row("missing-history", "exited", "demo")
            row["cwd"] = str(cwd)
            outside = root / "outside.jsonl"
            outside.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"id": row["session_id"]}}
                )
                + "\n"
            )
            linked = (
                home
                / ".codex"
                / "sessions"
                / "2026"
                / "07"
                / "07"
                / f"rollout-test-{row['session_id']}.jsonl"
            )
            linked.parent.mkdir(parents=True)
            linked.symlink_to(outside)
            with mock.patch.object(
                resume,
                "_absolute_executable",
                side_effect=lambda name: f"/usr/bin/{name}",
            ):
                available, reason = resume.resume_availability(row, home=home)
            self.assertFalse(available)
            self.assertIn("Conversation data", reason)

    def test_resume_replaces_dead_named_session_before_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            state = root / "state"
            cwd.mkdir()
            row = make_row("dead-session", "exited", "demo")
            row["cwd"] = str(cwd)
            write_resume_artifact(home, row)
            completed = subprocess.CompletedProcess
            results = [
                completed([], 0, stdout="/dev/pts/3|%31\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="1|1|codex\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="0|0|flock\n", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
            ]
            with (
                mock.patch.dict(
                    resume.os.environ,
                    {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%31"},
                    clear=False,
                ),
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=False),
                mock.patch.object(
                    resume, "_resume_lock_busy", side_effect=[False, True]
                ),
                mock.patch.object(resume.subprocess, "run", side_effect=results) as run,
            ):
                ok, message = resume.resume_in_new_tmux(
                    row, state_dir=state, home=home
                )
            self.assertTrue(ok, message)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(sum("kill-session" in command for command in commands), 1)
            self.assertEqual(sum("new-session" in command for command in commands), 1)

    def test_resume_refuses_session_already_running_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            cwd.mkdir()
            row = make_row("already-running", "exited", "demo")
            row["cwd"] = str(cwd)
            write_resume_artifact(home, row)
            with (
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=True),
                mock.patch.object(resume.subprocess, "run") as run,
            ):
                ok, message = resume.resume_in_new_tmux(row, home=home)
            self.assertFalse(ok)
            self.assertIn("already running", message)
            run.assert_not_called()

    def test_resume_reports_provider_that_exits_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            state = root / "state"
            cwd.mkdir()
            row = make_row("bad-resume", "exited", "demo")
            row["cwd"] = str(cwd)
            write_resume_artifact(home, row)
            completed = subprocess.CompletedProcess
            results = [
                completed([], 0, stdout="/dev/pts/4|%41\n", stderr=""),
                completed([], 1, stdout="", stderr="missing"),
                completed([], 0, stdout="", stderr=""),
                completed([], 1, stdout="", stderr="missing"),
            ]
            with (
                mock.patch.dict(
                    resume.os.environ,
                    {"TMUX": "/tmp/tmux-0/default,123,0", "TMUX_PANE": "%41"},
                    clear=False,
                ),
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=False),
                mock.patch.object(resume, "_resume_lock_busy", return_value=False),
                mock.patch.object(resume.subprocess, "run", side_effect=results) as run,
            ):
                ok, message = resume.resume_in_new_tmux(
                    row, state_dir=state, home=home
                )
            self.assertFalse(ok)
            self.assertIn("could not resume", message)
            self.assertFalse(
                any("switch-client" in call.args[0] for call in run.call_args_list)
            )

    def test_resume_outside_tmux_attaches_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cwd = root / "project"
            home = root / "home"
            state = root / "state"
            cwd.mkdir()
            row = make_row("outside-tmux", "exited", "demo", provider="claude")
            row["cwd"] = str(cwd)
            write_resume_artifact(home, row)
            completed = subprocess.CompletedProcess
            results = [
                completed([], 1, stdout="", stderr="missing"),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="", stderr=""),
                completed([], 0, stdout="0|0|flock\n", stderr=""),
                completed([], 0, stdout=None, stderr=None),
            ]
            environment = dict(resume.os.environ)
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
            with (
                mock.patch.dict(resume.os.environ, environment, clear=True),
                mock.patch.object(
                    resume,
                    "_absolute_executable",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                mock.patch.object(resume, "_session_is_active", return_value=False),
                mock.patch.object(
                    resume, "_resume_lock_busy", side_effect=[False, True]
                ),
                mock.patch.object(resume.subprocess, "run", side_effect=results) as run,
            ):
                ok, message = resume.resume_in_new_tmux(
                    row, state_dir=state, home=home
                )
            self.assertTrue(ok, message)
            attach = next(
                call
                for call in run.call_args_list
                if "attach-session" in call.args[0]
            )
            self.assertEqual(
                attach.args[0][-1], f"={resume.resume_tmux_name(row)}"
            )
            self.assertFalse(attach.kwargs["capture_output"])

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
                all(
                    line[line.index("updated") - 1].isspace() for line in activity_lines
                )
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
        self.assertEqual(ui.handle_key(view, "text", "b", 10), "progress")
        self.assertEqual(ui.handle_key(view, "text", "B", 10), "progress_all")
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
