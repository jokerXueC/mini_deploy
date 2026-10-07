const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/nginx-requests.js', 'utf8');
const context = vm.createContext({});
for (const name of ['normalizeRequestPath', 'summarizeRequests', 'groupRequests', 'requestTree', 'formatDuration']) {
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

test('dynamic path segments merge conservatively without changing originals or methods', () => {
  const a = '/cloud/runtime/sessions/01a10599-bccc-77d3-81b9-1538746408ce/mode';
  const b = '/cloud/runtime/sessions/01a10599-bccc-77d3-81b9-1538746408cf/mode';
  const input = [record({path: a}), record({path: b}), record({path: b, method: 'POST'})];
  const groups = context.groupRequests(input, true);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].path, '/cloud/runtime/sessions/:id/mode');
  assert.equal(groups[0].count, 2);
  assert.equal(groups[0].items[0].path, a);
  assert.equal(input[1].path, b);
  assert.equal(context.groupRequests(input).length, 3);
  assert.equal(context.normalizeRequestPath('/v1/users/123/messages/456/'), '/v1/users/:number/messages/:number/');
  assert.equal(context.normalizeRequestPath('/v1/runtime/heartbeat'), '/v1/runtime/heartbeat');
  assert.equal(context.normalizeRequestPath('/files/01a10599-bccc-77d3-81b9-1538746408ce.json'), '/files/01a10599-bccc-77d3-81b9-1538746408ce.json');
  assert.equal(context.normalizeRequestPath('/a//b/%31%32%33'), '/a//b/%31%32%33');
});

test('common prefixes compress and parents use weighted samples rather than averages of averages', () => {
  const groups = context.groupRequests([record({path: '/cloud/runtime/heartbeat', duration_ms: 10}),
    record({path: '/cloud/runtime/heartbeat', duration_ms: 30}),
    record({path: '/cloud/runtime/commands/claim', duration_ms: 200, status: 500})], true);
  const [root] = context.requestTree(groups);
  assert.equal(root.path, '/cloud/runtime');
  assert.equal(root.count, 3);
  assert.equal(root.leaves, 2);
  assert.equal(root.average, 80);
  assert.equal(root.errors, 1);
  assert.equal(root.children.length, 2);
  assert.equal(root.children[0].path, '/cloud/runtime/commands/claim');
  assert.equal(root.children[1].count, 2);
});

test('tree keeps terminal prefixes, trailing slashes, hosts and methods distinct', () => {
  const groups = context.groupRequests(['/api', '/api/', '/api/users', '/api/users/1']
    .map(path => record({path})).concat([record({path: '/api', method: 'POST'}), record({path: '/api', host: 'other.test'})]), true);
  const tree = context.requestTree(groups);
  assert.equal(tree.length, 2);
  const leaves = nodes => nodes.flatMap(node => node.children ? leaves(node.children) : [node]);
  assert.equal(leaves(tree).length, groups.length);
  assert.equal(tree.reduce((n, node) => n + node.count, 0), 6);
  assert.deepEqual(leaves(tree).map(node => node.path).sort(), groups.map(group => group.path).sort());
});

test('path nesting is bounded and sibling keys remain stable as request order changes', () => {
  const deep = '/' + Array.from({length: 200}, (_, i) => `p${i}`).join('/');
  const input = [record({path: deep}), record({path: deep + '/leaf'})];
  const tree = context.requestTree(context.groupRequests(input, true));
  const depth = node => node.children ? 1 + Math.max(...node.children.map(depth)) : 1;
  assert.ok(depth(tree[0]) <= 17);
  const flatten = nodes => nodes.flatMap(node => [node.key, ...(node.children ? flatten(node.children) : [])]);
  assert.deepEqual(flatten(tree), flatten(context.requestTree(context.groupRequests(input.slice().reverse(), true))));
});
