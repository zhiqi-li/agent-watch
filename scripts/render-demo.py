#!/usr/bin/env python3
"""Render the README screenshot from deterministic, synthetic session data."""

from __future__ import annotations

import io
import pathlib
import re
import sys
from typing import Any
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_watch.dashboard import (  # noqa: E402
    DashboardSnapshot,
    DashboardView,
    RICH_AVAILABLE,
    render_dashboard,
)

if RICH_AVAILABLE:  # pragma: no branch - this script requires the UI dependency
    from rich.console import Console


WIDTH = 140
HEIGHT = 38
DEMO_NOW = 1_767_268_800.0


def demo_row(
    key: str,
    provider: str,
    project: str,
    state: str,
    target: str,
    state_age: float,
    activity_age: float,
    source: str,
    pid: int,
) -> dict[str, Any]:
    now = DEMO_NOW
    return {
        "session_key": key,
        "provider": provider,
        "session_id": f"demo-{key}",
        "pid": pid,
        "proc_start": "demo",
        "pane_id": f"%{pid % 10}",
        "tmux_target": target,
        "tmux_socket": "/tmp/agent-watch-demo/default",
        "cwd": f"/workspace/{project}",
        "name": project,
        "state": state,
        "state_since": now - state_age,
        "last_seen": now - 1,
        "last_activity_at": now - activity_age,
        "event_id": f"demo-{key}-event",
        "source": source,
        "raw_status": state,
        "message": "",
    }


def build_demo() -> DashboardView:
    now = DEMO_NOW
    rows = [
        demo_row(
            "codex:checkout-api",
            "codex",
            "checkout-api",
            "needs_input",
            "dev:1.0",
            82,
            14,
            "codex-hook",
            4101,
        ),
        demo_row(
            "claude:release-notes",
            "claude",
            "release-notes",
            "ready",
            "dev:2.0",
            190,
            32,
            "claude-session",
            4102,
        ),
        demo_row(
            "codex:billing-worker",
            "codex",
            "billing-worker",
            "error",
            "ops:1.0",
            44,
            44,
            "codex-hook",
            4103,
        ),
        demo_row(
            "claude:docs-site",
            "claude",
            "docs-site",
            "running",
            "dev:3.0",
            540,
            7,
            "claude-session",
            4104,
        ),
        demo_row(
            "codex:research-sandbox",
            "codex",
            "research-sandbox",
            "running",
            "lab:1.0",
            1_240,
            780,
            "codex-rollout",
            4105,
        ),
    ]
    snapshot = DashboardSnapshot(
        sessions=rows,
        daemon_alive=True,
        daemon_pid="4100",
        heartbeat_at=now,
        last_success_at=now,
        pending_outbox=1,
        retrying_outbox=0,
        loaded_at=now,
        activity_stale_seconds=600,
    )
    view = DashboardView(snapshot, conversation_preview=True)
    view.selected_key = "codex:checkout-api"
    view._ensure_selection()
    view.spinner_index = 3
    view.set_context(
        "codex:checkout-api",
        {
            "user": {
                "text": (
                    "Add cursor pagination to the orders endpoint and keep the "
                    "response backward compatible."
                ),
                "at": 0,
            },
            "assistant": {
                "text": (
                    "Pagination and tests are complete. I need approval to run "
                    "the database migration smoke test."
                ),
                "at": 0,
            },
            "tool": {"text": "exec_command", "at": 0},
        },
    )
    return view


def render(output: pathlib.Path) -> None:
    if not RICH_AVAILABLE:
        raise SystemExit("render-demo.py requires the 'rich' package")
    console = Console(
        file=io.StringIO(),
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=WIDTH,
        height=HEIGHT,
        highlight=False,
    )
    with mock.patch("agent_watch.dashboard.time.time", return_value=DEMO_NOW):
        console.print(render_dashboard(build_demo(), WIDTH, HEIGHT))
    svg = console.export_svg(
        title="Agent Watch — Synthetic Demo",
        clear=True,
        unique_id="agent-watch-demo",
    )
    # Keep the repository image self-contained: prefer local monospace fonts
    # instead of the CDN-backed @font-face blocks in Rich's default template.
    svg = re.sub(r"\s*@font-face \{.*?\}\s*", "\n", svg, flags=re.DOTALL)
    svg = svg.replace(
        "font-family: Fira Code, monospace;",
        'font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;',
    )
    svg = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")


def main() -> int:
    output = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/agent-watch-demo.svg"
    render(output.resolve())
    print(f"wrote synthetic dashboard screenshot: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
