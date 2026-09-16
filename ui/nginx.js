let nginxSettings = null;
let nginxDetected = null;
let nginxBusy = false;

function nginxMessage(message, error = false) {
  $('nginxFeedback').textContent = message;
  $('nginxFeedback').className = error ? 'certificate-feedback failed' : 'certificate-feedback';
}

function nginxFields() {
  const docker = $('nginxMode').value === 'docker';
  $('nginxContainerField').hidden = !docker;
  $('nginxMountHelp').hidden = !docker;
  const container = nginxDetected?.containers?.find(item => item.name === $('nginxContainer').value);
  const fields = docker ? [
    ['容器', container?.name || '未选择'], ['网络', container?.network || '-'],
    ['发布端口', JSON.stringify(container?.ports || {})],
    ...(container?.mounts || []).map(m => [m.Destination, `${m.Source} (${m.Type}, ${m.RW ? '读写' : '只读'})`]),
  ] : [['本机 Nginx', nginxDetected ? nginxDetected.local ? '已发现' : '未发现' : '尚未检测']];
  $('nginxDetectedDetails').innerHTML = fields.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
  $('nginxMountExample').textContent = `volumes:\n  - /srv/mini-deploy-nginx/conf.d:/etc/nginx/conf.d:ro\n  - ${nginxDetected?.certificate_mount || '/var/lib/mini-deploy-agent/certificates:/etc/mini-deploy/certificates:ro'}\nports:\n  - "80:80"\n  - "443:443"`;
}

function nginxProjectFields() {
  const project = nginxSettings?.projects?.find(item => item.key === $('nginxProject').value);
  const bridge = nginxSettings?.profile?.mode === 'docker' && nginxSettings.profile.network !== 'host';
  $('nginxUpstream').value = nginxSettings?.upstreams?.[project?.key] || (bridge ? '' : '127.0.0.1');
  $('nginxPort').textContent = project ? `业务端口：${project.port}` : '';
  $('nginxNetworkHint').textContent = bridge
    ? '桥接网络：填写与 Nginx 共享网络的业务服务名，或容器可达的宿主机地址。127.0.0.1 指向 Nginx 容器本身。'
    : '填写 Nginx 可访问的业务地址，端口来自项目设置。';
}

function renderNginxSettings(data, updateForm = true) {
  nginxSettings = data;
  if (data.detected) nginxDetected = data.detected;
  $('nginxCurrent').textContent = data.configured
    ? `当前：${data.profile.mode === 'docker' ? `Docker / ${data.profile.container}` : data.profile.mode === 'local' ? '服务器本机' : '暂不配置'}`
    : '尚未确认 Nginx 运行环境';
  if (updateForm) {
    $('nginxMode').value = data.configured ? data.profile.mode : 'none';
    const containers = nginxDetected?.containers || [];
    const saved = data.profile.container;
    const names = [...new Set([...containers.map(c => c.name), ...(saved ? [saved] : [])])];
    $('nginxContainerOptions').innerHTML = names.map(name => `<option value="${escapeHtml(name)}"></option>`).join('');
    $('nginxContainer').value = saved || '';
    const previous = $('nginxProject').value;
    $('nginxProject').innerHTML = (data.projects || []).map(project => `<option value="${escapeHtml(project.key)}">${escapeHtml(project.name)}</option>`).join('');
    if (data.projects?.some(p => p.key === previous)) $('nginxProject').value = previous;
    nginxProjectFields();
  }
  nginxFields();
}

async function refreshNginxSettings(discover = false) {
  const [data, status] = await Promise.all([fetchJson(`nginx-settings${discover ? '?discover=1' : ''}`), fetchJson('status')]);
  csrfToken = status.csrf_token || csrfToken;
  renderNginxSettings(data);
  if (discover) nginxMessage(data.detected.errors?.join('；') || `发现 ${data.detected.local ? '本机 Nginx，' : ''}${data.detected.containers.length} 个运行中的 Nginx 容器。请选择后保存。`);
  return data;
}

async function nginxOperation(action) {
  if (nginxBusy || certificateBusy) return;
  nginxBusy = true;
  const controls = Array.from(document.querySelectorAll('.nginx-setup input, .nginx-setup select, .nginx-setup button'));
  controls.forEach(control => { control.disabled = true; });
  try {
    if (action === 'detect') {
      nginxMessage('正在检测本机与运行中的容器…');
      await refreshNginxSettings(true);
      return;
    }
    if (action === 'remove-site' && !await showConfirmDialog({title: '移除域名入口', message: '该项目通过此 Nginx 的域名访问将停止。业务服务和代码不删除。', confirmText: '移除'})) return;
    const payload = {action, mode: $('nginxMode').value, container: $('nginxContainer').value,
      project: $('nginxProject').value, host: $('nginxUpstream').value.trim()};
    nginxMessage('正在检查…');
    const result = await postJsonBody('nginx-settings', payload);
    if (action === 'save') renderNginxSettings(result);
    if (result.settings) renderNginxSettings(result.settings);
    nginxMessage(action === 'probe' ? '后端连通检查通过。' : action === 'remove-site' ? '域名入口已移除。' : '已检查并保存。');
    await refreshCertificates();
  } catch (error) {
    nginxMessage(error.message, true);
  } finally {
    nginxBusy = false;
    controls.forEach(control => { control.disabled = false; });
  }
}

async function ensureNginxConfigured() {
  try {
    const data = await fetchJson('nginx-settings');
    if (data.configured && data.profile.mode !== 'none') return true;
    closeProjectModal();
    setView('certificates');
    nginxMessage('请先选择并保存 Nginx 运行环境，再配置业务域名。', true);
  } catch (error) {
    setProjectFormError(`Nginx 接入检查失败：${error.message}`);
  }
  return false;
}

$('nginxMode').addEventListener('change', nginxFields);
$('nginxContainer').addEventListener('input', nginxFields);
$('nginxProject').addEventListener('change', nginxProjectFields);
$('nginxDetect').addEventListener('click', () => nginxOperation('detect'));
$('nginxProbe').addEventListener('click', () => nginxOperation('probe'));
$('nginxRemoveSite').addEventListener('click', () => nginxOperation('remove-site'));
$('nginxSettingsForm').addEventListener('submit', event => { event.preventDefault(); nginxOperation('save'); });
$('nginxUpstreamForm').addEventListener('submit', event => { event.preventDefault(); nginxOperation('save-upstream'); });
