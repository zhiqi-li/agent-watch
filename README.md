# Agent Watch

[![CI](https://github.com/zhiqi-li/agent-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/zhiqi-li/agent-watch/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Experimental, unofficial local monitor for Codex CLI and Claude Code. This
> project is not affiliated with, endorsed by, or supported by OpenAI or
> Anthropic.

[中文](#中文) · [English](#english) · [Privacy](PRIVACY.md) ·
[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

Agent Watch watches Codex CLI and Claude Code sessions owned by the current
Linux user. It presents a terminal dashboard and can notify you when a turn
finishes, an agent needs input, a run fails, or a process disappears.

## 中文

### 功能

- 综合原生 hooks、Codex rollout、Claude 会话状态、进程和 tmux 画面判断状态。
- Claude Code 风格的只读终端面板，显示项目、状态、活动时间和
  `tmux session:window.pane`，按 Enter 可进入目标 pane。
- 宽屏右栏可按 `p` 临时显示最近请求、最近进展和工具类型；默认隐藏，且不显示
  reasoning、工具参数或工具输出。
- SQLite outbox 去重、失败重试，并支持 console、tmux、桌面、ntfy、Telegram、
  webhook、Bark 和自定义命令。
- 运行中长时间没有终端或会话文件更新时，在 UI 中保守标记“可能卡住”。这不是
  对任务失败的确定判断。

### 环境要求

- Linux（依赖 `/proc`；仅监控当前 Unix 用户可见的进程）
- Python 3.11+
- tmux（强烈建议；定位、跳转和 tmux 通知需要它）
- [pipx](https://pipx.pypa.io/)（推荐安装方式）

### 安装

推荐使用 pipx 从 GitHub 安装：

```bash
pipx install "git+https://github.com/zhiqi-li/agent-watch.git"
agent-watch --version
```

升级或卸载：

```bash
pipx upgrade agent-watch
pipx uninstall agent-watch
```

### 配置与 hooks

```bash
mkdir -p ~/.config/agent-watch
curl -fsSL \
  https://raw.githubusercontent.com/zhiqi-li/agent-watch/main/config.example.toml \
  -o ~/.config/agent-watch/config.toml
chmod 600 ~/.config/agent-watch/config.toml

agent-watch install-hooks
agent-watch test-notification
```

`install-hooks` 会合并配置到 `~/.codex/hooks.json` 和
`~/.claude/settings.json`，修改前会创建备份。Claude Code 通常会热加载；Codex 可能
需要在任一会话中运行 `/hooks` 并信任一次。无需为了安装 hooks 重启正在运行的任务。
相关上游接口说明见 [Codex Hooks](https://developers.openai.com/codex/hooks) 与
[Claude Code Hooks](https://code.claude.com/docs/en/hooks)。

移除 Agent Watch hooks（保留其他 hooks）：

```bash
agent-watch uninstall-hooks
```

主配置位于 `~/.config/agent-watch/config.toml`。常用项：

```toml
[monitor]
interval_seconds = 5
ready_delay_seconds = 12
activity_stale_seconds = 600
retention_days = 30
ignore_tmux_sessions = ["agent-watch"]

[ui]
conversation_preview = false

[notifications]
console = true
tmux = true
include_cwd = false
include_message_preview = false
include_tmux_socket = false
allow_insecure_http = false

[notifications.ntfy]
url = ""
token = ""
```

完整选项见 [config.example.toml](config.example.toml)。也可用 `--config`、
`--state-dir`，或环境变量 `AGENT_WATCH_CONFIG`、`AGENT_WATCH_STATE_DIR` 指定位置。
使用自定义路径时，请带相同全局参数重新运行 `install-hooks`；安装器会把配置与状态路径
固定进 hook 命令。修改配置内容后需重启 daemon。

### 运行 daemon

选择一种方式；不要同时运行两个 daemon。进程锁会阻止重复实例。

#### systemd 用户服务（推荐）

安装仓库提供的加固用户单元
[systemd/agent-watch.service](systemd/agent-watch.service)：

```bash
mkdir -p ~/.config/systemd/user
curl -fsSL \
  https://raw.githubusercontent.com/zhiqi-li/agent-watch/main/systemd/agent-watch.service \
  -o ~/.config/systemd/user/agent-watch.service
systemctl --user daemon-reload
systemctl --user enable --now agent-watch.service
systemctl --user status agent-watch.service
journalctl --user -u agent-watch.service -f
```

该 unit 假设 pipx 命令入口位于 `~/.local/bin/agent-watch`；若设置了
`PIPX_BIN_DIR`，请把 `ExecStart` 改成 `command -v agent-watch` 显示的绝对路径。
部分禁用非特权 user namespace 的容器/发行版无法应用 unit 的加固项，此时使用下面的
tmux 方式运行。

如果希望退出 SSH 后用户服务仍运行，可由管理员启用 user lingering。

#### tmux（无 systemd 时）

```bash
tmux new-session -d -s agent-watch -n daemon \
  "$HOME/.local/bin/agent-watch daemon"
tmux new-window -t agent-watch -n dashboard \
  "$HOME/.local/bin/agent-watch ui"
tmux attach -t agent-watch:dashboard
```

关闭 dashboard 不会停止 daemon。重启 daemon pane：

```bash
tmux respawn-pane -k -t agent-watch:daemon \
  "$HOME/.local/bin/agent-watch daemon"
```

### 使用

```bash
agent-watch                 # 全屏 UI
agent-watch ui              # 同上
agent-watch status          # 静态摘要
agent-watch status --json   # 默认脱敏的机器可读状态
agent-watch status --json --full  # 含路径、会话 ID、正文等敏感本机字段
agent-watch daemon --once --no-notify-existing
agent-watch test-notification
# 清理前先停止 daemon；完成后再启动
agent-watch clear-history --yes
```

UI 快捷键：`↑/↓` 或 `j/k` 选择，`Enter` 进入 tmux，`/` 搜索，`f` 筛选，
`p` 显示/隐藏对话预览，`r` 刷新，`?` 帮助，`q` 仅关闭 UI。

### 隐私默认值

Agent Watch 会在本机读取进程元数据、tmux 画面以及 Codex/Claude 的本地会话文件。
对话预览默认关闭，启用后也只在 UI 进程内存中生成，不写入数据库或远端通知。默认
远端通知不包含 cwd、提示词、回答正文、tmux socket 绝对路径或 pane ID；只有显式
配置的通知目标会产生网络请求。数据库和通知历史保存在
`~/.local/state/agent-watch/`，默认保留 30 天；停止 daemon 后可用 `clear-history`
清除数据库历史、hook spool 与错误日志。共享
终端或敏感环境使用前请阅读
[PRIVACY.md](PRIVACY.md)。

### 兼容性与实验性说明

Agent Watch 读取 Codex 与 Claude Code 的内部本地文件格式。这些不是本项目控制的稳定
API，工具升级后可能发生变化，导致漏报、误报或预览缺失。原生 hooks 是首选信号，
tmux 文本匹配只是保守兜底。当前实测矩阵：

| 组件 | 实测版本 | 说明 |
|---|---:|---|
| Python | 3.12.3 | 支持目标为 3.11+ |
| Rich | 14 | TUI 渲染 |
| tmux | 3.4 | 默认及自定义 socket |
| Codex CLI | 0.142.5 | rollout + hooks |
| Claude Code | 2.1.202 | session/transcript + hooks |

其他版本可能可用，但尚未验证。提交兼容性问题时请附版本号和脱敏后的状态输出，不要
上传真实对话、token 或 transcript。

### 排障

- **没有发现会话：**确认 daemon 与 agent 属于同一 Unix 用户；运行
  `agent-watch daemon --once --no-notify-existing` 和 `agent-watch status --json`。
- **面板显示 daemon 停止：**查看 `systemctl --user status agent-watch`、
  `journalctl --user -u agent-watch`，或 `tmux capture-pane -p -t agent-watch:daemon`。
- **面板显示“最近扫描异常”：**daemon 进程仍在，但最近一次完整扫描失败；查看同一
  journal/tmux 日志。错误消失并完成下一轮扫描后，健康状态会自动恢复。
- **hooks 没触发：**重新运行 `agent-watch install-hooks`；Codex 中运行 `/hooks`；检查
  `~/.local/state/agent-watch/hook-errors.log`。
- **远端通知失败：**运行 `agent-watch test-notification`，确认 URL、token、代理和
  防火墙；默认只允许 HTTPS，确需明文 HTTP 时必须显式设置
  `allow_insecure_http = true`；失败事件会指数退避重试。
- **无法进入 tmux：**目标可能在另一个 tmux server、pane 已消失，或多个客户端同时
  查看 dashboard；按 UI 给出的完整命令手动连接。
- **UI 无法启动：**确认当前是交互式 TTY、Rich 已安装，并尝试把终端扩大到 100 列。
- **误报“可能卡住”：**安静运行的工具可能没有输出；调大
  `activity_stale_seconds`，并以实际会话为准。

## English

### What it does

- Combines native hooks, Codex rollouts, Claude session state, process metadata,
  and a conservative tmux fallback.
- Provides a Claude Code-inspired TUI with state, last activity, project,
  `tmux session:window.pane`, exact-pane navigation, and a bounded conversation
  preview that is hidden by default and toggled with `p`.
- Delivers deduplicated notifications through local and optional remote channels.
- Flags a running session as “possibly stalled” after a configurable period with
  no visible output or session-file activity. This is a hint, not a failure verdict.

### Requirements and installation

Agent Watch requires Linux with `/proc` and Python 3.11+. tmux is strongly
recommended:

```bash
pipx install "git+https://github.com/zhiqi-li/agent-watch.git"
agent-watch install-hooks
agent-watch --version
```

Copy [config.example.toml](config.example.toml) to
`~/.config/agent-watch/config.toml`, set file mode `0600`, and enable only the
notification channels you need. The default channels are local console and tmux;
remote message previews, cwd sharing, and tmux socket sharing are disabled.

### Run it

For a systemd user service, use the unit shown in the [Chinese setup](#systemd-用户服务推荐)
and run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-watch.service
agent-watch ui
```

On hosts without systemd:

```bash
tmux new-session -d -s agent-watch -n daemon \
  "$HOME/.local/bin/agent-watch daemon"
tmux new-window -t agent-watch -n dashboard \
  "$HOME/.local/bin/agent-watch ui"
tmux attach -t agent-watch:dashboard
```

Useful commands:

```bash
agent-watch status
agent-watch status --json
agent-watch status --json --full  # sensitive, explicit opt-in
agent-watch test-notification
agent-watch clear-history --yes  # stop the daemon first
agent-watch uninstall-hooks
```

Installing hooks merges entries into `~/.codex/hooks.json` and
`~/.claude/settings.json` after making backups. Claude usually reloads them
dynamically; Codex may ask you to trust the hook through `/hooks`. Uninstalling
Agent Watch does not automatically remove hooks, so run `agent-watch
uninstall-hooks` first. If you use custom config/state paths, pass those global
options when installing hooks; the generated commands pin both paths.

### Privacy, compatibility, and support status

Conversation previews are disabled by default and, when enabled, are generated
locally in dashboard memory. They are not written to SQLite or sent remotely. No
telemetry is collected, and network requests are made only for notification
channels you configure. Local state and notification history default to a 30-day
retention window. See
[PRIVACY.md](PRIVACY.md) before using the tool on a shared terminal.

Codex and Claude transcript/session parsing is **experimental** because these are
internal formats, not stable APIs owned by this project. The tested matrix is
Python 3.12.3, Rich 14, tmux 3.4, Codex CLI 0.142.5, and Claude Code 2.1.202.
Other versions may work but are not guaranteed.

For troubleshooting, start with `agent-watch daemon --once
--no-notify-existing`, `agent-watch status --json`, the daemon journal/tmux pane,
and `~/.local/state/agent-watch/hook-errors.log`. Redact paths, prompts, tokens,
and transcripts before filing an issue.

## Project status

This is an experimental single-user Linux utility, not a hosted monitoring
service. “Turn complete” does not mean the user's larger task succeeded, and
“possibly stalled” does not prove a deadlock. Please report security issues via
[SECURITY.md](SECURITY.md) and ordinary bugs at
<https://github.com/zhiqi-li/agent-watch/issues>.

Agent Watch is an independent community project. OpenAI, Codex, Anthropic, and
Claude are trademarks of their respective owners; their names are used only to
describe compatibility.
