# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers
follow [Semantic Versioning](https://semver.org/) while the `0.x` API remains
experimental.

## [Unreleased]

### Added

- An exited-session history page, opened from a single `Exited sessions` entry
  at the bottom of the dashboard. Enter resumes an available record with its
  original working directory and provider session ID in a new, independent tmux
  session; unavailable records explain why they cannot be resumed, and Esc
  returns to the main list.

### Changed

- Exited sessions are no longer shown individually in the main session list;
  their existing configured history retention period still applies.

## [0.2.1] - 2026-07-07

### Added

- A reproducible README dashboard screenshot generated entirely from synthetic
  session data, with a regression test that prevents real host data from being
  committed to the image.

### Changed

- English is now the default language for the TUI, CLI messages, notifications,
  README, and example configuration.
- The Chinese guide is retained as a separate translation under `docs/`.

## [0.2.0] - 2026-07-07

### Added

- Standard Python packaging, pipx installation, CI, an MIT license, a hardened
  systemd user unit, and public operations/privacy/security/contribution docs.
- Documented systemd and standalone tmux daemon deployment paths.
- Multi-signal monitoring for Codex CLI and Claude Code through native hooks,
  provider session artifacts, process discovery, and tmux fallback detection.
- SQLite session state, claimed outbox delivery, per-channel retry tracking,
  stale-event cancellation, and single-daemon locking.
- Local and remote notification adapters for console, tmux, desktop, command,
  webhook, ntfy, Telegram, and Bark.
- Claude Code-inspired full-screen terminal dashboard with search, filtering,
  responsive layouts, exact tmux pane navigation, and static/JSON status views.
- Last-activity tracking and a conservative “possibly stalled” UI indicator.
- Bounded, on-demand conversation previews for the selected Codex or Claude
  session, excluding reasoning and tool payloads.
- Hook installation/uninstallation with configuration backups.
- Hook commands pin custom config/state paths, transient alerts resolve on later
  lifecycle events, and immediate hook replays are deduplicated.
- Unit coverage for lifecycle mapping, outbox concurrency/CAS, retry behavior,
  hook merging, transcript filtering, terminal sanitization, responsive rendering,
  and fail-closed tmux switching.

### Security

- Notification commands use argument vectors rather than a shell.
- Remote HTTP helpers reject redirects.
- TUI text strips terminal controls, and preview readers enforce bounded reads,
  owner checks, and history-root containment.
- Remote notifications omit cwd, message previews, tmux socket paths, and pane IDs
  by default; plaintext HTTP requires explicit opt-in.
- Configuration, SQLite/WAL/SHM permissions, hook ownership, and automatic
  30-day history retention are enforced and regression-tested.
- Conversation previews are hidden by default and JSON status output is redacted
  unless `--full` is requested.
- Configuration values are type/range checked, UI-only mode does not retry an
  empty notification set, and daemon liveness is separated from scan success.

[Unreleased]: https://github.com/zhiqi-li/agent-watch/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/zhiqi-li/agent-watch/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/zhiqi-li/agent-watch/releases/tag/v0.2.0
