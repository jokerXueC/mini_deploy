const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/nginx.js', 'utf8');

function setup() {
  const nodes = new Map();
  const context = {
    nginxQuickVersion: 0, nginxQuickPlan: {token: 'old'}, nginxBusy: false, certificateBusy: false,
    nginxSettings: {configured: false, profile: {mode: 'local'}, projects: [{key:'api', port:8001, domain:''}], upstreams:{}},
    $: id => {
      if (!nodes.has(id)) nodes.set(id, {value:'', hidden:false, disabled:false, textContent:'',
        classList: {toggle() {}}, setAttribute() {}});
      return nodes.get(id);
    },
  };
  vm.createContext(context);
  for (const name of ['gatewayTab', 'invalidateNginxPlan', 'nginxQuickFields', 'nginxQuickPayload']) {
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    vm.runInContext(source.slice(start, end), context);
  }
  context.$('nginxQuickProject').value = 'api';
  return context;
}

test('beginner setup defaults to local host and hides advanced address', () => {
  const ctx = setup();
  ctx.nginxQuickFields();
  assert.equal(ctx.$('nginxQuickHost').value, '127.0.0.1');
  assert.equal(ctx.$('nginxQuickHostField').hidden, true);
  assert.equal(ctx.$('nginxQuickPort').value, 8001);
  assert.equal(ctx.nginxQuickPlan, null);
  assert.match(ctx.$('nginxQuickHint').textContent, /确认后安装/);
});

test('Docker bridge requires an explicit backend instead of guessing loopback', () => {
  const ctx = setup();
  ctx.nginxSettings.configured = true;
  ctx.nginxSettings.profile = {mode:'docker',network:'bridge'};
  ctx.nginxQuickFields();
  assert.equal(ctx.$('nginxQuickHostField').hidden, false);
  assert.equal(ctx.$('nginxQuickHost').required, true);
  assert.equal(ctx.$('nginxQuickHost').value, '');
  ctx.nginxSettings.upstreams.api = 'backend';
  ctx.nginxQuickFields();
  assert.equal(ctx.$('nginxQuickHost').value, 'backend');
});

test('custom local backend remains visible and empty projects cannot configure', () => {
  const ctx = setup();
  ctx.nginxSettings.upstreams.api = '10.0.0.5';
  ctx.nginxQuickFields();
  assert.equal(ctx.$('nginxQuickHostField').hidden, false);
  ctx.nginxSettings.projects = [];
  ctx.nginxQuickFields();
  assert.equal(ctx.$('nginxQuickCheck').disabled, true);
});

test('changing input invalidates the reviewed plan', () => {
  const ctx = setup();
  ctx.invalidateNginxPlan();
  assert.equal(ctx.nginxQuickPlan, null);
  assert.equal(ctx.$('nginxQuickPlan').hidden, true);
  assert.equal(ctx.nginxQuickVersion, 1);
});

test('gateway tabs expose only the chosen workspace', () => {
  const ctx = setup();
  ctx.gatewayTab('Certificates');
  assert.equal(ctx.$('gatewaySites').hidden, true);
  assert.equal(ctx.$('gatewayCertificates').hidden, false);
  ctx.gatewayTab('Sites');
  assert.equal(ctx.$('gatewaySites').hidden, false);
  assert.equal(ctx.$('gatewayCertificates').hidden, true);
});
