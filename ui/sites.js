let registeredSites = [];
let siteOriginalKey = '';
let siteBusy = false;
let siteRequest = 0;

function siteMessage(message, failed = false) {
  $('siteFeedback').textContent = message;
  $('siteFeedback').className = failed ? 'certificate-feedback failed' : 'certificate-feedback';
}

function siteControls() {
  document.querySelectorAll('.site-registry input, .site-registry button').forEach(control => {
    control.disabled = siteBusy;
  });
}

function renderSites() {
  $('siteRegistryList').innerHTML = registeredSites.length ? registeredSites.map(site => `
    <div class="gateway-site-row site-registry-row">
      <div><strong>${escapeHtml(site.name || site.key)}</strong><span>${escapeHtml(site.app_domain || '未设置域名')}</span></div>
      <code>${escapeHtml(site.key)} : ${escapeHtml(site.service_port)}</code>
      <div class="certificate-actions">
        <button class="ghost-button compact-action" type="button" data-site-edit="${escapeHtml(site.key)}">编辑</button>
        <button class="ghost-button compact-action" type="button" data-site-delete="${escapeHtml(site.key)}">删除登记</button>
      </div>
    </div>`).join('') : '<div class="gateway-empty">暂无站点</div>';
  siteControls();
}

async function refreshSites() {
  const request = ++siteRequest;
  try {
    const result = await fetchJson('sites');
    if (request !== siteRequest) return;
    registeredSites = result.sites || [];
    renderSites();
  } catch (error) {
    if (request === siteRequest) siteMessage(`站点加载失败：${error.message}`, true);
  }
}

function editSite(site = null) {
  if (siteBusy) return;
  siteOriginalKey = site?.key || '';
  $('siteFormTitle').textContent = site ? '编辑站点' : '添加站点';
  $('siteKey').value = site?.key || '';
  $('siteKey').readOnly = Boolean(site);
  $('siteName').value = site?.name || '';
  $('siteDomain').value = site?.app_domain || '';
  $('sitePort').value = site?.service_port || 8000;
  $('siteHealth').value = site?.health_url || '';
  $('siteForm').hidden = false;
  $('siteAdvanced').open = false;
  siteMessage('');
  $('siteName').focus();
}

async function changeSite(action, payload) {
  if (siteBusy || nginxBusy || certificateBusy) return;
  siteBusy = true;
  siteRequest++;
  siteControls();
  try {
    const status = await fetchJson('status');
    csrfToken = status.csrf_token || csrfToken;
    await postJsonBody(`sites/${action}`, payload);
    $('siteForm').hidden = true;
    siteOriginalKey = '';
    siteMessage(action === 'save' ? '站点已保存。' : '站点登记已删除。');
    certificateLoaded = false;
    await refreshCertificates();
  } catch (error) {
    siteMessage(error.message, true);
  } finally {
    siteBusy = false;
    siteControls();
  }
}

$('siteAdd').addEventListener('click', () => editSite());
$('siteCancel').addEventListener('click', () => { $('siteForm').hidden = true; });
$('siteForm').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('siteForm').reportValidity()) return;
  const site = {key: $('siteKey').value.trim(), name: $('siteName').value.trim(),
    app_domain: $('siteDomain').value.trim(), service_port: Number($('sitePort').value),
    health_url: $('siteHealth').value.trim()};
  const existing = registeredSites.find(item => item.key === siteOriginalKey);
  if (existing) site.enabled = existing.enabled !== false;
  changeSite('save', {site, ...(siteOriginalKey ? {original_key: siteOriginalKey} : {})});
});
$('siteRegistryList').addEventListener('click', async event => {
  if (siteBusy) return;
  const edit = event.target.closest('[data-site-edit]');
  if (edit) {
    const site = registeredSites.find(item => item.key === edit.dataset.siteEdit);
    if (site) editSite(site);
    return;
  }
  const button = event.target.closest('[data-site-delete]');
  if (!button) return;
  const key = button.dataset.siteDelete;
  if (!await showConfirmDialog({title: '删除站点登记',
    message: `确认删除站点 ${key} 的登记？业务服务和数据保留。存在托管 Nginx 配置或证书时无法删除。`,
    confirmText: '删除登记'})) return;
  await changeSite('delete', {key});
});
