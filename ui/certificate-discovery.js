(() => {
  const el = id => document.getElementById(id);
  const text = value => escapeHtml(String(value ?? ''));
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '尚未检查';
  let timer, loading = false, busy = false, selected = null, items = [];
  const feedback = message => { el('discoveryFeedback').textContent = message; };
  const concreteDomains = item => (item.domains || []).filter(domain =>
    /^(?=.{1,253}$)[a-z\d](?:[a-z\d-]*[a-z\d])?(?:\.[a-z\d](?:[a-z\d-]*[a-z\d])?)+$/i.test(domain));
  const primarySite = item => (item.domains || []).some(domain =>
    concreteDomains({domains: [domain.replace(/^\*\./, '')]}).length);

  function row(item) {
    const cert = item.certificate;
    const online = item.online;
    const status = item.stale ? '待重新确认' : cert ? cert.not_before > Date.now() / 1000 ? '尚未生效' : cert.days_remaining < 0 ? '已过期' : cert.days_remaining <= 14 ? '即将到期' : '文件有效'
      : item.tls ? '证书待确认' : item.referenced ? 'HTTP' : '待确认';
    const tone = !item.stale && cert ? cert.days_remaining <= 14 ? 'failed' : 'success' : 'neutral';
    const origin = `${item.source} · ${item.kind === 'file' ? '文件' : item.kind}`;
    const certificateSource = item.certificate_candidate ? '自动管理目录中的匹配证书，线上使用情况待检查' : item.referenced ? '配置引用的证书' : '未确认网站引用';
    const onlineStatus = online?.status === 'checking' ? '线上检查中…' : online?.checked_at ? `${online.status === 'healthy' ? '可访问' : '访问异常'} · ${online.detail || ''}` : '';
    return `<article class="discovery-row" data-discovery-id="${text(item.id)}">
      <div class="discovery-identity"><strong>${text((item.domains || []).filter(domain => domain !== '_' && domain !== '-').join('、') || '默认站点 / 未指定域名')}</strong><small>${text(origin)}${!item.active && item.referenced ? ' · 未确认运行' : ''}</small></div>
      <div class="discovery-validity"><span class="badge ${tone}">${status}</span><small>${cert ? `${text(cert.days_remaining)} 天 · ${text(new Date(cert.expires_at * 1000).toLocaleDateString('zh-CN'))}` : text(item.renewal || '')}</small></div>
      <div class="discovery-actions">${item.referenced && concreteDomains(item).length ? `<button class="ghost-button compact-action" type="button" data-discovery-action="check" data-id="${text(item.id)}" ${item.stale || online?.status === 'checking' ? 'disabled' : ''}>检查线上</button>` : ''}${item.can_replace && !item.stale ? `<button class="ghost-button compact-action" type="button" data-discovery-action="replace" data-id="${text(item.id)}">替换证书</button>` : ''}</div>
      <details class="discovery-detail" data-detail-id="${text(item.id)}"><summary>详情${onlineStatus ? ` · ${text(onlineStatus)}` : ''}</summary>
        <dl><div><dt>证书来源</dt><dd>${text(certificateSource)}</dd></div>
        <div><dt>配置文件</dt><dd>${text(item.config_path || '未发现引用')}</dd></div>
        <div><dt>证书文件</dt><dd>${text(item.certificate_path || '未读取到文件')}</dd></div>
        <div><dt>颁发者</dt><dd>${text(cert?.issuer || '-')}</dd></div>
        <div><dt>证书管理</dt><dd>${text(item.renewal || '原服务管理')}</dd></div>
        ${item.replacement_note ? `<div><dt>操作范围</dt><dd>${text(item.replacement_note)}</dd></div>` : ''}
        <div><dt>最近发现</dt><dd>${text(date(item.seen_at))}</dd></div>
        ${online?.checked_at ? `<div><dt>线上检查</dt><dd>${text(online.url)} · ${text(date(online.checked_at))}<br>${text(online.certificate?.detail || online.detail)}${online.matches_disk === true ? ' · 与磁盘证书一致' : online.matches_disk === false ? ' · 与磁盘证书不同' : ''}<br>${text(online.note || '')}</dd></div>` : ''}</dl>
        ${(item.notes || []).map(note => `<p>${text(note)}</p>`).join('')}
      </details>
    </article>`;
  }

  function render(data) {
    csrfToken = data.csrf_token || csrfToken;
    items = data.items || [];
    const open = new Set([...document.querySelectorAll('[data-detail-id][open]')].map(node => node.dataset.detailId));
    const sites = items.filter(item => item.referenced && primarySite(item));
    const internal = items.filter(item => item.referenced && !primarySite(item));
    const files = items.filter(item => !item.referenced);
    const html = sites.length ? sites.map(row).join('') : `<div class="project-empty compact-empty">${data.running || !data.last_scan ? '正在发现服务器上的网站和证书…' : internal.length ? '暂未发现有明确域名的网站，其他入口已收起。' : '常见位置未发现网站配置，可重新扫描或查看扫描提示。'}</div>`;
    for (const [id, content] of [['discoverySites', html], ['discoveryInternalSites', internal.map(row).join('')], ['discoveryFiles', files.map(row).join('')]]) {
      if (el(id)._content !== content) { el(id).innerHTML = content; el(id)._content = content; }
    }
    document.querySelectorAll('[data-detail-id]').forEach(node => { node.open = open.has(node.dataset.detailId); });
    el('discoveryOther').hidden = !files.length;
    el('discoveryOtherCount').textContent = `(${files.length})`;
    el('discoveryInternal').hidden = !internal.length;
    el('discoveryInternalCount').textContent = `(${internal.length})`;
    el('discoveryIssues').hidden = !(data.issues || []).length;
    el('discoveryIssueList').innerHTML = (data.issues || []).map(issue => `<li>${text(issue)}</li>`).join('');
    el('discoveryProgress').textContent = `${data.progress || '等待扫描'}${data.last_scan ? ` · ${date(data.last_scan)}` : ''}`;
    el('discoveryScan').disabled = Boolean(data.running) || busy;
    return data.running || items.some(item => item.online?.status === 'checking');
  }

  async function refresh() {
    clearTimeout(timer);
    if (loading || activeView !== 'certificates' || document.hidden) return;
    loading = true;
    let fast = false;
    try { fast = render(await fetchJson('certificate-discovery')); }
    catch (error) { feedback(`发现列表加载失败：${error.message}`); }
    finally { loading = false; timer = setTimeout(refresh, fast ? 1500 : 15000); }
  }

  async function action(payload) {
    if (busy) return;
    busy = true;
    el('discoveryScan').disabled = true;
    feedback(payload.action === 'scan' ? '正在扫描…' : '正在检查…');
    try { render(await postJsonBody('certificate-discovery', payload)); feedback(''); }
    catch (error) { feedback(error.message); }
    finally { busy = false; refresh(); }
  }

  el('discoveryScan').addEventListener('click', () => action({action: 'scan'}));
  el('certificatesView').addEventListener('click', event => {
    const button = event.target.closest('[data-discovery-action]');
    if (!button || busy) return;
    if (button.dataset.discoveryAction === 'check') action({action: 'check', id: button.dataset.id});
    else {
      selected = items.find(item => item.id === button.dataset.id);
      el('discoveryReplace').reset();
      el('discoveryReplaceTitle').textContent = `替换证书 · ${selected.domains.join('、')}`;
      el('discoveryReplace').hidden = false;
      el('discoveryReplace').scrollIntoView({block: 'nearest'});
    }
  });
  el('discoveryReplaceCancel').addEventListener('click', () => {
    if (busy) return;
    selected = null;
    el('discoveryReplace').reset();
    el('discoveryReplace').hidden = true;
  });
  el('discoveryReplace').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !selected) return;
    const cert = el('discoveryCertFile').files[0], key = el('discoveryKeyFile').files[0];
    if (!cert || !key || cert.size > 131072 || key.size > 131072) { feedback('请选择不超过 128KB 的证书链和私钥文件。'); return; }
    busy = true;
    el('discoveryReplaceSave').disabled = true;
    el('discoveryReplaceCancel').disabled = true;
    try {
      if (!await showConfirmDialog({title: '替换证书', message: '将校验共用此证书的站点域名，备份原文件并重载服务。校验或重载失败时恢复原文件。', confirmText: '替换并应用'})) return;
      feedback('正在校验证书并应用，请稍候…');
      const result = await postJsonBody('certificate-discovery', {action: 'replace', id: selected.id,
        fingerprint: selected.certificate.fingerprint, certificate: await cert.text(), private_key: await key.text()});
      feedback(`${result.message}。备份：${result.backup}`);
      el('discoveryReplace').reset();
      el('discoveryReplace').hidden = true;
      selected = null;
    } catch (error) { feedback(error.message); }
    finally { busy = false; el('discoveryReplaceSave').disabled = false; el('discoveryReplaceCancel').disabled = false; refresh(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.CertificateDiscovery = {refresh};
})();
