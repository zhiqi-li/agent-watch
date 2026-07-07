# Agent Watch Notifications for Cursor and VS Code

This small workspace extension receives Agent Watch notification payloads over a
private Unix domain socket and displays native information, warning, or error
toasts. Because it is a workspace extension, Cursor installs and runs it on the
remote extension host for Remote SSH workspaces.

## Install

From the repository root, build a VSIX without downloading npm dependencies and
install it in Cursor:

```sh
VSIX="$(python3 scripts/package_cursor_extension.py)"
cursor --install-extension "$VSIX" --force
```

For a Remote SSH window, use **Extensions: Install from VSIX...** in that window
and make sure the extension is listed under the SSH host rather than only under
Local. Reload the Cursor window after installation. The extension starts after
startup and logs its socket path in the **Agent Watch** output channel.

Run **Agent Watch: Test Cursor Notification** from the command palette to check
the native toast without Agent Watch. **Agent Watch: Show Output** opens the
extension log, and **Agent Watch: Show Cursor Notification Socket Path** shows
the resolved socket path.

The socket path is resolved in this order: the `agentWatch.socketPath` setting,
`AGENT_WATCH_CURSOR_SOCKET`, then the default:

```text
${AGENT_WATCH_STATE_DIR:-~/.local/state/agent-watch}/cursor-notify.sock
```

Set the machine-scoped `agentWatch.socketPath` setting or
`AGENT_WATCH_CURSOR_SOCKET` to an absolute path to override it. `~` is accepted
at the start of either value. A custom socket's parent directory must be owned
by the extension-host user, must not be a symlink, and must have mode `0700`.

## Configure Agent Watch

Enable the first-class Cursor notification channel:

```toml
[notifications.cursor]
enabled = true
# socket = "/absolute/private/directory/cursor-notify.sock"
include_prompt = true
```

Then run:

```sh
agent-watch test-notification
```

The `socket` value can stay empty when both processes use the default. If Agent
Watch uses a non-default state directory or the extension has a custom socket,
configure the same absolute socket path on both sides.

For manual troubleshooting, `agent-watch cursor-notify --socket PATH` reads one
notification JSON object from standard input and forwards it to the extension.
Run it with `--help` for all bridge options.

The command must run on the same host and as the same Unix user as Cursor's
workspace extension host. A local Agent Watch process cannot directly reach a
socket on a Remote SSH host.

## Protocol and security

Clients send exactly one UTF-8 JSON object and newline per connection. The line
may be at most 256 KiB and
must contain non-empty string `title` and `body` fields. An optional `severity`
field may be `info`, `warning`, or `error`; otherwise severity is inferred from
the `events[].state` values. The server replies after scheduling the native toast:

```json
{"ok":true}
```

Invalid input receives `{"ok":false,"error":"..."}`. An optional short string
`id` is echoed in successful acknowledgements.

The toast itself is intentionally compact: it omits the opaque host/container
ID and shows the state, tmux target, and an optional bounded user-prompt excerpt.
Prompt excerpts require the explicit `include_prompt = true` opt-in. Select
**Details** on a toast to write and open the complete body in the **Agent Watch**
output channel. Cursor may retain Output channel content in its own
extension-host logs.

The extension requires a private parent directory (`0700`), creates the socket
as `0600`, refuses to replace non-socket paths or another user's socket, and only
removes an existing socket after confirming that it is stale. This protects the
transport from other Unix users; processes already running as the same user are
inside Agent Watch's trust boundary.

Only one Cursor window can listen on a particular socket. Give additional
windows distinct `agentWatch.socketPath` values if they need separate listeners.

## Development

```sh
npm test
npm run check
npm run package
```

The extension uses only Node.js built-ins and the VS Code extension API, so it
has no runtime npm dependencies.
