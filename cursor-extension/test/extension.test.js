'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

let configuredPath = '';
const shown = [];
const vscodeMock = {
  commands: {
    registerCommand: () => ({ dispose() {} }),
  },
  env: {
    clipboard: { writeText: async () => {} },
  },
  window: {
    showErrorMessage: async (...args) => {
      shown.push(['error', ...args]);
      return undefined;
    },
    showInformationMessage: async (...args) => {
      shown.push(['info', ...args]);
      return undefined;
    },
    showWarningMessage: async (...args) => {
      shown.push(['warning', ...args]);
      return 'Details';
    },
  },
  workspace: {
    getConfiguration: () => ({ get: () => configuredPath }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
  },
};

const originalLoad = Module._load;
Module._load = function loadWithVscodeMock(request, parent, isMain) {
  if (request === 'vscode') {
    return vscodeMock;
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { NotificationSocket } = require('../extension');
Module._load = originalLoad;

function sendRaw(socketPath, data) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(socketPath);
    const response = [];
    client.once('connect', () => {
      client.end(data);
    });
    client.on('data', (chunk) => response.push(chunk));
    client.once('end', () => resolve(Buffer.concat(response).toString('utf8')));
    client.once('error', reject);
  });
}

function sendFrame(socketPath, payload) {
  return sendRaw(socketPath, `${JSON.stringify(payload)}\n`);
}

test('socket server accepts one frame, shows a compact toast, and cleans up', async () => {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'agent-watch-ext-'));
  await fs.promises.chmod(directory, 0o700);
  configuredPath = path.join(directory, 'cursor-notify.sock');
  shown.length = 0;
  const output = {
    lines: [],
    shown: false,
    appendLine(line) { this.lines.push(line); },
    show() { this.shown = true; },
  };
  const server = new NotificationSocket(output);

  try {
    await server.start();
    const metadata = await fs.promises.lstat(configuredPath);
    assert.equal(metadata.isSocket(), true);
    assert.equal(metadata.mode & 0o777, 0o600);

    const response = await sendFrame(configuredPath, {
      body: 'Host: remote-dev\nProject: agent-watch\ntmux: work:1.0',
      events: [{
        project: 'agent-watch',
        prompt: 'Please optimize the Cursor notification content',
        provider: 'codex',
        state: 'needs_input',
        tmux_target: 'work:1.0',
      }],
      title: 'Codex · Needs your response or approval',
    });
    assert.deepEqual(JSON.parse(response), { ok: true });
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(shown, [[
      'warning',
      'Codex · Needs your response\ntmux work:1.0\nPrompt: Please optimize the Cursor notification content',
      'Details',
    ]]);
    assert.equal(output.shown, true);
    assert.match(output.lines.join('\n'), /Host: remote-dev/u);

    const rejected = await sendRaw(
      configuredPath,
      '{"title":"one","body":"one"}\n{"title":"two","body":"two"}\n',
    );
    assert.equal(JSON.parse(rejected).ok, false);
    assert.equal(shown.length, 1);
  } finally {
    await server.stop();
    await fs.promises.rm(directory, { recursive: true, force: true });
  }
  assert.equal(fs.existsSync(configuredPath), false);
});
