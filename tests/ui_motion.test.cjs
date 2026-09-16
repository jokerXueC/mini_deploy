const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function environment(reduced = false) {
  let now = 0, id = 0;
  const frames = new Map();
  const context = vm.createContext({
    performance: {now: () => now}, document: {hidden: false},
    window: {matchMedia: () => ({matches: reduced}),
      requestAnimationFrame: fn => { frames.set(++id, fn); return id; },
      cancelAnimationFrame: key => frames.delete(key)},
  });
  vm.runInContext(fs.readFileSync('ui/motion.js', 'utf8') + '\nglobalThis.motion = DashboardMotion;', context);
  return {motion: context.motion, frames, context, tick(time) {
    now = time;
    const callbacks = [...frames.values()];
    frames.clear();
    callbacks.forEach(fn => fn(now));
  }};
}

test('resource values ease over time, retarget from visible state and finish exactly', () => {
  const e = environment(), el = {isConnected: true};
  let shown = 0;
  e.motion.value(el, 100, n => {shown = n;}, {initial: 0, duration: 1000});
  e.tick(500);
  assert.equal(shown, 50);
  e.motion.value(el, 20, n => {shown = n;}, {duration: 1000});
  assert.equal(shown, 50);
  e.tick(1000);
  assert.equal(shown, 35);
  e.tick(1500);
  assert.equal(shown, 20);
  assert.equal(e.frames.size, 0);
});

test('same target does not restart transitions, reduced motion is immediate', () => {
  const e = environment(), el = {isConnected: true};
  let shown;
  e.motion.value(el, 80, n => {shown = n;}, {initial: 0, duration: 1000});
  e.tick(500);
  e.motion.value(el, 80, n => {shown = n;});
  e.tick(1000);
  assert.equal(shown, 80);
  const quiet = environment(true);
  quiet.motion.value(el, 30, n => {shown = n;}, {initial: 0});
  assert.equal(shown, 30);
  assert.equal(quiet.frames.size, 0);
});

test('hidden pages settle and detached resources stop scheduling frames', () => {
  const e = environment(), el = {isConnected: true};
  let shown;
  e.motion.value(el, 80, n => {shown = n;}, {initial: 0});
  e.context.document.hidden = true;
  e.tick(10);
  assert.equal(shown, 80);
  assert.equal(e.frames.size, 0);
  e.context.document.hidden = false;
  e.motion.value(el, 40, () => {});
  el.isConnected = false;
  e.tick(20);
  assert.equal(e.frames.size, 0);
});

test('curves pass through samples without overshooting neighbouring values', () => {
  const {motion} = environment();
  for (const samples of [[0, 100, 0, 100], [20, 20, 20], [1, 3, 90, 91], [100, 99, 2, 1]]) {
    const points = samples.map((y, i) => [i * 18, y]);
    const path = motion.curve(points);
    const commands = path.split(' C').slice(1).map(c => c.split(' ').map(Number));
    assert.equal(commands.length, samples.length - 1);
    commands.forEach((c, i) => {
      assert.equal(c[4], points[i + 1][0]);
      assert.equal(c[5], samples[i + 1]);
      for (let t = 0; t <= 1; t += .02) {
        const y = (1-t)**3 * samples[i] + 3*(1-t)**2*t*c[1] + 3*(1-t)*t*t*c[3] + t**3*c[5];
        assert.ok(y >= Math.min(samples[i], samples[i + 1]) - .001);
        assert.ok(y <= Math.max(samples[i], samples[i + 1]) + .001);
      }
    });
  }
  assert.equal(motion.curve([]), '');
  assert.equal(motion.curve([[4, 7]]), 'M4 7');
});

test('missing metric values split curves and are never rendered as zero', () => {
  const e = environment();
  const js = fs.readFileSync('ui/app.js', 'utf8');
  for (const name of ['trendPointValue', 'trendPointX', 'trendPath', 'trendLinePath']) {
    const start = js.indexOf(`function ${name}(`);
    const end = js.indexOf('\n}', start) + 2;
    vm.runInContext(js.slice(start, end), e.context);
  }
  assert.equal(e.context.trendPointValue({cpu: null}, 'cpu'), null);
  const paths = e.context.trendPath([{cpu: 10}, {cpu: 20}, {cpu: null}, {cpu: 50}], 'cpu', 100, 100,
    {top: 0, bottom: 0, left: 0, right: 0}, {min: 0, max: 100});
  assert.equal(paths.length, 2);
  assert.ok(paths[0].includes(' C'));
  assert.ok(!paths[1].includes(' C'));
});

function trendContext() {
  const e = environment();
  const js = fs.readFileSync('ui/app.js', 'utf8');
  Object.assign(e.context, {escapeHtml: String, formatPercent: n => `${n}%`});
  for (const name of ['trendPointValue', 'trendAvailability', 'trendScale', 'renderTrendCard']) {
    const start = js.indexOf(`function ${name}(`);
    const end = js.indexOf('\n}', start) + 2;
    vm.runInContext(js.slice(start, end), e.context);
  }
  return e.context;
}

test('a single metric sample renders an empty chart with a decreasing estimate', () => {
  const ctx = trendContext(), config = {key: 'cpu', label: 'CPU', className: 'cpu'};
  const points = [{ts: 10000, cpu: 2.3}];
  const timing = {lastSampleTs: 10000, nowSeconds: 10000, intervalSeconds: 1800};
  const waiting = ctx.trendAvailability(points, config, timing);
  assert.equal(waiting.ready, false);
  assert.match(waiting.message, /30 分钟/);
  const html = ctx.renderTrendCard(points, config, waiting);
  assert.ok(!html.includes('<svg'));
  assert.ok(!html.includes('trend-axis-label'));
  assert.match(html, /样本不足/);
  assert.match(html, /2.3%/);
  assert.match(ctx.trendAvailability(points, config, {...timing, nowSeconds: 10600}).message, /20 分钟/);
});

test('zero, missing, separated and ready samples are evaluated per metric', () => {
  const ctx = trendContext(), config = {key: 'cpu'};
  const timing = {lastSampleTs: 10000, nowSeconds: 10000, intervalSeconds: 1800};
  assert.match(ctx.trendAvailability([], config).message, /首次采样/);
  assert.match(ctx.trendAvailability([{cpu: null}], config, timing).message, /60 分钟/);
  assert.equal(ctx.trendAvailability([{cpu: 0}, {cpu: 0}], config, timing).ready, true);
  assert.equal(ctx.trendAvailability([{cpu: 2}, {cpu: null}, {cpu: 4}], config, timing).ready, false);
  const points = [{cpu: 1, network: null}, {cpu: 2, network: 10}];
  assert.equal(ctx.trendAvailability(points, config, timing).ready, true);
  assert.equal(ctx.trendAvailability(points, {key: 'network'}, timing).ready, false);
});

test('overdue samples do not promise an expired countdown, custom intervals are respected', () => {
  const ctx = trendContext(), config = {key: 'cpu'};
  const points = [{cpu: 4}];
  assert.match(ctx.trendAvailability(points, config, {
    lastSampleTs: 10000, nowSeconds: 12000, intervalSeconds: 1800,
  }).message, /等待采样更新/);
  assert.match(ctx.trendAvailability(points, config, {
    lastSampleTs: 10000, nowSeconds: 10000, intervalSeconds: 300,
  }).message, /5 分钟/);
  assert.match(ctx.trendAvailability(points, config, {
    lastSampleTs: 10000, nowSeconds: 10000, intervalSeconds: 1,
  }).message, /1 秒/);
});

test('realtime polling skips hidden pages and overlapping requests', async () => {
  const js = fs.readFileSync('ui/app.js', 'utf8');
  const start = js.indexOf('async function refreshRealtimeMetrics(');
  const end = js.indexOf('\n}', start) + 2;
  let finish, requests = 0, rendered;
  const context = vm.createContext({
    activeView: 'server', document: {hidden: false}, realtimeRefreshInFlight: false,
    lastSystemPayload: {history: ['old'], docker: {available: true}},
    fetchJson: path => {assert.equal(path, 'system-metrics'); requests++; return new Promise(resolve => {finish = resolve;});},
    renderSystemStatus: value => {rendered = value;},
  });
  vm.runInContext(js.slice(start, end), context);
  const pending = context.refreshRealtimeMetrics();
  await context.refreshRealtimeMetrics();
  assert.equal(requests, 1);
  finish({realtime_history: [{cpu_percent: 2}]});
  await pending;
  assert.equal(rendered.history[0], 'old');
  assert.equal(rendered.docker.available, true);
  assert.equal(rendered.realtime_history[0].cpu_percent, 2);
  context.document.hidden = true;
  await context.refreshRealtimeMetrics();
  assert.equal(requests, 1);
});

test('server refresh is serialized and ignores superseded status responses', async () => {
  const js = fs.readFileSync('ui/app.js', 'utf8');
  const start = js.indexOf('async function refreshServerStatus(');
  const end = js.indexOf('\n}', start) + 2;
  let finish, requests = 0, renders = 0, scheduled = 0;
  const context = vm.createContext({
    activeView: 'server', document: {hidden: false}, serverRefreshInFlight: false,
    systemStatusRequestId: 0, clearServerRefresh() {},
    fetchJson: () => { requests++; return new Promise(resolve => {finish = resolve;}); },
    renderSystemStatus() {renders++;}, renderAlerts() {}, renderEvents() {}, renderDeployLock() {},
    $: () => ({textContent: ''}), scheduleServerRefresh() {scheduled++;},
  });
  vm.runInContext(js.slice(start, end), context);
  const pending = context.refreshServerStatus();
  await context.refreshServerStatus();
  assert.equal(requests, 1);
  context.systemStatusRequestId++;
  finish({system: {}});
  await pending;
  assert.equal(renders, 0);
  assert.equal(scheduled, 1);
  assert.equal(context.serverRefreshInFlight, false);
  const next = context.refreshServerStatus();
  finish({system: {}});
  await next;
  assert.equal(renders, 1);
});
