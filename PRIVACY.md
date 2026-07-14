# Privacy

Agent Watch is a local, single-user monitoring utility. It has no analytics,
telemetry, account system, or hosted backend. It still handles sensitive local
metadata and can send notifications to services you configure.

## Data read locally

Depending on enabled features, Agent Watch reads:

- current-user process IDs, executable names, start times, cwd, TTY, environment
  fields used for tmux location, and open rollout file descriptors from Linux
  `/proc` or the macOS process APIs exposed by psutil;
- tmux session/window/pane metadata and a bounded tail of pane text;
- Codex rollout JSONL and Claude session/transcript files under the current
  user's home directory;
- Codex and Claude hook payloads delivered to the local hook command;
- the Agent Watch TOML configuration.

Provider session and transcript formats are internal and experimental. A parser
may misclassify content after an upstream format change.

## Data stored locally

The default state directory is `~/.local/state/agent-watch/`. It contains:

- `state.sqlite3` with session identity, provider, PID, cwd, tmux location,
  derived state, timestamps, event fingerprints, short hook messages, outbox
  snapshots, delivery results, and notification history;
- `spool/` files used when a hook cannot briefly acquire SQLite;
- `hook-errors.log` when hook ingestion fails;
- the daemon lock file and SQLite WAL/SHM files;
- `cursor-notify.sock` while the optional Cursor companion extension is active.
  This Unix socket endpoint does not persist notification payloads.

The conversation preview shown in the right-hand TUI panel is read on demand and
cached only in dashboard process memory. It is not written to SQLite. The preview
is designed to omit reasoning, system instructions, tool arguments, and tool
outputs, but this is not a formal data-loss-prevention boundary.

When the operator presses `b` or `B`, the dashboard sends a bounded `/btw`
progress question to the selected running Codex/Claude tmux pane. The provider
answer is captured from its temporary overlay and cached only in dashboard
process memory. It is not added to the main provider transcript, written to
SQLite, or included in notifications. This action invokes the configured model
and may consume provider usage.

Optional ephemeral-container persistence is disabled by default. When enabled,
it copies `~/.codex/sessions`, `~/.claude/projects`, and a consistent Agent Watch
SQLite snapshot to the operator-selected filesystem directory. The backup does
not include provider authentication or settings, Agent Watch configuration,
hook logs, or current-process session metadata. Provider transcripts themselves
can contain prompts, responses, source code, tool output, filesystem paths, and
secrets. The destination is therefore sensitive even though it is not a remote
notification channel. Agent Watch applies directory mode `0700`, file mode
`0600`, rejects symlinked transcript files, atomically replaces full copies, and
uses prefix-verified, resumable appends for complete JSONL records. Restore omits
an incomplete final JSONL record that an abrupt process kill may leave behind.
Storage-level ACLs and administrator access remain outside its control.

Persistent provider transcript backups are additive and are not governed by
`monitor.retention_days`: deleting a local transcript does not delete its backup.
The SQLite snapshot reflects normal database retention on each replacement.
Operators must delete the dedicated persistent directory when the transcript
backup is no longer needed. Automatic restore fills only missing local files and
does not overwrite existing transcripts or a non-empty local database.

Delivered notification/outbox history and stale sessions are pruned after 30
days by default. Configure `monitor.retention_days`; pending delivery records are
preserved so failed notifications can still retry. Stop the daemon, then use
`agent-watch clear-history --yes` to delete database history, hook spool files,
and the hook error log.

## Data sent elsewhere

By default, notification output is local console/tmux only. Agent Watch invokes
an external delivery action only when the operator enables desktop, Cursor, a
command, webhook, ntfy, Telegram, or Bark delivery. Network requests are limited
to the configured webhook, ntfy, Telegram, and Bark channels.

Remote notification payloads normally include host name, provider, derived state,
project name, timestamp, and `session:window.pane`. The optional Cursor channel
uses a separate editor-only payload over a private same-user Unix socket. It
omits the host name and can include a bounded latest user-prompt excerpt only
after a separate opt-in. Full tmux socket paths and pane IDs are not sent by
default. The following are opt-in:

```toml
[notifications]
include_cwd = false
include_message_preview = false
include_tmux_socket = false

[notifications.cursor]
include_prompt = false
```

Setting a corresponding option to `true` can disclose local paths or message
excerpts to every enabled notification channel. A custom command receives the
notification JSON on stdin. Delivery attempts and endpoint result summaries are
retained locally.

Cursor toasts are compact by default. Selecting their **Details** action writes
the complete notification body to the Agent Watch Output channel; Cursor may
retain that Output content in its extension-host logs.

Third-party services apply their own privacy policies and retention. Public ntfy
topic names can function like bearer credentials; use a long random topic with
authentication or a private server for sensitive work.

## Operator controls

- Keep the config file mode `0600` and state directory mode `0700`.
- Keep any configured persistence directory outside public source checkouts,
  verify its filesystem ACLs, and treat every backed-up transcript as sensitive.
- Use `include_cwd = false` and `include_message_preview = false` unless disclosure
  is explicitly acceptable.
- Keep `ui.conversation_preview = false` (the default) when selected text must not
  appear on screen. The `p` key enables it only for the current UI process; avoid
  terminal recording and shared scrollback for sensitive sessions.
- Use `b`/`B` only when sending the fixed progress question to those sessions and
  displaying the returned summary is acceptable. Agent Watch refuses to type
  into non-running panes or panes currently active in another tmux client.
- `agent-watch status --json` is redacted by default. Treat `--full`, database
  rows, logs, screenshots, and bug reports as sensitive.
- Review configured notification endpoints and their credentials regularly.

To remove hooks and erase local Agent Watch state, first stop the daemon, then:

```bash
agent-watch uninstall-hooks
systemctl --user disable --now agent-watch.service 2>/dev/null || true
tmux kill-session -t agent-watch 2>/dev/null || true
agent-watch clear-history --yes
rm -rf ~/.local/state/agent-watch  # optional: remove all remaining runtime files
```

Removing state cannot retract notifications already delivered to terminals,
commands, or third-party services. Remove the config separately if it contains
credentials. Uninstalling the pipx package does not by itself remove hooks,
configuration, or state.

## Scope and contact

Agent Watch monitors only data visible to the current Unix user and is not
designed for multi-user or centrally administered monitoring. For a privacy bug
with security impact, follow [SECURITY.md](SECURITY.md). For documentation or
feature requests, use
<https://github.com/zhiqi-li/agent-watch/issues> with synthetic data only.

This project is independent and is not affiliated with OpenAI or Anthropic.
