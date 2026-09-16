let nginxSettings = null;
let nginxDetected = null;
let nginxBusy = false;
let nginxQuickPlan = null;
let nginxQuickVersion = 0;

function nginxInstallFields() {
  const docker = $('nginxInstallMode').value === 'docker';
  $('nginxInstallPort').readOnly = !docker;
  if (!docker) $('nginxInstallPort').value = 80;
  $('nginxInstallContainerField').hidden = !docker;
  $('nginxInstallContainer').required = docker;
  $('nginxInstallDockerOptions').hidden = !docker;
  $('nginxInstallHint').textContent = docker
    ? '需要已安装并启动 Docker。创建独立 Nginx 容器，先通过服务器 IP 和 HTTP 端口访问。'
    : '使用系统包管理器安装，保留已有站点；本机安装默认使用 80 端口。';
}

function toggleNginxInstall(show) {
  $('nginxInstallForm').hidden = !show;
  $('nginxInstall').setAttribute('aria-expanded', String(show));
  if (show) nginxInstallFields();
}

function nginxInstallPayload() {
  const mode = $('nginxInstallMode').value;
  return {mode, port: Number($('nginxInstallPort').value), container: $('nginxInstallContainer').value.trim(),
    reserve_https: mode === 'docker' && $('nginxInstallHttps').checked};
}

function gatewayTab(name) {
  for (const value of ['Sites', 'Certificates']) {
    const active = value === name;
    $(`gateway${value}`).hidden = !active;
    $(`gateway${value}Tab`).classList.toggle('active', active);
    $(`gateway${value}Tab`).setAttribute('aria-selected', String(active));
    $(`gateway${value}Tab`).tabIndex = active ? 0 : -1;
  }
}

function invalidateNginxPlan() {
  nginxQuickVersion++;
  nginxQuickPlan = null;
  $('nginxQuickPlan').hidden = true;
}

function nginxQuickFields() {
  invalidateNginxPlan();
  const project = nginxSettings?.projects?.find(item => item.key === $('nginxQuickProject').value);
  $('nginxQuickForm').hidden = !project;
  const bridge = nginxSettings?.configured && nginxSettings.profile.mode === 'docker' && nginxSettings.profile.network !== 'host';
  $('nginxQuickDomain').value = project?.domain || '';
  $('nginxQuickPort').value = project?.port || 8000;
  const host = nginxSettings?.upstreams?.[project?.key] || (bridge ? '' : '127.0.0.1');
  $('nginxQuickHost').value = host;
  $('nginxQuickHostField').hidden = !bridge && host === '127.0.0.1';
  $('nginxQuickHost').required = Boolean(bridge);
  $('nginxQuickCheck').disabled = !project || nginxBusy || certificateBusy;
  $('nginxQuickHint').textContent = bridge
    ? '当前使用 Docker Nginx。业务地址需为它可访问的容器服务名或主机地址。'
    : nginxSettings?.configured && nginxSettings.profile.mode !== 'none'
      ? '使用已接入的 Nginx。业务需要先启动并监听此端口。'
      : '默认配置本机 Nginx；如未安装，将在确认后安装。业务需要先启动。';
}

function renderNginxSites() {
  const projects = (nginxSettings?.projects || []).filter(project => project.site_configured);
  $('nginxSiteList').innerHTML = projects.length ? projects.map(project => `
    <div class="gateway-site-row">
      <div><strong>${escapeHtml(project.name)}</strong><span>${escapeHtml(project.domain || '尚未设置域名')}</span></div>
      <code>${escapeHtml(nginxSettings.upstreams?.[project.key] || '127.0.0.1')}:${escapeHtml(project.port)}</code>
      <span class="badge success">已配置入口</span>
    </div>`).join('') : '<div class="gateway-empty">暂无已配置的访问入口</div>';
}

function nginxQuickPayload() {
  return {project: $('nginxQuickProject').value, domain: $('nginxQuickDomain').value.trim(),
    port: Number($('nginxQuickPort').value), host: $('nginxQuickHost').value.trim()};
}

async function nginxQuickOperation(apply = false) {
  if (nginxBusy || certificateBusy || !$('nginxQuickForm').reportValidity()) return;
  const payload = nginxQuickPayload();
  const signature = JSON.stringify(payload);
  if (apply && (!nginxQuickPlan || nginxQuickPlan.signature !== signature)) {
    invalidateNginxPlan();
    nginxMessage('配置已变化，请重新检查并预览。', true);
    return;
  }
  const version = nginxQuickVersion;
  nginxBusy = true;
  const controls = Array.from(document.querySelectorAll('#certificatesView input, #certificatesView select, #certificatesView button'));
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  $('nginxQuickResult').textContent = '';
  nginxMessage(apply ? '正在配置，请稍候。首次安装可能需要几分钟…' : '正在检查运行环境和业务端口…');
  try {
    const result = await postJsonBody('nginx-settings', {...payload,
      action: apply ? 'apply-site' : 'plan-site', token: apply ? nginxQuickPlan.token : ''});
    if (!apply) {
      if (version !== nginxQuickVersion || signature !== JSON.stringify(nginxQuickPayload())) return;
      nginxQuickPlan = {...result.plan, signature};
      $('nginxQuickSteps').innerHTML = result.plan.steps.map(step => `<li>${escapeHtml(step)}</li>`).join('');
      $('nginxQuickNotice').textContent = result.plan.notice;
      $('nginxQuickConfig').textContent = result.plan.config;
      $('nginxQuickPlan').hidden = false;
      nginxMessage('业务端口检查通过，请确认以下操作。');
    } else {
      invalidateNginxPlan();
      renderNginxSettings(result.settings);
      const link = document.createElement('a');
      link.href = result.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = result.url;
      $('nginxQuickResult').replaceChildren(document.createTextNode('访问入口已配置：'), link);
      nginxMessage(result.notice);
      certificateLoaded = false;
    }
  } catch (error) {
    invalidateNginxPlan();
    nginxMessage(`${error.message}${apply ? '。如已完成 Nginx 安装，软件将保留；请处理提示的问题后重试。' : ''}`, true);
  } finally {
    nginxBusy = false;
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    certificateControls();
  }
}

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
    const quickPrevious = $('nginxQuickProject').value;
    $('nginxQuickProject').innerHTML = data.projects?.length
      ? data.projects.map(project => `<option value="${escapeHtml(project.key)}">${escapeHtml(project.name)}</option>`).join('')
      : '<option value="">请先添加项目</option>';
    if (data.projects?.some(project => project.key === quickPrevious)) $('nginxQuickProject').value = quickPrevious;
    nginxQuickFields();
  }
  renderNginxSites();
  nginxFields();
  window.AppSelects?.syncAll();
}

async function refreshNginxSettings(discover = false) {
  const [data, status] = await Promise.all([fetchJson(`nginx-settings${discover ? '?discover=1' : ''}`), fetchJson('status')]);
  csrfToken = status.csrf_token || csrfToken;
  renderNginxSettings(data);
  if (discover) nginxMessage(data.detected.errors?.join('；') || (data.detected.local || data.detected.containers.length
    ? `发现 ${data.detected.local ? '本机 Nginx，' : ''}${data.detected.containers.length} 个运行中的 Nginx 容器，可在高级接入中确认使用。`
    : '未发现 Nginx，可选择本机或 Docker 安装，项目和域名稍后再配置。'));
  return data;
}

async function nginxOperation(action) {
  if (nginxBusy || certificateBusy) return;
  if (action === 'install-nginx' && !$('nginxInstallForm').reportValidity()) return;
  nginxBusy = true;
  const controls = Array.from(document.querySelectorAll('.nginx-setup input, .nginx-setup select, .nginx-setup button'));
  controls.forEach(control => { control.disabled = true; });
  try {
    invalidateNginxPlan();
    if (action === 'detect') {
      nginxMessage('正在检测本机与运行中的容器…');
      await refreshNginxSettings(true);
      return;
    }
    if (action === 'install-nginx') {
      const install = nginxInstallPayload();
      $('nginxInstallResult').replaceChildren();
      nginxMessage(`正在检查安装环境和 ${install.port} 端口…`);
      const {plan} = await postJsonBody('nginx-settings', {action: 'plan-install', ...install});
      if (!await showConfirmDialog({title: install.mode === 'docker' ? '安装 Docker Nginx' : '准备本机 Nginx',
        message: `${plan.steps.join('；')}。${plan.notice}`, confirmText: '确认执行'})) {
        nginxMessage('已取消安装。');
        return;
      }
      nginxMessage('正在准备 Nginx，首次下载和安装可能需要几分钟…');
      const result = await postJsonBody('nginx-settings', {action: 'install-nginx', ...install, token: plan.token});
      renderNginxSettings(result.settings);
      nginxMessage(result.message);
      const url = new URL(window.location.href);
      url.protocol = 'http:';
      url.port = String(result.access_port || install.port);
      url.pathname = '/';
      url.search = '';
      url.hash = '';
      const link = document.createElement('a');
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = url.href;
      const notice = document.createElement('p');
      notice.className = 'gateway-caption';
      notice.textContent = `本机 HTTP 检查通过。外部访问请在安全组放行 ${install.port} 端口；通过代理访问面板时，将链接中的主机改为服务器 IP。`;
      $('nginxInstallResult').replaceChildren(document.createTextNode('测试地址：'), link, notice);
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
  } catch (error) {
    nginxMessage(`${error.message}${action === 'install-nginx' ? '。如软件或镜像已下载，将保留；处理问题后可重试。' : ''}`, true);
  } finally {
    nginxBusy = false;
    controls.forEach(control => { control.disabled = false; });
  }
  await refreshCertificates();
}

async function ensureNginxConfigured() {
  try {
    const data = await fetchJson('nginx-settings');
    if (data.configured && data.profile.mode !== 'none') return true;
    closeProjectModal();
    setView('certificates');
    gatewayTab('Sites');
    nginxMessage('在访问入口中选择项目、填写域名和端口，即可检查并配置。');
  } catch (error) {
    setProjectFormError(`Nginx 接入检查失败：${error.message}`);
  }
  return false;
}

$('nginxMode').addEventListener('change', nginxFields);
$('nginxContainer').addEventListener('input', nginxFields);
$('nginxProject').addEventListener('change', nginxProjectFields);
$('nginxDetect').addEventListener('click', () => nginxOperation('detect'));
$('nginxInstall').addEventListener('click', () => toggleNginxInstall($('nginxInstallForm').hidden));
$('nginxInstallCancel').addEventListener('click', () => toggleNginxInstall(false));
$('nginxInstallMode').addEventListener('change', nginxInstallFields);
$('nginxInstallForm').addEventListener('submit', event => { event.preventDefault(); nginxOperation('install-nginx'); });
$('nginxProbe').addEventListener('click', () => nginxOperation('probe'));
$('nginxRemoveSite').addEventListener('click', () => nginxOperation('remove-site'));
$('nginxSettingsForm').addEventListener('submit', event => { event.preventDefault(); nginxOperation('save'); });
$('nginxUpstreamForm').addEventListener('submit', event => { event.preventDefault(); nginxOperation('save-upstream'); });
$('nginxQuickProject').addEventListener('change', nginxQuickFields);
$('nginxQuickForm').addEventListener('input', invalidateNginxPlan);
$('nginxQuickForm').addEventListener('submit', event => { event.preventDefault(); nginxQuickOperation(); });
$('nginxQuickApply').addEventListener('click', () => nginxQuickOperation(true));
for (const name of ['Sites', 'Certificates']) {
  $(`gateway${name}Tab`).addEventListener('click', () => {
    gatewayTab(name);
    if (name === 'Certificates') refreshCertificates();
  });
  $(`gateway${name}Tab`).addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'Sites' : event.key === 'End' ? 'Certificates' : name === 'Sites' ? 'Certificates' : 'Sites';
    $(`gateway${next}Tab`).click();
    $(`gateway${next}Tab`).focus();
  });
}
