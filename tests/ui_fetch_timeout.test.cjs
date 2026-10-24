const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
const code = source.slice(source.indexOf('async function fetchJson('), source.indexOf('async function postJson('));
const postCode = source.slice(source.indexOf('async function postJsonBody('), source.indexOf('function severityClass('));

test('read timeout covers a stalled response body and the next request can succeed', async () => {
  let fire, cleared = 0, attempts = 0;
  const context = vm.createContext({AbortController, Error,
    setTimeout(callback) { fire = callback; return 1; },
    clearTimeout() { cleared++; },
    location: {reload() {}},
    async fetch(url, {signal}) {
      attempts++;
      return {ok:true, status:200, json: () => attempts > 1 ? Promise.resolve({ok:true}) : new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
      })};
    }});
  vm.runInContext(code, context);
  const pending = context.fetchJson('/status');
  await new Promise(resolve => setImmediate(resolve));
  fire();
  await assert.rejects(pending, /读取超时/);
  assert.equal(cleared, 1);
  assert.equal((await context.fetchJson('/status')).ok, true);
  assert.equal(attempts, 2);
  assert.equal(cleared, 2);
});

test('readonly inspection timeout covers the body while mutating operations keep their original lifetime', async () => {
  let fire, cleared = 0, attempts = 0, scheduled = 0;
  const requests = [];
  const context = vm.createContext({AbortController, Error, csrfToken: 'test-token',
    setTimeout(callback) { fire = callback; scheduled++; return 1; },
    clearTimeout() { cleared++; },
    location: {reload() {}},
    async fetch(url, options) {
      attempts++;
      requests.push(options);
      return {ok: true, status: 200, json: () => attempts > 1 ? Promise.resolve({ok: true}) : new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
      })};
    }});
  vm.runInContext(postCode, context);
  const pending = context.postJsonBody('/gateway-connections', {action: 'inspect'}, {timeoutMs: 20000});
  await new Promise(resolve => setImmediate(resolve));
  fire();
  await assert.rejects(pending, /读取配置超时/);
  assert.equal(cleared, 1);
  assert.equal((await context.postJsonBody('/gateway-connections', {action: 'apply'})).ok, true);
  assert.equal(requests[1].signal, undefined);
  assert.equal(scheduled, 1);
});
