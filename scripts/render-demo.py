#!/usr/bin/env python3
"""Render deterministic README screenshots from synthetic session data."""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import sys
from collections.abc import Callable
from typing import Any
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_watch.dashboard import (  # noqa: E402
    DashboardSnapshot,
    DashboardView,
    ProgressSummary,
    RICH_AVAILABLE,
    render_dashboard,
)

if RICH_AVAILABLE:  # pragma: no branch - this script requires the UI dependency
    from rich.console import Console


WIDTH = 140
HEIGHT = 38
DEMO_NOW = 1_767_268_800.0
DEMO_FILES = {
    "overview": "agent-watch-demo.svg",
    "progress": "agent-watch-progress-demo.svg",
    "recovery": "agent-watch-recovery-demo.svg",
    "shortcuts": "agent-watch-shortcuts-demo.svg",
}


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
    return {
        "session_key": key,
        "provider": provider,
        "session_id": f"demo-{key.replace(':', '-')}",
        "pid": pid,
        "proc_start": "demo",
        "pane_id": f"%{pid % 10}",
        "tmux_target": target,
        "tmux_socket": "/tmp/agent-watch-demo/default",
        "cwd": f"/workspace/{project}",
        "name": project,
        "state": state,
        "state_since": DEMO_NOW - state_age,
        "last_seen": DEMO_NOW - 1,
        "last_activity_at": DEMO_NOW - activity_age,
        "event_id": f"demo-{key}-event",
        "source": source,
        "raw_status": state,
        "message": "",
    }


def demo_snapshot(
    rows: list[dict[str, Any]], *, pending: int = 0, retrying: int = 0
) -> DashboardSnapshot:
    return DashboardSnapshot(
        sessions=rows,
        daemon_alive=True,
        daemon_pid="4100",
        heartbeat_at=DEMO_NOW,
        last_success_at=DEMO_NOW,
        pending_outbox=pending,
        retrying_outbox=retrying,
        loaded_at=DEMO_NOW,
        activity_stale_seconds=600,
    )


def select(view: DashboardView, session_key: str) -> DashboardView:
    view.selected_key = session_key
    view._ensure_selection()
    view.spinner_index = 3
    return view


def build_overview_demo() -> DashboardView:
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
            "auto_wait",
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
        demo_row(
            "claude:catalog-import",
            "claude",
            "catalog-import",
            "exited",
            "dev:4.0",
            3_600,
            3_600,
            "claude-hook",
            4106,
        ),
    ]
    view = select(
        DashboardView(
            demo_snapshot(rows, pending=3, retrying=1), conversation_preview=True
        ),
        "codex:checkout-api",
    )
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


def progress_summary(
    goal: str, done: str, current: str, next_step: str, blocker: str = ""
) -> ProgressSummary:
    return ProgressSummary(
        goal=goal,
        done=done,
        current=current,
        next=next_step,
        blocker=blocker,
        provider="codex",
        captured_at=DEMO_NOW - 45,
    )


def build_progress_demo() -> DashboardView:
    rows = [
        demo_row(
            "claude:release-train",
            "claude",
            "release-train",
            "ready",
            "ship:1.0",
            95,
            18,
            "claude-session",
            4201,
        ),
        demo_row(
            "codex:checkout-api",
            "codex",
            "checkout-api",
            "running",
            "dev:1.0",
            1_380,
            3,
            "codex-rollout",
            4202,
        ),
        demo_row(
            "claude:data-backfill",
            "claude",
            "data-backfill",
            "auto_wait",
            "ops:2.0",
            2_140,
            12,
            "claude-session",
            4203,
        ),
        demo_row(
            "codex:mobile-ci",
            "codex",
            "mobile-ci",
            "running",
            "ci:1.0",
            860,
            9,
            "codex-hook",
            4204,
        ),
        demo_row(
            "claude:docs-site",
            "claude",
            "docs-site",
            "running",
            "dev:3.0",
            470,
            4,
            "claude-session",
            4205,
        ),
    ]
    view = select(
        DashboardView(demo_snapshot(rows), conversation_preview=True),
        "codex:checkout-api",
    )
    view.progress_summaries = {
        "claude:release-train": progress_summary(
            "Publish version 2.4",
            "Release notes and packages are ready",
            "Waiting for final approval",
            "Create the signed tag",
            "Release manager approval",
        ),
        "codex:checkout-api": progress_summary(
            "Ship cursor pagination safely",
            "API and compatibility tests complete",
            "Running the migration smoke test",
            "Deploy canary and watch error rate",
            "Production approval required",
        ),
        "codex:mobile-ci": progress_summary(
            "Stabilize the mobile release pipeline",
            "Flaky simulator tests isolated",
            "Validating the retry policy on three runners",
            "Enable the policy for the release branch",
        ),
    }
    view.progress_pending.add("claude:data-backfill")
    view.progress_errors["claude:docs-site"] = (
        "The provider did not return a structured progress line before the timeout"
    )
    view.set_context(
        "codex:checkout-api",
        {
            "user": {
                "text": "Finish the rollout end to end and verify the canary metrics.",
                "at": DEMO_NOW - 1_300,
            },
            "assistant": {
                "text": "The migration test is running against the production-like snapshot.",
                "at": DEMO_NOW - 70,
            },
            "tool": {"text": "exec_command", "at": DEMO_NOW - 20},
        },
    )
    view.flash = "Captured 3 global progress snapshots; 1 query still running"
    view.flash_until = DEMO_NOW + 60
    return view


def build_recovery_demo() -> DashboardView:
    rows = [
        demo_row(
            "codex:checkout-api",
            "codex",
            "checkout-api",
            "exited",
            "dev:1.0",
            210,
            210,
            "codex-rollout",
            4301,
        ),
        demo_row(
            "claude:release-notes",
            "claude",
            "release-notes",
            "exited",
            "dev:2.0",
            760,
            760,
            "claude-session",
            4302,
        ),
        demo_row(
            "codex:legacy-import",
            "codex",
            "legacy-import",
            "exited",
            "archive:1.0",
            3_900,
            3_900,
            "codex-rollout",
            4303,
        ),
        demo_row(
            "claude:prototype",
            "claude",
            "prototype",
            "exited",
            "lab:2.0",
            8_400,
            8_400,
            "claude-session",
            4304,
        ),
    ]
    view = DashboardView(demo_snapshot(rows), conversation_preview=False)
    view.history_mode = True
    return select(view, "codex:checkout-api")


def build_shortcuts_demo() -> DashboardView:
    view = build_overview_demo()
    view.show_help = True
    return view


def demo_resume_availability(row: dict[str, Any]) -> tuple[bool, str]:
    key = str(row.get("session_key") or "")
    if key in {"codex:checkout-api", "claude:release-notes"}:
        return True, ""
    if key == "codex:legacy-import":
        return False, "Conversation data is no longer available"
    return False, "Working directory no longer exists"


def demo_git_context(cwd: str) -> tuple[str, str]:
    branches = {
        "checkout-api": "feature/cursor-pagination",
        "release-train": "release/2.4",
        "release-notes": "docs/release-2.4",
    }
    return cwd, branches.get(pathlib.Path(cwd).name, "main")


SCENARIOS: dict[str, tuple[str, Callable[[], DashboardView]]] = {
    "overview": ("Agent Watch — Live Triage", build_overview_demo),
    "progress": ("Agent Watch — Global Progress", build_progress_demo),
    "recovery": ("Agent Watch — Session Recovery", build_recovery_demo),
    "shortcuts": ("Agent Watch — Keyboard Workflow", build_shortcuts_demo),
}


def render(output: pathlib.Path, scenario: str = "overview") -> None:
    if not RICH_AVAILABLE:
        raise SystemExit("render-demo.py requires the 'rich' package")
    title, build_view = SCENARIOS[scenario]
    console = Console(
        file=io.StringIO(),
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=WIDTH,
        height=HEIGHT,
        highlight=False,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch("agent_watch.dashboard.time.time", return_value=DEMO_NOW)
        )
        stack.enter_context(
            mock.patch("agent_watch.dashboard.git_context", side_effect=demo_git_context)
        )
        stack.enter_context(
            mock.patch(
                "agent_watch.dashboard.clock_time", return_value="12:00:00"
            )
        )
        if scenario == "recovery":
            stack.enter_context(
                mock.patch(
                    "agent_watch.dashboard.resume_availability",
                    side_effect=demo_resume_availability,
                )
            )
        console.print(render_dashboard(build_view(), WIDTH, HEIGHT))
    svg = console.export_svg(
        title=title,
        clear=True,
        unique_id=f"agent-watch-{scenario}-demo",
    )
    # Keep repository images self-contained and independent of CDN fonts.
    svg = re.sub(r"\s*@font-face \{.*?\}\s*", "\n", svg, flags=re.DOTALL)
    svg = svg.replace(
        "font-family: Fira Code, monospace;",
        'font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;',
    )
    svg = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")


def render_all(output_dir: pathlib.Path) -> list[pathlib.Path]:
    outputs: list[pathlib.Path] = []
    for scenario, filename in DEMO_FILES.items():
        output = output_dir / filename
        render(output, scenario)
        outputs.append(output)
    return outputs


def main() -> int:
    if len(sys.argv) > 2:
        raise SystemExit("usage: render-demo.py [OUTPUT.svg | OUTPUT_DIRECTORY]")
    if len(sys.argv) == 1:
        outputs = render_all(ROOT / "docs")
    else:
        destination = pathlib.Path(sys.argv[1]).resolve()
        if destination.suffix.lower() == ".svg":
            render(destination)
            outputs = [destination]
        else:
            outputs = render_all(destination)
    for output in outputs:
        print(f"wrote synthetic dashboard screenshot: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
