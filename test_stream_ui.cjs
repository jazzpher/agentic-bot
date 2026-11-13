// Streaming reply layout: one bubble per turn, text after a tool card in its own bubble,
// and no empty bubble when the model sends no reply text.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const { TextEncoder, TextDecoder } = require('util');

function makeNode() {
  return {
    children: [], textContent: '', className: '', value: '', disabled: false, style: {},
    scrollTop: 0, scrollHeight: 0, checked: false,
    classList: { add() {}, remove() {} },
    appendChild(c) { if (c.parentNode) c.remove(); this.children.push(c); c.parentNode = this; return c; },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this); },
    addEventListener(n, f) { this['on' + n] = f; },
    focus() {}, querySelector() { return null; },
  };
}

function build() {
  const notes = [], nodes = {};
  const get = id => (nodes[id] || (nodes[id] = makeNode()));
  const ctx = {
    JSON, Promise, Map, Set, Math, Date, String, Number, Object, Array, Error, TextEncoder, TextDecoder,
    setTimeout: () => 0, clearTimeout: () => {},
    console: { log() {}, error() {}, warn() {} },
    document: { getElementById: get, createElement: () => makeNode() },
    window: { Node: function () {}, history: { replaceState() {} }, addEventListener() {} },
  };
  vm.createContext(ctx);
  const src = fs.readFileSync('agenticbot/static/app.js', 'utf8')
    .replace('restoreTasks();', '').replace('restoreHistory();', '')
    .replace('loadPolicy();', '').replace('refreshKeyStatus();', '');
  vm.runInContext(src, ctx);
  for (const name of ['setStatus', 'setBusy', 'renderHistory', 'loadHistory', 'loadSessions',
    'refreshSessions', 'setView', 'renderMessages', 'scrollBottom', 'loadModels', 'loadSettings',
    'loadTools', 'restoreState', 'saveState', 'renderEmpty', 'showToast', 'updateBadge', 'loadUsage',
    'setRunning', 'subagentCard']) {
    if (typeof ctx[name] === 'function') ctx[name] = function () {};
  }
  ctx.note = (text, kind) => notes.push({ text, kind });
  ctx.toolCard = ev => { const d = makeNode(); d.className = 'toolcard'; d.textContent = 'tool:' + ev.name; get('chat').appendChild(d); };
  return { ctx, notes, nodes, chat: get('chat') };
}

function streamOf(events) {
  let i = 0;
  return {
    ok: true,
    body: { getReader() { return { read: async () => (i < events.length ? { done: false, value: new TextEncoder().encode(JSON.stringify(events[i++]) + '\n') } : { done: true }) }; } },
  };
}

async function turn(events) {
  const env = build();
  env.ctx.fetch = async () => streamOf(events);
  env.nodes.in.value = 'hi';
  await env.ctx.send();
  return env;
}

(async () => {
  // 1. one bubble for the whole reply, even with the live status line after it
  let env = await turn([
    { type: 'conversation', id: 'c1', title: 't' },
    { type: 'context_info', memories: [], skills: [], tools: 29 },
    { type: 'text', delta: 'H' },
    { type: 'text', delta: 'ello' },
    { type: 'text', delta: ' there' },
    { type: 'usage', prompt_tokens: 12, completion_tokens: 8 },
    { type: 'done', status: 'completed', steps: 1, tool_calls: 0, usage: {} },
  ]);
  let bubbles = env.chat.children.filter(c => String(c.className).includes('agent-msg'));
  assert.strictEqual(bubbles.length, 1, 'reply must be one bubble, got ' + bubbles.length);
  assert.strictEqual(bubbles[0].textContent, 'Hello there');

  // 2. text after a tool card starts a new bubble, after the card
  env = await turn([
    { type: 'conversation', id: 'c2', title: 't' },
    { type: 'text', delta: 'it.' },
    { type: 'tool_start', name: 'exec', arguments: { command: 'ls' }, id: 't1' },
    { type: 'tool_end', id: 't1', ok: true, output: 'x', duration: 0.1 },
    { type: 'text', delta: 'The file is there.' },
    { type: 'done', status: 'completed', steps: 1, tool_calls: 1, usage: {} },
  ]);
  const kids = env.chat.children;
  const first = kids.findIndex(c => c.textContent === 'it.');
  const card = kids.findIndex(c => String(c.className).includes('toolcard'));
  const second = kids.findIndex(c => c.textContent === 'The file is there.');
  assert.ok(first >= 0 && card > first && second > card, 'order must be text, card, text: ' + kids.map(c => c.textContent).join('|'));
  assert.ok(!kids.some(c => c.textContent === 'it.The file is there.'), 'text must not be glued across a tool card');

  // 3. no reply text: no empty bubble, and the user is told
  env = await turn([
    { type: 'conversation', id: 'c3', title: 't' },
    { type: 'tool_start', name: 'exec', args: { command: 'ls' }, id: 't2' },
    { type: 'tool_end', id: 't2', ok: true, output: 'x', duration: 0.1 },
    { type: 'done', status: 'completed', steps: 1, tool_calls: 1, output: '' },
  ]);
  const empties = env.chat.children.filter(c => String(c.className).includes('agent-msg') && !c.textContent);
  assert.strictEqual(empties.length, 0, 'no empty reply bubble');
  assert.ok(env.notes.some(n => /without reply text/.test(n.text)), 'a note says the turn had no reply text');

  console.log('test_stream_ui: ok');
})().catch(e => { console.error(e); process.exit(1); });
