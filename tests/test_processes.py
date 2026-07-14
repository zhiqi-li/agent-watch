from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest

from agent_watch import processes


class FakePsutilError(Exception):
    pass


class FakeUIDs:
    def __init__(self, real: int) -> None:
        self.real = real


class FakeOpenFile:
    def __init__(self, path: str) -> None:
        self.path = path


class FakeProcess:
    def __init__(
        self,
        info: dict,
        *,
        environment: dict[str, str] | None = None,
        files: list[str] | None = None,
    ) -> None:
        self.info = info
        self._environment = environment or {}
        self._files = files or []

    def environ(self) -> dict[str, str]:
        return self._environment

    def open_files(self) -> list[FakeOpenFile]:
        return [FakeOpenFile(path) for path in self._files]

    def status(self) -> str:
        return str(self.info["status"])

    def create_time(self) -> float:
        return float(self.info["create_time"])


class FakePsutil:
    NoSuchProcess = FakePsutilError
    AccessDenied = FakePsutilError
    ZombieProcess = FakePsutilError
    STATUS_ZOMBIE = "zombie"

    def __init__(self, values: list[FakeProcess]) -> None:
        self.values = values
        self.by_pid = {int(value.info["pid"]): value for value in values}

    def process_iter(self, _attributes):
        return iter(self.values)

    def Process(self, pid: int) -> FakeProcess:
        return self.by_pid[pid]


class ProcessDiscoveryTests(unittest.TestCase):
    def test_darwin_backend_collects_same_user_agent_metadata(self):
        created = 1_783_508_172.0
        current_uid = os.getuid()
        codex = FakeProcess(
            {
                "pid": 101,
                "uids": FakeUIDs(current_uid),
                "name": "codex",
                "status": "running",
                "create_time": created,
                "cwd": "/work/demo",
                "terminal": "/dev/ttys001",
            },
            environment={
                "TMUX": "/tmp/tmux-501/default,123,0",
                "TMUX_PANE": "%7",
            },
            files=["/home/me/.codex/sessions/rollout-test.jsonl"],
        )
        other_user = FakeProcess(
            {
                "pid": 102,
                "uids": FakeUIDs(current_uid + 1),
                "name": "claude",
                "status": "running",
                "create_time": created,
                "cwd": "/private",
                "terminal": None,
            }
        )
        zombie = FakeProcess(
            {
                "pid": 103,
                "uids": FakeUIDs(current_uid),
                "name": "claude",
                "status": "zombie",
                "create_time": created,
                "cwd": "/work/demo",
                "terminal": None,
            }
        )
        claude_daemon = FakeProcess(
            {
                "pid": 104,
                "uids": FakeUIDs(current_uid),
                "name": "claude",
                "status": "running",
                "create_time": created,
                "cwd": "/private",
                "terminal": None,
                "cmdline": ["/usr/local/bin/claude", "daemon", "run"],
            },
            environment={
                "TMUX": "/tmp/tmux-501/default,123,0",
                "TMUX_PANE": "%7",
            },
        )
        fake = FakePsutil([codex, other_user, zombie, claude_daemon])

        found = processes._darwin_agent_processes(fake)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].provider, "codex")
        self.assertEqual(found[0].start_time, time.ctime(created))
        self.assertEqual(found[0].tty, "/dev/ttys001")
        self.assertEqual(found[0].tmux_socket, "/tmp/tmux-501/default")
        self.assertEqual(found[0].tmux_pane, "%7")
        self.assertEqual(
            processes._darwin_process_details(101, fake),
            ("running", time.ctime(created)),
        )
        self.assertEqual(
            processes._darwin_open_process_files(101, fake),
            [pathlib.Path("/home/me/.codex/sessions/rollout-test.jsonl")],
        )

    def test_linux_backend_preserves_proc_metadata_and_open_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc_root = root / "proc"
            process_root = proc_root / "123"
            descriptors = process_root / "fd"
            descriptors.mkdir(parents=True)
            (process_root / "comm").write_text("codex\n")
            fields = ["S", *("0" for _ in range(18)), "4242"]
            (process_root / "stat").write_text(f"123 (codex) {' '.join(fields)}\n")
            (process_root / "environ").write_bytes(
                b"TMUX=/tmp/tmux-100/default,1,0\0TMUX_PANE=%3\0"
            )
            cwd = root / "project"
            cwd.mkdir()
            (process_root / "cwd").symlink_to(cwd)
            (descriptors / "0").symlink_to("/dev/ttys001")
            rollout = root / "rollout-test.jsonl"
            rollout.write_text("{}\n")
            (descriptors / "9").symlink_to(rollout)

            found = processes._linux_agent_processes(proc_root, uid=os.getuid())

            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].start_time, "4242")
            self.assertEqual(found[0].cwd, str(cwd))
            self.assertEqual(found[0].tty, "/dev/ttys001")
            self.assertEqual(found[0].tmux_pane, "%3")
            self.assertEqual(
                processes._linux_open_process_files(123, proc_root), [rollout]
            )

    def test_linux_backend_ignores_claude_daemon_with_inherited_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc_root = root / "proc"
            process_root = proc_root / "456"
            descriptors = process_root / "fd"
            descriptors.mkdir(parents=True)
            (process_root / "comm").write_text("claude\n")
            fields = ["S", *("0" for _ in range(18)), "5151"]
            (process_root / "stat").write_text(
                f"456 (claude) {' '.join(fields)}\n"
            )
            (process_root / "cmdline").write_bytes(
                b"/usr/local/bin/claude\0daemon\0run\0--origin\0transient\0"
            )
            (process_root / "environ").write_bytes(
                b"TMUX=/tmp/tmux-100/default,1,0\0TMUX_PANE=%3\0"
            )
            cwd = root / "project"
            cwd.mkdir()
            (process_root / "cwd").symlink_to(cwd)
            (descriptors / "0").symlink_to("/dev/null")

            found = processes._linux_agent_processes(proc_root, uid=os.getuid())

            self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
