const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');

function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Missing ${name}`);
  const end = source.indexOf('\n}', start) + 2;
  const prefix = source.slice(Math.max(0, start - 6), start) === 'async ' ? 'async ' : '';
  return prefix + source.slice(start, end);
}

function setup() {
  const elements = new Map();
  const timers = new Map();
  let timerId = 0;
  const context = {
    activeView: 'deploy', eventsRefreshTimer: null, csrfToken: '', lastConfig: null,
    $: id => {
      if (!elements.has(id)) elements.set(id, {textContent: '', classList: {toggle() {}}});
      return elements.get(id);
    },
    window: {setTimeout: callback => {timers.set(++timerId, callback); return timerId;}, clearTimeout: id => timers.delete(id)},
    clearRefresh() {}, clearServerRefresh() {}, refresh() {}, refreshServerStatus() {}, refreshCertificates() {},
    renderAlerts() {}, renderEvents() {}, renderNotificationConfig() {},
    fetchJson: async () => ({csrf_token: 'new-csrf', notifications: {}}),
  };
  vm.createContext(context);
  for (const name of ['clearEventsRefresh', 'refreshEventsStatus', 'refreshNotificationConfig', 'setView']) {
    vm.runInContext(functionSource(name), context);
  }
  return {context, timers, elements};
}

test('every tab switches without an undefined refresh function', async () => {
  const {context} = setup();
  for (const view of ['server', 'events', 'notify', 'certificates', 'deploy']) {
    context.setView(view);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(context.activeView, view);
  }
});

test('events poll while active and stop when leaving', async () => {
  const {context, timers} = setup();
  context.setView('events');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 1);
  assert.equal(context.csrfToken, 'new-csrf');
  context.setView('notify');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 0);
});

test('late event response does not resume polling after navigation', async () => {
  const {context, timers} = setup();
  let finish;
  context.fetchJson = () => new Promise(resolve => {finish = resolve;});
  context.setView('events');
  context.setView('deploy');
  finish({csrf_token: 'token'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 0);
});

test('events fetch failure is visible and retries', async () => {
  const {context, timers, elements} = setup();
  context.fetchJson = async () => {throw new Error('offline');};
  context.setView('events');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.get('eventTimelineHint').textContent, /offline/);
  assert.equal(timers.size, 1);
});

test('webhook URLs retain fixed port for direct IP access and optional proxy prefix', () => {
  for (const [origin, pathname, expected] of [
    ['http://203.0.113.10:6868', '/', 'http://203.0.113.10:6868/webhook?project=api'],
    ['http://203.0.113.10:6868', '/ui', 'http://203.0.113.10:6868/webhook?project=api'],
    ['https://deploy.example.test', '/deploy/ui', 'https://deploy.example.test/deploy/webhook?project=api'],
  ]) {
    const context = vm.createContext({URL, location: {origin, pathname}});
    vm.runInContext(functionSource('webhookBaseUrl') + '\n' + functionSource('projectWebhookUrl'), context);
    assert.equal(context.projectWebhookUrl('api'), expected);
  }
});
