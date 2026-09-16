let certificateProjects = [];
let certificateBusy = false;
let certificateLoaded = false;
let certificateRequest = 0;

function certificateSelection() {
  return certificateProjects.find(item => item.project === $('certificateProject').value);
}

function certificateMessage(text, failed = false) {
  const target = $('certificateFeedback');
  target.textContent = text;
  target.className = failed ? 'certificate-feedback failed' : 'certificate-feedback';
}

function certificateControls() {
  const item = certificateSelection();
  const cert = item?.certificate;
  ['certificateProject', 'certificateLabel', 'certificateFile', 'certificateKeyFile',
    'certificatePem', 'certificatePrivateKey'].forEach(id => { $(id).disabled = certificateBusy; });
  $('certificateUpload').disabled = certificateBusy || !item?.domain || Boolean(item?.error);
  $('certificateEnable').disabled = certificateBusy || !cert || Boolean(item?.error);
  $('certificateDisable').disabled = certificateBusy || !cert?.active;
  $('certificateDelete').disabled = certificateBusy || !cert || cert.active;
  $('certificateRename').disabled = certificateBusy || !cert;
  $('certificateUpload').textContent = cert?.active ? '替换并应用证书' : cert ? '替换证书' : '保存证书';
  window.AppSelects?.syncAll();
}

function renderCertificateDetails(resetForm = false) {
  const item = certificateSelection();
  const cert = item?.certificate;
  const fields = [
    ['项目域名', item?.domain || '未设置业务域名'],
    ['HTTPS 状态', cert?.active ? '已启用（上传证书）' : '未使用上传证书'],
    ['证书名称', cert?.label || '未上传'],
    ['证书域名', cert?.domain || '-'],
    ['颁发者', cert?.issuer || '-'],
    ['有效期至', cert?.not_after || '-'],
    ['剩余天数', cert ? `${cert.days_left} 天${cert.days_left < 0 ? '（已过期）' : cert.days_left <= 30 ? '（即将到期）' : ''}` : '-'],
    ['SHA-256 指纹', cert?.fingerprint || '-'],
  ];
  $('certificateDetails').innerHTML = fields.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
  if (resetForm) {
    $('certificateForm').reset();
    $('certificateLabel').value = cert?.label || item?.name || '';
  }
  if (item?.error) certificateMessage(item.error, true);
  certificateControls();
}

function renderCertificates(payload, resetForm = false) {
  const previous = $('certificateProject').value;
  certificateProjects = payload.projects || [];
  $('certificateProject').innerHTML = certificateProjects.length
    ? certificateProjects.map(item => `<option value="${escapeHtml(item.project)}">${escapeHtml(item.name)} (${escapeHtml(item.domain || '未设置域名')})</option>`).join('')
    : '<option value="">请先添加项目</option>';
  if (certificateProjects.some(item => item.project === previous)) $('certificateProject').value = previous;
  else resetForm = true;
  const mode = nginxSettings?.profile?.mode;
  const nginxLabel = !nginxSettings?.configured ? 'Nginx 尚未接入' : mode === 'docker' ? `Docker / ${nginxSettings.profile.container}` : mode === 'local' ? '本机 Nginx' : '暂不配置 Nginx';
  $('certificateEnvironment').textContent = `${nginxLabel} · OpenSSL ${payload.openssl_available ? '已安装' : '未安装'}`;
  renderCertificateDetails(resetForm);
}

async function refreshCertificates() {
  if (certificateBusy || nginxBusy) return;
  const request = ++certificateRequest;
  try {
    await refreshNginxSettings();
    const [payload, status] = await Promise.all([fetchJson('certificates'), fetchJson('status')]);
    csrfToken = status.csrf_token || csrfToken;
    if (request !== certificateRequest) return;
    renderCertificates(payload, !certificateLoaded);
    certificateLoaded = true;
  } catch (error) {
    certificateMessage(`证书加载失败：${error.message}`, true);
  }
}

async function certificateAction(action) {
  if (certificateBusy || nginxBusy) return;
  if (!nginxSettings?.configured || nginxSettings.profile.mode === 'none') {
    certificateMessage('请先在“访问入口”配置项目域名，或在高级接入中保存已有 Nginx。', true);
    return;
  }
  const item = certificateSelection();
  if (!item) return;
  const prompts = {
    enable: `将为 ${item.domain} 启用 HTTPS，并把 HTTP 访问重定向到 HTTPS。`,
    disable: `将停用 ${item.domain} 的 HTTPS，站点改为 HTTP。现有 HTTPS 链接将无法访问。`,
    delete: `将永久删除 ${item.name} 的上传证书、私钥及保留的旧版本。`,
    upload: item.certificate?.active ? `将替换 ${item.domain} 正在使用的证书，并重新加载 Nginx。` : '',
  };
  certificateBusy = true;
  ++certificateRequest;
  certificateControls();
  try {
    if (prompts[action] && !await showConfirmDialog({title: '确认证书操作', message: prompts[action], confirmText: '确认'})) return;
    const payload = {project: item.project, action, label: $('certificateLabel').value.trim()};
    if (action === 'upload') {
      payload.certificate = $('certificatePem').value;
      payload.private_key = $('certificatePrivateKey').value;
    }
    certificateMessage('正在处理…');
    const result = await postJsonBody('certificates', payload);
    renderCertificates(result, true);
    certificateMessage({upload: item.certificate?.active ? '证书已替换并应用。' : '证书已保存，可点击启用 HTTPS。',
      enable: 'HTTPS 已启用。', disable: 'HTTPS 已停用，站点已切回 HTTP。',
      delete: '证书已删除。', rename: '名称已修改。'}[action]);
  } catch (error) {
    certificateMessage(error.message, true);
  } finally {
    certificateBusy = false;
    certificateControls();
  }
}

$('certificateProject').addEventListener('change', () => {
  certificateMessage('');
  renderCertificateDetails(true);
});
$('certificateForm').addEventListener('submit', event => {
  event.preventDefault();
  certificateAction('upload');
});
for (const action of ['enable', 'disable', 'delete', 'rename']) {
  $(`certificate${action[0].toUpperCase()}${action.slice(1)}`).addEventListener('click', () => certificateAction(action));
}
for (const [input, target] of [['certificateFile', 'certificatePem'], ['certificateKeyFile', 'certificatePrivateKey']]) {
  $(input).addEventListener('change', async () => {
    const file = $(input).files[0];
    if (!file) return;
    if (file.size > 128 * 1024) {
      $(input).value = '';
      $(target).value = '';
      certificateMessage('单个文件不能超过 128 KB。', true);
      return;
    }
    const project = $('certificateProject').value;
    try {
      const value = await file.text();
      if (!certificateBusy && project === $('certificateProject').value && $(input).files[0] === file) $(target).value = value;
    } catch (_) {
      certificateMessage('文件读取失败，请重新选择。', true);
    }
  });
}
