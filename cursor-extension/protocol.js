'use strict';

const { TextDecoder } = require('node:util');

const MAX_PAYLOAD_BYTES = 256 * 1024;
const MAX_TITLE_CHARACTERS = 256;
const MAX_BODY_CHARACTERS = 16 * 1024;
const MAX_ID_CHARACTERS = 128;
const MAX_EVENT_FIELD_CHARACTERS = 256;
const MAX_PROMPT_CHARACTERS = 512;
const ALLOWED_SEVERITIES = new Set(['info', 'warning', 'error']);
const WARNING_STATES = new Set(['needs_input', 'exited']);
const COMPACT_STATE_LABELS = new Map([
  ['error', 'Failed'],
  ['exited', 'Process exited'],
  ['needs_input', 'Needs your response'],
  ['ready', 'Turn finished'],
]);
const utf8Decoder = new TextDecoder('utf-8', { fatal: true });

class ProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProtocolError';
  }
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validateDisplayString(value, field, maxCharacters, allowNewlines) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new ProtocolError(`${field} must be a non-empty string`);
  }
  if (Array.from(value).length > maxCharacters) {
    throw new ProtocolError(`${field} is too long`);
  }

  const forbiddenControls = allowNewlines
    ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u
    : /[\u0000-\u001f\u007f]/u;
  if (forbiddenControls.test(value) || /[\u0080-\u009f]|\p{Cf}/u.test(value)) {
    throw new ProtocolError(`${field} contains unsupported control characters`);
  }
}

function optionalDisplayString(value, field, maxCharacters = MAX_EVENT_FIELD_CHARACTERS) {
  if (value === undefined || value === '') {
    return undefined;
  }
  validateDisplayString(value, field, maxCharacters, false);
  return value;
}

function validateEvents(events) {
  if (events === undefined) {
    return [];
  }
  if (!Array.isArray(events)) {
    throw new ProtocolError('events must be an array');
  }
  return events.map((event) => {
    if (!isObject(event)) {
      throw new ProtocolError('each events item must be an object');
    }
    if (event.state !== undefined && typeof event.state !== 'string') {
      throw new ProtocolError('events state must be a string');
    }
    return {
      project: optionalDisplayString(event.project, 'events project'),
      prompt: optionalDisplayString(
        event.prompt,
        'events prompt',
        MAX_PROMPT_CHARACTERS,
      ),
      provider: optionalDisplayString(event.provider, 'events provider'),
      state: optionalDisplayString(event.state, 'events state'),
      stateLabel: optionalDisplayString(event.state_label, 'events state_label'),
      tmuxTarget: optionalDisplayString(event.tmux_target, 'events tmux_target'),
    };
  });
}

function inferSeverity(payload) {
  if (payload.severity !== undefined) {
    if (typeof payload.severity !== 'string' || !ALLOWED_SEVERITIES.has(payload.severity)) {
      throw new ProtocolError('severity must be info, warning, or error');
    }
    return payload.severity;
  }

  const states = Array.isArray(payload.events)
    ? payload.events.map((event) => event.state).filter((state) => typeof state === 'string')
    : [];
  if (states.includes('error')) {
    return 'error';
  }
  if (states.some((state) => WARNING_STATES.has(state))) {
    return 'warning';
  }
  return 'info';
}

function parseNotificationLine(line, maxBytes = MAX_PAYLOAD_BYTES) {
  if (!Buffer.isBuffer(line)) {
    throw new TypeError('line must be a Buffer');
  }
  if (line.length === 0) {
    throw new ProtocolError('notification line must not be empty');
  }
  if (line.length > maxBytes) {
    throw new ProtocolError(`notification exceeds ${maxBytes} bytes`);
  }

  let text;
  try {
    text = utf8Decoder.decode(line);
  } catch {
    throw new ProtocolError('notification must be valid UTF-8');
  }

  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new ProtocolError('notification must be valid JSON');
  }
  if (!isObject(payload)) {
    throw new ProtocolError('notification must be a JSON object');
  }

  validateDisplayString(payload.title, 'title', MAX_TITLE_CHARACTERS, false);
  validateDisplayString(payload.body, 'body', MAX_BODY_CHARACTERS, true);
  const events = validateEvents(payload.events);

  if (payload.id !== undefined) {
    if (
      typeof payload.id !== 'string'
      || payload.id.length === 0
      || payload.id.length > MAX_ID_CHARACTERS
      || /[\u0000-\u001f\u007f]/u.test(payload.id)
    ) {
      throw new ProtocolError('id must be a short, non-empty string');
    }
  }

  return {
    body: payload.body,
    events,
    id: payload.id,
    severity: inferSeverity(payload),
    title: payload.title,
  };
}

function providerLabel(provider) {
  if (provider === 'codex') {
    return 'Codex';
  }
  if (provider === 'claude') {
    return 'Claude';
  }
  return provider || 'Agent';
}

function eventLocation(event) {
  if (event.tmuxTarget) {
    return `tmux ${event.tmuxTarget}`;
  }
  return event.project || '';
}

function compactText(value, limit) {
  const characters = Array.from(value || '');
  if (characters.length <= limit) {
    return value || '';
  }
  return `${characters.slice(0, Math.max(1, limit - 1)).join('').trimEnd()}…`;
}

function formatToast(notification) {
  if (notification.events.length === 1) {
    const event = notification.events[0];
    const location = eventLocation(event);
    const compactState = COMPACT_STATE_LABELS.get(event.state);
    const headline = compactState
      ? `${providerLabel(event.provider)} · ${compactState}`
      : notification.title;
    const lines = [headline];
    if (location) {
      lines.push(location);
    }
    if (event.prompt) {
      lines.push(`Prompt: ${compactText(event.prompt, 180)}`);
    }
    return lines.join('\n');
  }

  if (notification.events.length > 1) {
    const summaries = notification.events.slice(0, 3).map((event) => {
      const location = eventLocation(event);
      const state = COMPACT_STATE_LABELS.get(event.state) || event.stateLabel || event.state;
      const prompt = event.prompt ? `Prompt: ${compactText(event.prompt, 72)}` : '';
      return [providerLabel(event.provider), state, location, prompt]
        .filter(Boolean)
        .join(' · ');
    });
    const remaining = notification.events.length - summaries.length;
    if (remaining > 0) {
      summaries.push(`+${remaining} more`);
    }
    return `${notification.title}\n${summaries.join('  |  ')}`;
  }

  const usefulBodyLine = notification.body
    .split(/\r?\n/u)
    .find((line) => line && !line.startsWith('Host:'));
  return usefulBodyLine
    ? `${notification.title}\n${usefulBodyLine}`
    : notification.title;
}

function encodeAck(ok, error, id) {
  const response = { ok };
  if (typeof id === 'string') {
    response.id = id;
  }
  if (!ok) {
    response.error = String(error || 'notification rejected').slice(0, 512);
  }
  return `${JSON.stringify(response)}\n`;
}

module.exports = {
  MAX_PAYLOAD_BYTES,
  ProtocolError,
  encodeAck,
  formatToast,
  inferSeverity,
  parseNotificationLine,
};
