'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const {
  MAX_PAYLOAD_BYTES,
  ProtocolError,
  encodeAck,
  formatToast,
  parseNotificationLine,
} = require('../protocol');

function line(value) {
  return Buffer.from(JSON.stringify(value));
}

test('parses an Agent Watch payload and defaults to info', () => {
  const result = parseNotificationLine(line({
    app: 'agent-watch',
    body: 'Host: devbox\nProject: demo',
    events: [{ state: 'ready' }],
    title: 'Codex · Ready for review',
  }));
  assert.equal(result.title, 'Codex · Ready for review');
  assert.equal(result.severity, 'info');
});

test('formats a compact single-session toast without the host line', () => {
  const notification = parseNotificationLine(line({
    body: 'Host: remote-dev\nProject: agent-watch\ntmux: work:1.0',
    events: [{
      project: 'agent-watch',
      prompt: 'Please optimize the Cursor notification content',
      provider: 'codex',
      state: 'needs_input',
      state_label: 'Needs your response or approval',
      tmux_target: 'work:1.0',
    }],
    title: 'Codex · Needs your response or approval',
  }));
  assert.equal(
    formatToast(notification),
    'Codex · Needs your response\ntmux work:1.0\nPrompt: Please optimize the Cursor notification content',
  );
  assert.doesNotMatch(formatToast(notification), /remote-dev/u);
});

test('formats a bounded batch summary', () => {
  const notification = parseNotificationLine(line({
    body: 'Host: remote-dev',
    events: [
      { project: 'one', provider: 'codex', state: 'ready' },
      { project: 'two', provider: 'claude', state: 'error' },
      { project: 'three', provider: 'codex', state: 'exited' },
      { project: 'four', provider: 'claude', state: 'needs_input' },
    ],
    title: 'Agent Watch · 4 sessions need attention',
  }));
  assert.equal(
    formatToast(notification),
    'Agent Watch · 4 sessions need attention\nCodex · Turn finished · one  |  Claude · Failed · two  |  Codex · Process exited · three  |  +1 more',
  );
});

test('truncates long emoji prompts without splitting surrogate pairs', () => {
  const notification = parseNotificationLine(line({
    body: 'tmux: work:1.0',
    events: [{
      prompt: '🧪'.repeat(220),
      provider: 'codex',
      state: 'needs_input',
      tmux_target: 'work:1.0',
    }],
    title: 'Codex · Needs your response',
  }));
  const promptLine = formatToast(notification).split('\n')[2];
  const promptCharacters = Array.from(promptLine.slice('Prompt: '.length));
  assert.equal(promptCharacters.length, 180);
  assert.equal(promptCharacters.at(-1), '…');
  assert.equal(promptCharacters.slice(0, -1).every((value) => value === '🧪'), true);
});

test('uses explicit severity and echoes a valid id in acknowledgements', () => {
  const result = parseNotificationLine(line({
    body: 'Compilation failed',
    id: 'notice-7',
    severity: 'error',
    title: 'Build error',
  }));
  assert.equal(result.severity, 'error');
  assert.deepEqual(JSON.parse(encodeAck(true, undefined, result.id)), {
    id: 'notice-7',
    ok: true,
  });
});

test('infers error and warning severity from event states', () => {
  const error = parseNotificationLine(line({
    body: 'Host: devbox',
    events: [{ state: 'needs_input' }, { state: 'error' }],
    title: 'Two sessions',
  }));
  const warning = parseNotificationLine(line({
    body: 'Host: devbox',
    events: [{ state: 'exited' }],
    title: 'Session exited',
  }));
  assert.equal(error.severity, 'error');
  assert.equal(warning.severity, 'warning');
});

test('rejects malformed, oversized, and invalid UTF-8 input', () => {
  assert.throws(() => parseNotificationLine(Buffer.from('[]')), ProtocolError);
  assert.throws(() => parseNotificationLine(line({ title: 'Missing body' })), ProtocolError);
  assert.throws(
    () => parseNotificationLine(Buffer.alloc(MAX_PAYLOAD_BYTES + 1, 0x20)),
    /exceeds/,
  );
  assert.throws(() => parseNotificationLine(Buffer.from([0xc3, 0x28])), /UTF-8/);
});

test('rejects unsupported fields used by the UI', () => {
  assert.throws(() => parseNotificationLine(line({
    body: 'Body',
    severity: 'critical',
    title: 'Title',
  })), /severity/);
  assert.throws(() => parseNotificationLine(line({
    body: 'Body',
    events: ['not-an-object'],
    title: 'Title',
  })), /events item/);
  assert.throws(() => parseNotificationLine(line({
    body: 'Body',
    title: 'Title\nInjected',
  })), /control/);
  assert.throws(() => parseNotificationLine(line({
    body: 'Body',
    events: [{ prompt: 'safe\u202edanger' }],
    title: 'Title',
  })), /control/);
  assert.throws(() => parseNotificationLine(line({
    body: 'Body',
    events: [{ prompt: 'safe\u0085danger' }],
    title: 'Title',
  })), /control/);
});
