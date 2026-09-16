const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ui/app.js', 'utf8');

function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1);
  const end = source.indexOf('\n}', start) + 2;
  return (source.slice(start - 6, start) === 'async ' ? 'async ' : '') + source.slice(start, end);
}

function setup() {
  const elements = new Map();
  const context = {
    projectGuidanceVersion: 0, projectPreviewSignature: '',
    form: {key: 'api', template: 'python', service_port: 8001},
    $: id => {
      if (!elements.has(id)) elements.set(id, {
        value: '', dataset: {}, checked: false, disabled: false, hidden: false, textContent: '', innerHTML: '',
        replaceChildren() {this.innerHTML = '';}, reportValidity: () => true,
      });
      return elements.get(id);
    },
    collectProjectForm: () => context.form,
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  };
  vm.createContext(context);
  for (const name of ['normalizedProjectKeyFromForm', 'applyTemplateDefaults', 'invalidateProjectPreview', 'previewCurrentProject', 'renderFailureAdvice']) {
    vm.runInContext(functionSource(name), context);
  }
  return context;
}

test('generated defaults follow the project key while user values are preserved', () => {
  const context = setup();
  context.$('projectTemplateInput').value = 'python';
  context.applyTemplateDefaults(true);
  assert.equal(context.$('projectWorkdirInput').value, '/srv/api');
  context.form.key = 'backend';
  context.applyTemplateDefaults(false);
  assert.equal(context.$('projectWorkdirInput').value, '/srv/backend');
  assert.equal(context.$('projectScriptInput').value, '/srv/backend/deploy/deploy.sh');
  context.$('projectWorkdirInput').value = '/srv/custom';
  context.$('projectServicePortInput').value = '9000';
  context.$('projectAppDomainInput').value = 'api.example.test';
  context.form.key = 'next';
  context.$('projectTemplateInput').value = 'java';
  context.applyTemplateDefaults(false);
  assert.equal(context.$('projectWorkdirInput').value, '/srv/custom');
  assert.equal(context.$('projectServicePortInput').value, '9000');
  assert.equal(context.$('projectAppDomainInput').value, 'api.example.test');
  assert.equal(context.$('projectHealthInput').value, '');
});

test('preview is read-only and confirmation requires the exact current form', async () => {
  const context = setup();
  context.postJsonBody = async path => {
    assert.equal(path, 'projects-config/preview');
    return {files: [{path: '/srv/api/deploy.sh', exists: true, content: '<script>unsafe</script>'}]};
  };
  await context.previewCurrentProject();
  assert.equal(context.projectPreviewSignature, JSON.stringify(context.form));
  assert.equal(context.$('projectConfigConfirmed').disabled, false);
  assert.equal(context.$('projectConfigConfirmed').checked, false);
  assert.match(context.$('projectFilePreview').innerHTML, /&lt;script&gt;/);
  context.invalidateProjectPreview();
  assert.equal(context.projectPreviewSignature, '');
  assert.equal(context.$('projectConfigConfirmed').disabled, true);
});

test('editing while preview loads rejects the stale result', async () => {
  const context = setup();
  let finish;
  context.postJsonBody = () => new Promise(resolve => {finish = resolve;});
  const pending = context.previewCurrentProject();
  context.form.service_port = 9001;
  finish({files: []});
  await pending;
  assert.equal(context.$('projectConfigConfirmed').disabled, true);
  assert.equal(context.projectPreviewSignature, '');
  assert.match(context.$('projectFilePreview').textContent, /重新预览/);
});

test('closed modal ignores a pending preview', async () => {
  const context = setup();
  let finish;
  context.postJsonBody = () => new Promise(resolve => {finish = resolve;});
  const pending = context.previewCurrentProject();
  context.projectGuidanceVersion++;
  context.$('projectModal').hidden = true;
  finish({files: []});
  await pending;
  assert.equal(context.projectPreviewSignature, '');
  assert.equal(context.$('projectConfigConfirmed').disabled, true);
});

test('diagnostic content is escaped', () => {
  const context = setup();
  const html = context.renderFailureAdvice([{title: '<img src=x>', advice: '<script>test</script>'}]);
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
});
