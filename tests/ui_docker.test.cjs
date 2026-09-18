const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup() {
  const elements = new Map(), posts = [];
  const ctx = {window: {}, document: {querySelectorAll: () => []},
    $: id => {
      if (!elements.has(id)) elements.set(id, {value: '', textContent: '', innerHTML: '', events: {},
        addEventListener(event, handler) {this.events[event] = handler;}, reportValidity: () => true});
      return elements.get(id);
    },
    escapeHtml: s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    fetchJson: async () => ({images: [{id: 'sha256:' + 'a'.repeat(64), tags: ['<script>'], size: '1MB', created: 'today'}]}),
    postJsonBody: async (path, data) => {posts.push({path, data});},
    showConfirmDialog: async () => false,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('ui/docker-images.js', 'utf8'), ctx);
  const clickRemove = () => ctx.$('dockerImageList').events.click({target: {closest: () => ({dataset: {imageRemove: 'sha256:' + 'a'.repeat(64)}})}});
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {ctx, posts, clickRemove, settle};
}

test('image content is escaped and search supports empty results', async () => {
  const {ctx} = setup();
  await ctx.window.DockerImages.refresh();
  assert.ok(ctx.$('dockerImageList').innerHTML.includes('&lt;script>'));
  assert.ok(!ctx.$('dockerImageList').innerHTML.includes('<script>'));
  ctx.$('dockerImageSearch').value = 'not-present';
  ctx.$('dockerImageSearch').events.input();
  assert.match(ctx.$('dockerImageList').innerHTML, /没有匹配/);
});

test('cancelled image deletion never posts; confirmed deletion uses immutable ID', async () => {
  const {ctx, posts, clickRemove, settle} = setup();
  await ctx.window.DockerImages.refresh();
  clickRemove();
  await settle();
  assert.equal(posts.length, 0);
  ctx.showConfirmDialog = async () => true;
  clickRemove();
  await settle();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].data.confirmed, true);
  assert.equal(posts[0].data.reference, 'sha256:' + 'a'.repeat(64));
});

test('pull blocks duplicate submissions and leaves failures visible', async () => {
  const {ctx, settle} = setup();
  let reject, count = 0;
  ctx.postJsonBody = () => {count++; return new Promise((_, fail) => {reject = fail;});};
  ctx.$('dockerImageReference').value = 'nginx:alpine';
  const submit = () => ctx.$('dockerImagePullForm').events.submit({preventDefault() {}});
  submit();
  submit();
  assert.equal(count, 1);
  assert.equal(ctx.$('dockerImagePull').disabled, true);
  reject(new Error('network unavailable'));
  await settle();
  assert.match(ctx.$('dockerImageFeedback').textContent, /network unavailable/);
  assert.equal(ctx.$('dockerImagePull').disabled, false);
});
