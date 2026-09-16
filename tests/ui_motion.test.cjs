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
