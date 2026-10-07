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
    activeView: 'server', eventsRefreshTimer: null, csrfToken: '', lastConfig: null,
    $: id => {
      if (!elements.has(id)) elements.set(id, {textContent: '', classList: {toggle() {}}});
      return elements.get(id);
    },
    window: {setTimeout: callback => {timers.set(++timerId, callback); return timerId;}, clearTimeout: id => timers.delete(id)},
    clearServerRefresh() {}, refreshServerStatus() {}, refreshCertificates() {},
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
  for (const view of ['server', 'events', 'notify', 'certificates', 'requests']) {
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

test('removed deployment view and unknown routes fall back to server', () => {
  const {context} = setup();
  for (const view of ['deploy', 'unknown', '', null]) {
    context.setView(view);
    assert.equal(context.activeView, 'server');
  }
});

test('notification settings use an independent endpoint and refresh CSRF', async () => {
  const {context} = setup();
  const calls = [];
  context.fetchJson = async path => {
    calls.push(path);
    return path === 'status' ? {csrf_token: 'site-token'} : {notifications: {email: {enabled: true}}};
  };
  context.activeView = 'notify';
  await context.refreshNotificationConfig();
  assert.deepEqual(calls, ['notifications', 'status']);
  assert.equal(context.csrfToken, 'site-token');
  assert.equal(context.lastConfig.notifications.email.enabled, true);
});

test('server status accepts monitoring-only payloads and supplies CSRF to Docker actions', async () => {
  const {context} = setup();
  Object.assign(context, {serverRefreshInFlight: false, systemStatusRequestId: 0,
    document: {hidden: false}, renderSystemStatus() {}, updateLiveIndicator() {}, scheduleServerRefresh() {}});
  vm.runInContext(functionSource('refreshServerStatus'), context);
  context.fetchJson = async path => {
    assert.equal(path, 'status');
    return {agent: {}, system: {}, alerts: [], events: [], csrf_token: 'docker-token'};
  };
  await context.refreshServerStatus();
  assert.equal(context.csrfToken, 'docker-token');
  assert.equal(context.serverRefreshInFlight, false);
});
