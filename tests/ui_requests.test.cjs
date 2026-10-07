const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/nginx-requests.js', 'utf8');
const context = vm.createContext({});
for (const name of ['groupRequests', 'formatDuration']) {
  const start = source.indexOf(`  function ${name}(`);
  assert.ok(start >= 0);
  const end = source.indexOf('\n  }\n', start) + 5;
  vm.runInContext(source.slice(start, end), context);
}
const record = (overrides = {}) => ({host: 'site.test', method: 'GET', path: '/api',
  status: 200, at: '2026-10-07T12:00:00Z', duration_ms: 100, upstream_ms: 80, ...overrides});

test('same host, method and path aggregate valid timing samples and retain failures', () => {
  const input = [record({duration_ms: 120, upstream_ms: null}),
    record({status: 500, duration_ms: 300, upstream_ms: 0}),
    record({status: 302, duration_ms: null, upstream_ms: 50})];
  const [group] = context.groupRequests(input);
  assert.equal(group.count, 3);
  assert.equal(group.successes, 2);
  assert.equal(group.errors, 1);
  assert.equal(group.average, 210);
  assert.equal(group.upstreamAverage, 25);
  assert.equal(group.durationCount, 2);
  assert.equal(group.upstreamCount, 2);
  assert.ok(Math.abs(group.successRate - 200 / 3) < 1e-10);
  assert.equal(input.length, 3);
});

test('different methods, hosts and literal paths stay separate', () => {
  assert.equal(context.groupRequests([record(), record({method: 'POST'}),
    record({host: 'other.test'}), record({path: '/api/1'}), record({path: '/api/2'})]).length, 5);
});

test('missing and invalid timings are not zeroes, real zero is included', () => {
  const [missing] = context.groupRequests([record({duration_ms: -1, upstream_ms: NaN}),
    record({duration_ms: Infinity, upstream_ms: null})]);
  assert.equal(missing.average, null);
  assert.equal(missing.upstreamAverage, null);
  const [zero] = context.groupRequests([record({duration_ms: 0, upstream_ms: 0})]);
  assert.equal(zero.average, 0);
  assert.equal(zero.upstreamAverage, 0);
  assert.equal(context.groupRequests([]).length, 0);
});

test('requests are newest first across time zones without modifying the input', () => {
  const older = record({at: '2026-10-07T12:00:00+08:00'});
  const newer = record({at: '2026-10-07T05:00:00Z'});
  const input = [older, newer];
  assert.equal(context.groupRequests(input)[0].items[0], newer);
  assert.equal(input[0], older);
});

test('average durations use two decimals and seconds only above 1000 ms', () => {
  assert.equal(context.formatDuration(1.7000000000000002), '1.70 ms');
  assert.equal(context.formatDuration(1000), '1000.00 ms');
  assert.equal(context.formatDuration(6290), '6.29 s');
  assert.equal(context.formatDuration(null), '未记录');
});
