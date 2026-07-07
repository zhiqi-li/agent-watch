# Contributing

Thanks for helping improve Agent Watch. The project is experimental and interacts
with private local agent state, so correctness, privacy, and graceful failure are
more important than adding broad heuristics quickly.

## Before opening an issue

- Search existing issues at
  <https://github.com/zhiqi-li/agent-watch/issues>.
- Include Linux, Python, Rich, tmux, Codex CLI, Claude Code, and Agent Watch
  versions.
- Include the exact command and expected/actual state transition.
- Use synthetic fixtures. Never attach a real rollout, transcript, hook payload,
  database, configuration with credentials, or unredacted `status --json` output.
- Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

## Development setup

Requirements are Linux with `/proc`, Python 3.11+, and tmux. Create an isolated
environment from a clone:

```bash
git clone https://github.com/zhiqi-li/agent-watch.git
cd agent-watch
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
```

Do not install development hooks into your real Codex/Claude settings unless the
change specifically requires an end-to-end test. Use a temporary `HOME`, config,
and state directory for manual hook tests.

```bash
export AGENT_WATCH_CONFIG="$PWD/.tmp/config.toml"
export AGENT_WATCH_STATE_DIR="$PWD/.tmp/state"
agent-watch daemon --once --no-notify-existing
```

Keep `.tmp/`, virtual environments, databases, transcripts, tokens, and generated
terminal captures out of commits.

## Design expectations

- Prefer explicit provider lifecycle events over terminal-text heuristics.
- Keep provider-specific parsing isolated and fail to `unknown` when a format is
  not understood.
- Treat “turn complete,” “needs input,” process exit, and “possibly stalled” as
  different facts; do not infer business success from them.
- Hook handlers must remain fast, silent, local, and non-blocking. Network delivery
  belongs in the daemon/outbox path.
- Preserve event deduplication, stale-event checks, retry backoff, and the
  single-daemon guarantee.
- Run subprocesses with argument lists, not `shell=True`; sanitize all terminal
  output and bound every file/network read.
- Keep remote disclosure opt-in. New notification fields require privacy review
  and updates to [PRIVACY.md](PRIVACY.md).
- Avoid unbounded caches or state. If persistent data is added, define migration,
  retention, and deletion behavior.

## Testing

Run the complete suite before submitting a pull request:

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

New behavior should include focused tests for success and failure paths. Provider
format changes need minimal synthetic JSONL fixtures covering unknown and malformed
records. Concurrency changes need tests with independent SQLite connections.
Terminal changes need ANSI/control-character, CJK width, narrow-screen, and tmux
ambiguity cases.

Manual tests should use disposable tmux sockets/sessions and must clean them up.
Do not make tests depend on network access, live APIs, or a developer's real home
directory.

## Pull requests

- Keep each pull request scoped and explain the user-visible behavior and failure
  mode.
- Document schema/config changes and provide backward-compatible migrations.
- Update README, privacy/security documentation, and the changelog when relevant.
- Preserve Python 3.11 compatibility even when developing on Python 3.12+.
- Confirm that installation and uninstallation do not overwrite unrelated user
  hooks or settings.
- Do not claim official OpenAI/Anthropic support or copy proprietary UI assets.

By contributing, you agree that your contribution may be distributed under the
repository's license.
