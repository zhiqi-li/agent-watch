from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import socket
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


class AckServer:
    """A small real AF_UNIX peer used to exercise the complete wire protocol."""

    def __init__(
        self,
        path: pathlib.Path,
        response: bytes = b'{"ok":true}\n',
        *,
        hold_response: bool = False,
    ) -> None:
        self.path = path
        self.response = response
        self.hold_response = hold_response
        self.received = bytearray()
        self.accepted = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        path.chmod(0o600)
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            while not self.release.is_set():
                try:
                    connection, _address = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            self.accepted.set()
            with connection:
                connection.settimeout(1.0)
                while b"\n" not in self.received:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    self.received.extend(chunk)
                if self.hold_response:
                    self.release.wait(1.0)
                try:
                    connection.sendall(self.response)
                except OSError:
                    if not self.hold_response:
                        raise
        except BaseException as exc:  # surfaced by __exit__ in the test thread
            self.error = exc
        finally:
            self.finished.set()

    def __enter__(self) -> AckServer:
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release.set()
        self.finished.wait(0.5)
        self.listener.close()
        self.thread.join(0.2)
        if exc_type is None:
            if self.thread.is_alive():
                raise AssertionError("test Unix socket server did not stop")
            if self.error is not None:
                raise AssertionError("test Unix socket server failed") from self.error


def private_temp_dir() -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory()
    pathlib.Path(temporary.name).chmod(0o700)
    return temporary


class CursorNotifyTests(unittest.TestCase):
    def invoke_command(
        self,
        raw_input: bytes,
        socket_path: str | None,
        *,
        timeout: float = 0.5,
        state_dir: str | None = None,
    ) -> tuple[int, str]:
        stdin = io.TextIOWrapper(io.BytesIO(raw_input), encoding="utf-8")
        stderr = io.StringIO()
        args = Namespace(
            socket_path=socket_path,
            timeout=timeout,
            state_dir=state_dir or str(ROOT),
        )
        try:
            with mock.patch.object(aw.sys, "stdin", stdin), contextlib.redirect_stderr(
                stderr
            ):
                result = aw.cursor_notify_command(args)
        finally:
            stdin.detach()
        return result, stderr.getvalue()

    def invoke_main(self, argv: list[str], raw_input: bytes) -> tuple[int, str]:
        stdin = io.TextIOWrapper(io.BytesIO(raw_input), encoding="utf-8")
        stderr = io.StringIO()
        try:
            with mock.patch.object(aw.sys, "stdin", stdin), contextlib.redirect_stderr(
                stderr
            ):
                result = aw.main(argv)
        finally:
            stdin.detach()
        return result, stderr.getvalue()

    def test_cli_uses_state_directory_socket_and_round_trips_compact_unicode_json(self):
        with private_temp_dir() as tmp:
            state_dir = pathlib.Path(tmp)
            socket_path = state_dir / "cursor-notify.sock"
            payload = {
                "title": "Codex · 需要回复",
                "body": "项目 agent-watch 正在等待输入",
                "severity": "warning",
                "events": [{"provider": "codex", "state": "needs_input"}],
            }
            with AckServer(socket_path) as server:
                result, stderr = self.invoke_main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "cursor-notify",
                        "--timeout",
                        "0.5",
                    ],
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )

            self.assertEqual(result, 0, stderr)
            wire = bytes(server.received)
            self.assertTrue(wire.endswith(b"\n"))
            self.assertEqual(wire.count(b"\n"), 1)
            self.assertNotIn(b": ", wire)
            self.assertEqual(json.loads(wire), payload)

    def test_negative_or_non_boolean_ack_is_delivery_failure(self):
        responses = {
            "negative": b'{"ok":false,"error":"notifications disabled"}\n',
            "non-boolean": b'{"ok":1}\n',
            "malformed": b"not-json\n",
            "missing-newline": b'{"ok":true}',
            "multiple-lines": b'{"ok":true}\n{"ok":true}\n',
            "oversized": (
                b'{"ok":true,"padding":"'
                + b"x" * aw.CURSOR_NOTIFY_MAX_ACK_BYTES
                + b'"}\n'
            ),
        }
        for name, response in responses.items():
            with self.subTest(case=name), private_temp_dir() as tmp:
                socket_path = pathlib.Path(tmp) / "cursor.sock"
                with AckServer(socket_path, response=response):
                    result, _stderr = self.invoke_command(
                        b'{"title":"Agent Watch","body":"Needs input"}',
                        str(socket_path),
                    )
                self.assertEqual(result, 1)

    def test_response_timeout_is_fast_and_reported_as_delivery_failure(self):
        with private_temp_dir() as tmp:
            socket_path = pathlib.Path(tmp) / "cursor.sock"
            with AckServer(socket_path, hold_response=True) as server:
                result, _stderr = self.invoke_command(
                    b'{"title":"Agent Watch","body":"Needs input"}',
                    str(socket_path),
                    timeout=0.05,
                )
                self.assertTrue(server.accepted.wait(0.2))
            self.assertEqual(result, 1)

    def test_invalid_timeout_is_an_input_error(self):
        for timeout in (0, -1, float("nan"), float("inf"), 30.01):
            with self.subTest(timeout=timeout):
                result, _stderr = self.invoke_command(
                    b'{"title":"Agent Watch","body":"Needs input"}',
                    "/does/not/need/to/exist.sock",
                    timeout=timeout,
                )
                self.assertEqual(result, 2)

    def test_stdin_must_be_exactly_one_utf8_json_object(self):
        invalid_inputs = (
            b"",
            b"{",
            b"[]",
            b"{}",
            b"{} {}",
            b'"string"',
            b'{"title":"Agent Watch","body":""}',
            b'{"title":"Agent Watch","body":"ok","number":NaN}',
            b"\xff",
        )
        for raw_input in invalid_inputs:
            with self.subTest(raw_input=raw_input):
                result, _stderr = self.invoke_command(
                    raw_input,
                    "/does/not/need/to/exist.sock",
                )
                self.assertEqual(result, 2)

    def test_payload_larger_than_256_kib_is_rejected_before_connect(self):
        prefix = b'{"title":"Agent Watch","body":"'
        suffix = b'"}'
        at_limit = (
            prefix
            + b"x" * (aw.CURSOR_NOTIFY_MAX_PAYLOAD_BYTES - len(prefix) - len(suffix))
            + suffix
        )
        oversized = at_limit[:-len(suffix)] + b"x" + suffix
        self.assertEqual(len(at_limit), aw.CURSOR_NOTIFY_MAX_PAYLOAD_BYTES)
        self.assertEqual(len(oversized), aw.CURSOR_NOTIFY_MAX_PAYLOAD_BYTES + 1)

        with private_temp_dir() as tmp:
            socket_path = pathlib.Path(tmp) / "cursor.sock"
            with AckServer(socket_path):
                result, stderr = self.invoke_command(at_limit, str(socket_path))
            self.assertEqual(result, 0, stderr)

        result, _stderr = self.invoke_command(
            oversized,
            "/does/not/need/to/exist.sock",
        )
        self.assertEqual(result, 2)

    def test_relative_regular_symlink_and_accessible_socket_paths_are_rejected(self):
        payload = b'{"title":"Agent Watch","body":"Needs input"}'
        with private_temp_dir() as tmp:
            root = pathlib.Path(tmp)
            regular = root / "regular.sock"
            regular.write_bytes(b"")
            regular.chmod(0o600)
            accessible_parent = root / "accessible"
            accessible_parent.mkdir(mode=0o755)

            real_socket = root / "real.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(real_socket))
                real_socket.chmod(0o600)
                symlink = root / "linked.sock"
                symlink.symlink_to(real_socket)

                for unsafe_path in (
                    "relative.sock",
                    str(regular),
                    str(symlink),
                    str(accessible_parent / "missing.sock"),
                ):
                    with self.subTest(path=unsafe_path):
                        result, _stderr = self.invoke_command(payload, unsafe_path)
                        self.assertEqual(result, 2)

                actual_uid = os.getuid()
                with mock.patch.object(aw.os, "getuid", return_value=actual_uid + 1):
                    result, _stderr = self.invoke_command(payload, str(real_socket))
                self.assertEqual(result, 2)

                real_socket.chmod(0o660)
                result, _stderr = self.invoke_command(payload, str(real_socket))
                self.assertEqual(result, 2)
            finally:
                listener.close()

    def test_missing_socket_is_a_delivery_failure(self):
        with private_temp_dir() as tmp:
            result, _stderr = self.invoke_command(
                b'{"title":"Agent Watch","body":"Needs input"}',
                str(pathlib.Path(tmp) / "missing.sock"),
            )
        self.assertEqual(result, 1)

    def test_environment_socket_overrides_state_directory_default(self):
        with private_temp_dir() as tmp:
            root = pathlib.Path(tmp)
            socket_path = root / "from-env.sock"
            unused_state = root / "unused-state"
            with AckServer(socket_path), mock.patch.dict(
                os.environ,
                {"AGENT_WATCH_CURSOR_SOCKET": str(socket_path)},
            ):
                result, stderr = self.invoke_command(
                    b'{"title":"Agent Watch","body":"Needs input"}',
                    None,
                    state_dir=str(unused_state),
                )
            self.assertEqual(result, 0, stderr)

    def test_first_class_cursor_channel_delivers_and_is_required(self):
        row = {
            "provider": "codex",
            "session_id": "cursor-test",
            "tmux_target": "work:1.0",
            "tmux_socket": "",
            "pane_id": "%1",
            "cwd": "/work/agent-watch",
            "name": "agent-watch",
            "state": "needs_input",
            "message": "private prompt",
            "pid": 123,
            "source": "codex-rollout",
        }
        config = aw.deep_merge(
            aw.DEFAULT_CONFIG,
            {
                "notifications": {
                    "console": False,
                    "tmux": False,
                    "cursor": {"enabled": True, "include_prompt": True},
                }
            },
        )

        class PromptLoader:
            @staticmethod
            def load(_row):
                return {"user": {"text": "latest user prompt"}}

        with private_temp_dir() as tmp:
            state_dir = pathlib.Path(tmp)
            socket_path = state_dir / aw.CURSOR_NOTIFY_SOCKET_NAME
            with AckServer(socket_path) as server, mock.patch.object(
                aw,
                "CURSOR_PROMPT_LOADER",
                PromptLoader(),
            ):
                payload, delivered = aw.send_notifications(
                    [row],
                    config,
                    state_dir=state_dir,
                )

        self.assertIs(delivered["cursor"], True)
        self.assertEqual(aw.required_channels(config), {"cursor"})
        cursor_payload = json.loads(bytes(server.received))
        self.assertNotIn("host", cursor_payload)
        self.assertNotIn("Host:", cursor_payload["body"])
        self.assertEqual(cursor_payload["events"][0]["tmux_target"], "work:1.0")
        self.assertEqual(cursor_payload["events"][0]["prompt"], "latest user prompt")
        self.assertIn("Prompt: latest user prompt", cursor_payload["body"])
        self.assertNotIn("latest user prompt", json.dumps(payload))
        self.assertNotIn("private prompt", json.dumps(payload))

    def test_cursor_prompt_is_absent_without_explicit_opt_in(self):
        row = {
            "provider": "claude",
            "session_id": "cursor-test",
            "tmux_target": "work:2.0",
            "tmux_socket": "",
            "cwd": "/work/agent-watch",
            "state": "ready",
            "message": "private prompt",
            "pid": 123,
            "source": "claude-hook",
        }
        config = aw.deep_merge(
            aw.DEFAULT_CONFIG,
            {
                "notifications": {
                    "console": False,
                    "tmux": False,
                    "cursor": {"enabled": True, "include_prompt": False},
                }
            },
        )
        with private_temp_dir() as tmp:
            state_dir = pathlib.Path(tmp)
            with AckServer(state_dir / aw.CURSOR_NOTIFY_SOCKET_NAME) as server:
                aw.send_notifications([row], config, state_dir=state_dir)
        cursor_payload = json.loads(bytes(server.received))
        self.assertNotIn("prompt", cursor_payload["events"][0])
        self.assertNotIn("private prompt", json.dumps(cursor_payload))

    def test_first_class_cursor_channel_reports_missing_listener(self):
        config = aw.deep_merge(
            aw.DEFAULT_CONFIG,
            {
                "notifications": {
                    "console": False,
                    "tmux": False,
                    "cursor": {"enabled": True},
                }
            },
        )
        with private_temp_dir() as tmp:
            _payload, delivered = aw.send_notifications(
                [],
                config,
                state_dir=pathlib.Path(tmp),
            )
        self.assertFalse(aw.channel_succeeded(delivered["cursor"]))
        self.assertIn("not running", delivered["cursor"][1])

    def test_cursor_channel_configuration_is_type_checked(self):
        for cursor_settings in (
            "enabled",
            {"enabled": "true", "socket": ""},
            {"enabled": False, "socket": 123},
            {"enabled": False, "socket": "", "include_prompt": "true"},
        ):
            with self.subTest(cursor_settings=cursor_settings):
                config = aw.deep_merge(aw.DEFAULT_CONFIG, {})
                config["notifications"]["cursor"] = cursor_settings
                with self.assertRaises(ValueError):
                    aw.validate_config(config)

    def test_new_cursor_listener_wakes_a_delayed_retry(self):
        config = aw.deep_merge(
            aw.DEFAULT_CONFIG,
            {
                "notifications": {
                    "console": False,
                    "tmux": False,
                    "cursor": {"enabled": True},
                }
            },
        )
        with private_temp_dir() as tmp:
            state_dir = pathlib.Path(tmp)
            db = aw.StateDB(state_dir)
            observation = aw.Observation(
                key="codex:cursor-retry",
                provider="codex",
                session_id="cursor-retry",
                pid=123,
                proc_start="1",
                pane_id="%1",
                tmux_target="work:1.0",
                cwd="/work/agent-watch",
                name="agent-watch",
                state="needs_input",
                event_id="event-1",
                source="test",
                observed_at=time.time(),
            )
            db.upsert(observation)
            self.assertTrue(db.enqueue_session_now(observation.key, config))
            claimed = db.claim_outbox()
            db.finish_outbox(claimed, {"cursor": (False, "offline")}, False)
            delayed = db.conn.execute(
                "SELECT available_at FROM outbox WHERE event_key=?",
                (claimed[0]["event_key"],),
            ).fetchone()[0]
            self.assertGreater(delayed, time.time() + 30)

            socket_path = state_dir / aw.CURSOR_NOTIFY_SOCKET_NAME
            aw.CURSOR_SOCKET_GENERATIONS.clear()
            try:
                with AckServer(socket_path):
                    self.assertEqual(aw.notify_due(db, config), 1)
                sent_at = db.conn.execute(
                    "SELECT sent_at FROM outbox WHERE event_key=?",
                    (claimed[0]["event_key"],),
                ).fetchone()[0]
                self.assertIsNotNone(sent_at)
            finally:
                aw.CURSOR_SOCKET_GENERATIONS.clear()
                db.close()


if __name__ == "__main__":
    unittest.main()
