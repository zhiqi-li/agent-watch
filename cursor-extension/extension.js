'use strict';

const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

const {
  MAX_PAYLOAD_BYTES,
  ProtocolError,
  encodeAck,
  formatToast,
  parseNotificationLine,
} = require('./protocol');

const MAX_CLIENTS = 32;
const CLIENT_TIMEOUT_MS = 15_000;
const SOCKET_PATH_BYTE_LIMIT = 100;

function defaultSocketPath() {
  const stateDirectory = process.env.AGENT_WATCH_STATE_DIR
    ? expandHome(process.env.AGENT_WATCH_STATE_DIR)
    : path.join(os.homedir(), '.local', 'state', 'agent-watch');
  return path.join(stateDirectory, 'cursor-notify.sock');
}

function expandHome(value) {
  if (value === '~') {
    return os.homedir();
  }
  if (value.startsWith(`~${path.sep}`)) {
    return path.join(os.homedir(), value.slice(2));
  }
  return value;
}

function configuredSocketPath() {
  const setting = vscode.workspace
    .getConfiguration('agentWatch')
    .get('socketPath', '')
    .trim();
  const environment = (process.env.AGENT_WATCH_CURSOR_SOCKET || '').trim();
  const override = setting || environment;
  const socketPath = override ? expandHome(override) : defaultSocketPath();
  if (!path.isAbsolute(socketPath)) {
    throw new Error('agentWatch.socketPath must be an absolute path');
  }
  if (Buffer.byteLength(socketPath) > SOCKET_PATH_BYTE_LIMIT) {
    throw new Error(`Unix socket path is too long: ${socketPath}`);
  }
  return { isDefault: !override, socketPath: path.normalize(socketPath) };
}

async function ensurePrivateParent(socketPath, allowModeRepair) {
  const parent = path.dirname(socketPath);
  let existed = true;
  try {
    await fs.promises.lstat(parent);
  } catch (error) {
    if (error.code !== 'ENOENT') {
      throw error;
    }
    existed = false;
    await fs.promises.mkdir(parent, { mode: 0o700, recursive: true });
  }

  let stat = await fs.promises.lstat(parent);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error(`Socket parent is not a real directory: ${parent}`);
  }

  const uid = typeof process.getuid === 'function' ? process.getuid() : undefined;
  if (uid !== undefined && stat.uid !== uid) {
    throw new Error(`Socket parent is not owned by the current user: ${parent}`);
  }

  if ((stat.mode & 0o077) !== 0 && (allowModeRepair || !existed)) {
    await fs.promises.chmod(parent, 0o700);
    stat = await fs.promises.lstat(parent);
  }
  if ((stat.mode & 0o077) !== 0) {
    throw new Error(`Socket parent must have mode 0700: ${parent}`);
  }
}

function socketIsActive(socketPath) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(socketPath);
    const timer = setTimeout(() => {
      client.destroy();
      reject(new Error(`Timed out checking existing socket: ${socketPath}`));
    }, 500);

    client.once('connect', () => {
      clearTimeout(timer);
      client.destroy();
      resolve(true);
    });
    client.once('error', (error) => {
      clearTimeout(timer);
      if (error.code === 'ECONNREFUSED' || error.code === 'ENOENT') {
        resolve(false);
      } else {
        reject(error);
      }
    });
  });
}

async function removeStaleSocket(socketPath) {
  let before;
  try {
    before = await fs.promises.lstat(socketPath);
  } catch (error) {
    if (error.code === 'ENOENT') {
      return;
    }
    throw error;
  }

  if (!before.isSocket()) {
    throw new Error(`Refusing to replace non-socket path: ${socketPath}`);
  }
  const uid = typeof process.getuid === 'function' ? process.getuid() : undefined;
  if (uid !== undefined && before.uid !== uid) {
    throw new Error(`Refusing to replace a socket owned by another user: ${socketPath}`);
  }
  if (await socketIsActive(socketPath)) {
    throw new Error(`Another Cursor window is already listening on ${socketPath}`);
  }

  const after = await fs.promises.lstat(socketPath);
  if (!after.isSocket() || after.dev !== before.dev || after.ino !== before.ino) {
    throw new Error(`Socket changed while checking it: ${socketPath}`);
  }
  await fs.promises.unlink(socketPath);
}

class NotificationSocket {
  constructor(output) {
    this.output = output;
    this.server = undefined;
    this.clients = new Set();
    this.socketPath = undefined;
    this.socketIdentity = undefined;
  }

  async start() {
    if (process.platform === 'win32') {
      throw new Error('Agent Watch Cursor notifications require a Unix extension host');
    }

    const resolved = configuredSocketPath();
    await ensurePrivateParent(resolved.socketPath, resolved.isDefault);
    await removeStaleSocket(resolved.socketPath);

    const server = net.createServer((client) => this.accept(client));
    this.server = server;
    this.socketPath = resolved.socketPath;

    try {
      await new Promise((resolve, reject) => {
        const onError = (error) => reject(error);
        server.once('error', onError);
        // net.Server has no mode option for Unix sockets. Binding while the
        // event loop is synchronously under a 0177 umask makes the socket 0600
        // from creation; chmod below verifies and repairs it before start ends.
        const previousUmask = process.umask(0o177);
        try {
          server.listen(resolved.socketPath, () => {
            server.removeListener('error', onError);
            resolve();
          });
        } finally {
          process.umask(previousUmask);
        }
      });
      await fs.promises.chmod(resolved.socketPath, 0o600);
      this.socketIdentity = await fs.promises.lstat(resolved.socketPath);
    } catch (error) {
      await this.stop();
      throw error;
    }

    server.on('error', (error) => {
      this.output.appendLine(`Socket server error: ${error.message}`);
    });
    this.output.appendLine(`Listening on ${resolved.socketPath}`);
  }

  accept(client) {
    if (this.clients.size >= MAX_CLIENTS) {
      this.clients.add(client);
      client.once('close', () => this.clients.delete(client));
      client.end(encodeAck(false, 'too many notification clients'), () => {
        client.destroy();
      });
      return;
    }

    this.clients.add(client);
    let buffer = Buffer.alloc(0);
    let closing = false;
    client.setTimeout(CLIENT_TIMEOUT_MS);

    const failAndClose = (message) => {
      if (closing) {
        return;
      }
      closing = true;
      client.end(encodeAck(false, message));
    };

    client.on('data', (chunk) => {
      if (closing) {
        return;
      }
      buffer = Buffer.concat([buffer, chunk], buffer.length + chunk.length);

      const newline = buffer.indexOf(0x0a);
      if (newline !== -1) {
        if (buffer.length !== newline + 1) {
          failAndClose('exactly one notification is allowed per connection');
          return;
        }
        let line = buffer.subarray(0, newline);
        if (line.length > 0 && line[line.length - 1] === 0x0d) {
          line = line.subarray(0, line.length - 1);
        }
        closing = true;
        client.pause();
        this.handleLine(client, line);
      } else if (buffer.length > MAX_PAYLOAD_BYTES) {
        failAndClose(`notification exceeds ${MAX_PAYLOAD_BYTES} bytes`);
      }
    });
    client.on('timeout', () => failAndClose('notification connection timed out'));
    client.on('end', () => failAndClose('notification must end with a newline'));
    client.on('error', (error) => {
      this.output.appendLine(`Notification client error: ${error.message}`);
    });
    client.on('close', () => this.clients.delete(client));
  }

  handleLine(client, line) {
    let notification;
    try {
      notification = parseNotificationLine(line);
      this.showNotification(notification);
      client.end(encodeAck(true, undefined, notification.id));
    } catch (error) {
      const message = error instanceof ProtocolError
        ? error.message
        : 'notification could not be displayed';
      this.output.appendLine(`Rejected notification: ${message}`);
      client.end(encodeAck(false, message));
    }
  }

  showNotification(notification) {
    const message = formatToast(notification);
    const detailsAction = 'Details';
    let shown;
    if (notification.severity === 'error') {
      shown = vscode.window.showErrorMessage(message, detailsAction);
    } else if (notification.severity === 'warning') {
      shown = vscode.window.showWarningMessage(message, detailsAction);
    } else {
      shown = vscode.window.showInformationMessage(message, detailsAction);
    }
    Promise.resolve(shown)
      .then((action) => {
        if (action === detailsAction) {
          this.output.appendLine(`[${new Date().toISOString()}] ${notification.title}`);
          for (const line of notification.body.split(/\r?\n/u)) {
            this.output.appendLine(`  ${line}`);
          }
          this.output.show(true);
        }
      })
      .catch((error) => {
        this.output.appendLine(`Could not show notification: ${error.message}`);
      });
  }

  async stop() {
    const server = this.server;
    const socketPath = this.socketPath;
    const identity = this.socketIdentity;
    this.server = undefined;
    this.socketPath = undefined;
    this.socketIdentity = undefined;

    for (const client of this.clients) {
      client.destroy();
    }
    this.clients.clear();

    if (server) {
      await new Promise((resolve) => server.close(() => resolve()));
    }
    if (socketPath && identity) {
      try {
        const current = await fs.promises.lstat(socketPath);
        if (current.isSocket() && current.dev === identity.dev && current.ino === identity.ino) {
          await fs.promises.unlink(socketPath);
        }
      } catch (error) {
        if (error.code !== 'ENOENT') {
          this.output.appendLine(`Could not remove socket: ${error.message}`);
        }
      }
    }
  }
}

let notificationSocket;
let output;
let restartPromise = Promise.resolve();

function reportSocketStartError(error) {
  output.appendLine(`Could not start notification socket: ${error.message}`);
  vscode.window.showErrorMessage(`Agent Watch notifications unavailable: ${error.message}`);
}

function restartNotificationSocket() {
  restartPromise = restartPromise
    .then(async () => {
      await notificationSocket.stop();
      await notificationSocket.start();
    })
    .catch(reportSocketStartError);
  return restartPromise;
}

async function activate(context) {
  output = vscode.window.createOutputChannel('Agent Watch');
  notificationSocket = new NotificationSocket(output);
  context.subscriptions.push(output);

  context.subscriptions.push(vscode.commands.registerCommand('agentWatch.showSocketPath', async () => {
    let socketPath;
    try {
      socketPath = notificationSocket.socketPath || configuredSocketPath().socketPath;
    } catch (error) {
      vscode.window.showErrorMessage(`Agent Watch: ${error.message}`);
      return;
    }
    const action = await vscode.window.showInformationMessage(
      `Agent Watch socket: ${socketPath}`,
      'Copy',
    );
    if (action === 'Copy') {
      await vscode.env.clipboard.writeText(socketPath);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('agentWatch.showOutput', () => {
    output.show(true);
  }));
  context.subscriptions.push(vscode.commands.registerCommand('agentWatch.testCursorNotification', () => {
    vscode.window.showInformationMessage(
      'Agent Watch · Cursor notification test\nNative notifications are working in this window.',
    );
  }));
  try {
    await notificationSocket.start();
  } catch (error) {
    reportSocketStartError(error);
  }
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((event) => {
    if (event.affectsConfiguration('agentWatch.socketPath')) {
      void restartNotificationSocket();
    }
  }));
}

async function deactivate() {
  if (notificationSocket) {
    await restartPromise;
    await notificationSocket.stop();
  }
}

module.exports = {
  NotificationSocket,
  activate,
  configuredSocketPath,
  deactivate,
};
