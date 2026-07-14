# Security Policy

## Supported versions

Agent Watch is experimental. Security fixes are made on a best-effort basis for
the latest tagged `0.x` release and the current `main` branch. Older snapshots are
not supported.

| Version | Security updates |
|---|---|
| Latest `0.x` | Yes |
| `main` | Yes, may be unstable |
| Older releases | No |

## Reporting a vulnerability

Do not open a public issue for a vulnerability or attach real Codex/Claude
transcripts. Use a private GitHub security advisory:

<https://github.com/zhiqi-li/agent-watch/security/advisories/new>

Include:

- the Agent Watch version or commit;
- operating system, Python, tmux, Codex CLI, and Claude Code versions;
- an impact description and minimal reproduction using synthetic data;
- whether the issue requires the same Unix user, a malicious project, or a
  configured remote notification endpoint.

Remove API keys, bot tokens, webhook secrets, usernames, hostnames, paths, prompts,
responses, and transcript content. Maintainers will acknowledge reports and
coordinate disclosure on a best-effort basis; there is currently no paid bug
bounty or guaranteed response SLA.

## Security model

Agent Watch is intended for a single Unix user on a trusted Linux or macOS host.
Run it as that user, never as root merely to discover more sessions. It is not a
privilege boundary, sandbox, multi-tenant service, or protection against a
process already running as the same user.

The daemon reads same-user process metadata through Linux `/proc` or macOS
process APIs, tmux metadata and pane text, Codex rollouts, Claude
session/transcript files, and hook payloads. It writes local state under
`~/.local/state/agent-watch/` and changes Codex/Claude hook configuration only
when `install-hooks` or `uninstall-hooks` is explicitly run.

The dashboard is passive by default. Pressing `b` or `B` explicitly authorizes a
fixed `/btw` progress question to one or more eligible agent panes. Needs-input
and error panes are blocked. Before sending keys, Agent Watch verifies the pane
identity and rejects tmux copy mode. For ready panes and panes active in another
tmux client, it moves the cursor to a verified provider composer start. Any
existing single-line draft is submitted as part of the temporary question.
Returned summaries remain in dashboard memory only.

Relevant implementation safeguards include parameterized SQLite queries,
argument-vector subprocess execution, terminal-control sanitization, bounded
transcript reads, owner/path checks for preview files, no-follow HTTP redirect
handling, a single-daemon lock, and an outbox lease. These controls reduce risk;
they do not make untrusted same-user data safe in every environment.

## Operator guidance

- Keep `~/.config/agent-watch/config.toml` mode `0600` and the state directory
  mode `0700`.
- Treat ntfy topics, Telegram bot tokens, Bark URLs, webhook bearer tokens, and
  custom notification commands as credentials.
- Prefer HTTPS and a private/self-hosted notification endpoint for sensitive
  work. `allow_insecure_http=true` is an explicit opt-in to plaintext transport.
- Review backups and diffs after `agent-watch install-hooks` or
  `agent-watch uninstall-hooks`.
- A configured custom notification command executes with the daemon user's
  privileges. Use an absolute executable path and audit it.
- Install the Cursor companion only on the workspace host that runs Agent Watch.
  Keep its socket directory `0700` and socket `0600`; custom socket paths must be
  absolute and owned by the same Unix user.
- Keep `notifications.cursor.include_prompt=false` unless showing recent user
  prompt text in Cursor notifications is acceptable for that workspace.
- Do not expose the SQLite database, daemon socket namespace, or dashboard to
  other users.
- Treat `b` and especially bulk `B` as active model requests. Do not use them on
  sessions whose context should not be summarized on the monitoring terminal.
- Keep Codex CLI and Claude Code versions within the tested matrix, and verify
  detection after upgrades because their internal formats can change.

## Known boundaries

- Notification delivery cannot be exactly-once across process crashes and remote
  services that do not support idempotency keys. A rare duplicate is possible.
- The Cursor socket protects against other Unix users through filesystem modes
  and peer checks. Processes already running as the Agent Watch user remain
  inside the project's trust boundary.
- tmux prompt detection is text-based fallback logic and can miss or misclassify
  changed upstream UI wording.
- Local history is pruned after the configured retention window, but pending
  notification retries and current sessions may remain longer.
- The TUI displays selected conversation excerpts on screen. Terminal access,
  scrollback, recording, and shoulder-surfing are outside this project's control.
- Remote endpoint trust, TLS interception, proxy configuration, and endpoint data
  retention are the operator's responsibility.

This project is independent and is not affiliated with OpenAI or Anthropic.
