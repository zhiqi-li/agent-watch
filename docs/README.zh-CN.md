# Agent Watch

> 用于 Codex CLI 和 Claude Code 的实验性、非官方本地监控工具。本项目与
> OpenAI 或 Anthropic 无隶属、背书或支持关系。

[English](../README.md) · [隐私说明](../PRIVACY.md) ·
[安全策略](../SECURITY.md) · [参与贡献](../CONTRIBUTING.md) ·
[更新记录](../CHANGELOG.md)

Agent Watch 监控当前 Linux 用户拥有的 Codex CLI 和 Claude Code 会话。它提供终端
仪表盘，并可在一轮结束、Agent 需要输入、运行失败或进程消失时发送提醒。

![使用合成演示数据的 Agent Watch 终端仪表盘](agent-watch-demo.svg)

截图由 [`scripts/render-demo.py`](../scripts/render-demo.py) 使用合成演示内容生成，不包含
真实会话名、提示词、路径、tmux 位置或其他用户数据。

## 功能

- 综合原生 hooks、Codex rollout 事件、Claude 会话状态、进程元数据和保守的 tmux
  画面兜底判断状态。
- 提供 Claude Code 风格的只读终端面板，显示项目、状态、最近活动时间和
  `tmux session:window.pane`；按 Enter 可进入准确的目标 pane。
- 宽屏预览区可显示最近请求、最近进展和工具类型。对话预览默认隐藏，可按 `p` 在当前
  UI 中临时显示或隐藏。
- 使用 SQLite outbox 去重和重试，支持 console、tmux、桌面、ntfy、Telegram、
  webhook、Bark 和自定义命令通知。
- 运行中的会话在一段时间没有终端或会话文件活动后，会被保守标记为“可能卡住”。这只是
  提示，不代表任务已经失败。

Agent Watch 优先使用提供方的明确生命周期信号。只有 hooks 或本地会话文件没有暴露的
交互提示才使用 tmux 文本匹配兜底。同样，提供方报告“一轮结束”并不表示用户的整个
目标已经成功完成。

## 环境要求

- Linux，并且具有 `/proc`；只监控当前 Unix 用户可见的进程。
- Python 3.11 或更高版本。
- 强烈建议安装 tmux。会话定位、精确 pane 跳转和 tmux 通知依赖它。
- 推荐使用 [pipx](https://pipx.pypa.io/) 安装。

## 安装

推荐使用 pipx 从 GitHub 安装最新版本：

```bash
pipx install "git+https://github.com/zhiqi-li/agent-watch.git@v0.2.1"
agent-watch --version
```

升级或卸载：

```bash
pipx upgrade agent-watch
pipx uninstall agent-watch
```

卸载前请按下文移除已安装的 Codex 和 Claude hooks。仅卸载 pipx 包不会自动删除 hooks、
配置或本地状态。

## 配置与 hooks

复制示例配置，并在写入通知凭据前限制文件权限：

```bash
mkdir -p ~/.config/agent-watch
curl -fsSL \
  https://raw.githubusercontent.com/zhiqi-li/agent-watch/main/config.example.toml \
  -o ~/.config/agent-watch/config.toml
chmod 600 ~/.config/agent-watch/config.toml

agent-watch install-hooks
agent-watch test-notification
```

`install-hooks` 会把 Agent Watch 配置合并到 `~/.codex/hooks.json` 和
`~/.claude/settings.json`，修改前会创建备份。Claude Code 通常会动态加载 hooks；
Codex 可能需要在一个会话中运行 `/hooks` 并信任一次命令。无需仅为安装 hooks 重启
正在运行的任务。

只移除 Agent Watch 自己的 hooks、保留其他 hooks：

```bash
agent-watch uninstall-hooks
```

主配置文件位于 `~/.config/agent-watch/config.toml`。常用配置如下：

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

所有选项见 [config.example.toml](../config.example.toml)。也可通过 `--config`、
`--state-dir`、`AGENT_WATCH_CONFIG` 或 `AGENT_WATCH_STATE_DIR` 覆盖默认路径。修改
配置后需要重启 daemon。

默认通知只输出到本机 console 和已连接的 tmux 客户端。只有显式配置通知渠道才会产生
网络请求。公共 ntfy 主题名可能相当于访问凭据；敏感环境应使用带认证的长随机主题或
私有服务器。除非明确设置 `allow_insecure_http = true`，否则拒绝明文 HTTP 端点。

## 运行 daemon

选择一种 daemon 方式，不要同时运行 systemd 和 tmux 两个 daemon。进程锁会阻止
重复实例。

### systemd 用户服务（推荐）

安装仓库提供的加固用户单元
[systemd/agent-watch.service](../systemd/agent-watch.service)：

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

该单元假设 pipx 使用默认路径 `~/.local/bin/agent-watch`。如果设置了不同的
`PIPX_BIN_DIR`，请修改 `ExecStart`。如果希望退出 SSH 后用户服务仍继续运行，可由
管理员为该用户启用 systemd lingering。

部分发行版会禁用 systemd 沙箱依赖的非特权 namespace。如果单元在创建 namespace
时失败，请让管理员检查加固选项，或改用下方的 tmux 方式。

### tmux（无 systemd 的节点）

```bash
tmux new-session -d -s agent-watch -n daemon \
  "$HOME/.local/bin/agent-watch daemon"
tmux new-window -t agent-watch -n dashboard \
  "$HOME/.local/bin/agent-watch ui"
tmux attach -t agent-watch:dashboard
```

关闭 dashboard 不会停止 daemon。只重启 daemon pane：

```bash
tmux respawn-pane -k -t agent-watch:daemon \
  "$HOME/.local/bin/agent-watch daemon"
```

## 使用

```bash
agent-watch                       # 全屏仪表盘
agent-watch ui                    # 同上
agent-watch status                # 静态摘要
agent-watch status --json         # 默认脱敏的机器可读状态
agent-watch status --json --full  # 包含敏感路径、ID 和消息字段
agent-watch daemon --once --no-notify-existing
agent-watch test-notification
agent-watch clear-history --yes       # 需要先停止 daemon
```

仪表盘快捷键：

```text
↑/↓ 或 j/k      选择会话        Enter       进入所选 tmux pane
tmux 前缀键 + L  返回 Agent Watch /           搜索
f                切换筛选        g/G         第一项/最后一项
p                显示/隐藏预览   r           刷新
?                帮助            q           只关闭 UI
```

需要输入、运行失败、一轮结束、进程退出、运行中和自动等待会被区分显示。运行中的会话
会显示距离最近一次真实终端、rollout 或 transcript 活动的时间。默认连续十分钟无活动
会显示“可能卡住”，可通过 `monitor.activity_stale_seconds` 调整。

存在 tmux 位置时，列表和详情会显示 `session:window.pane`；自定义 tmux server 也会
标明。Enter 会定位到准确 pane。多个 tmux 客户端导致触发方不明确时会安全拒绝跳转；
目标位于另一个 tmux server 时会给出完整的手动连接命令。

## 隐私默认值

Agent Watch 会读取当前用户的进程元数据、有限长度的 tmux pane 文本以及本地
Codex/Claude 会话文件。所选会话的对话预览默认关闭。启用后也只在 dashboard 进程
内存中生成，不会写入 SQLite 或发送到远端通知。预览会尽量排除 reasoning、系统指令、
工具参数和工具输出，但它不是正式的数据防泄漏边界。

默认远端通知不包含 cwd、提示词或回答片段、tmux socket 绝对路径和 pane ID。
`agent-watch status --json` 同样默认脱敏；`--full` 是明确的敏感数据选择。

本地数据库、outbox 和通知历史位于 `~/.local/state/agent-watch/`。已投递通知历史和
过期会话默认保留 30 天；等待重试的通知不会被保留期清理。停止 daemon 后，可使用
`agent-watch clear-history --yes` 删除数据库历史、hook spool 文件和 hook 错误日志。

在共享终端或使用远端通知服务前，请阅读 [PRIVACY.md](../PRIVACY.md)。

## 兼容性与实验性说明

Agent Watch 会读取 Codex CLI 和 Claude Code 生成的内部本地格式。这些格式不是本项目
控制的稳定 API。提供方升级后可能出现漏报、误报或预览缺失。原生 hooks 是首选信号，
tmux 文本匹配仅为保守兜底。

当前实测矩阵：

| 组件 | 实测版本 | 说明 |
|---|---:|---|
| Python | 3.12.3 | 支持目标为 3.11+ |
| Rich | 14 | TUI 渲染 |
| tmux | 3.4 | 默认和自定义 socket |
| Codex CLI | 0.142.5 | rollout 事件和 hooks |
| Claude Code | 2.1.202 | session/transcript 文件和 hooks |

其他版本可能可用，但不作保证。提交兼容性问题时请附版本号和脱敏后的状态输出。不要上传
真实对话、token、配置、数据库或 transcript。

## 排障

- **没有发现会话：**确认 daemon 和 Agent 属于同一 Unix 用户；运行
  `agent-watch daemon --once --no-notify-existing` 和 `agent-watch status --json`。
- **仪表盘显示 daemon 已停止：**检查 `systemctl --user status agent-watch`、
  `journalctl --user -u agent-watch` 或
  `tmux capture-pane -p -t agent-watch:daemon`。
- **hooks 没有触发：**重新运行 `agent-watch install-hooks`；在 Codex 中运行
  `/hooks`；检查 `~/.local/state/agent-watch/hook-errors.log`。
- **远端通知失败：**运行 `agent-watch test-notification`，然后检查 URL、token、代理、
  防火墙和 HTTPS 策略。失败投递会指数退避重试。
- **UI 无法进入 tmux pane：**pane 可能已经消失、位于其他 tmux server，或多个客户端
  同时查看 dashboard。请使用 UI 显示的完整手动命令。
- **UI 无法启动：**使用交互式 TTY，确认已安装 Rich，并尝试至少 100 列宽的终端。
- **安静任务被标记为“可能卡住”：**有些工具会长时间无输出。调大
  `activity_stale_seconds`，并在操作前核对真实 Agent 状态。

## 项目状态

Agent Watch 是实验性的单用户 Linux 工具，不是托管监控服务。安全修复以尽力而为方式
提供给最新的 `0.x` 版本和 `main`。安全问题请按 [SECURITY.md](../SECURITY.md) 私下
报告；普通问题请使用合成或脱敏数据提交到
<https://github.com/zhiqi-li/agent-watch/issues>。

Agent Watch 使用 [MIT License](../LICENSE)。OpenAI、Codex、Anthropic 和 Claude 是
各自所有者的商标；本项目仅为说明兼容性而使用这些名称。
