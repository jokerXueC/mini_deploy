(() => {
  let editing = '';
  let timer = null;
  let loading = false;
  let busy = false;
  let settingsLoaded = false;
  let snapshot = {};
  const el = id => document.getElementById(id);
  const visible = () => ['events', 'certificates'].includes(activeView);
  const text = value => escapeHtml(String(value ?? ''));
  const time = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '等待检查';
  const duration = value => Number.isFinite(value) ? (value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`) : '-';
  const feedback = message => { el('monitorFeedback').textContent = message; };

  function certificateText(result) {
    const cert = result.certificate || {};
    if (cert.status === 'not_applicable') return 'HTTP · 无证书';
    if (cert.checked_at && cert.stale) return '证书数据待更新';
    if (cert.status === 'valid') return `证书剩余 ${cert.days_remaining} 天`;
    if (cert.status === 'invalid') return '证书校验失败';
    if (cert.status === 'unknown') return '证书暂时无法读取';
    return '证书待检查';
  }

  function render(data) {
    snapshot = data;
    const rows = data.targets || [];
    el('monitorTargets').innerHTML = rows.length ? rows.map(target => {
      const result = target.result || {};
      const status = !target.enabled ? '已暂停' : !result.checked_at ? '等待首次检查' : result.stale ? '数据待更新' : result.status === 'healthy' ? '可访问' : '访问异常';
      const tone = !target.enabled || !result.checked_at || result.stale ? 'neutral' : result.status === 'healthy' ? 'success' : 'failed';
      return `<div class="monitor-row">
        <div class="monitor-address"><strong>${text(target.name)}</strong><span>${text(target.url)}</span><small title="${text(result.detail)}">${text(result.detail || '通常 30 秒内完成首次检查')} · ${text(time(result.checked_at))}</small></div>
        <div class="monitor-readings"><span class="badge ${tone}">${status}</span><span>${duration(result.duration_ms)}</span><small>${text(certificateText(result))}</small></div>
        <details class="monitor-actions"><summary aria-label="${text(target.name)} 的操作">操作</summary><div>
          <button type="button" data-monitor-action="check" data-key="${text(target.key)}" ${!target.enabled ? 'disabled' : ''}>立即检查</button>
          <button type="button" data-monitor-action="edit" data-key="${text(target.key)}">修改网址</button>
          <button type="button" data-monitor-action="toggle" data-key="${text(target.key)}">${target.enabled ? '暂停监测' : '恢复监测'}</button>
          <button type="button" data-monitor-action="remove" data-key="${text(target.key)}">移除监测</button>
        </div></details>
      </div>`;
    }).join('') : '<div class="project-empty compact-empty">暂无监测网站</div>';
    el('monitorCertificates').innerHTML = rows.length ? rows.map(target => {
      const result = target.result || {};
      const cert = result.certificate || {};
      return `<div class="monitor-certificate-row"><div class="monitor-address"><strong>${text(target.name)}${!target.enabled ? ' · 已暂停' : ''}</strong><span>${text(target.url)}</span></div><div class="monitor-cert-detail"><strong>${text(certificateText(result))}</strong><span>${text(cert.detail || '等待检查')}${cert.issuer ? ` · ${text(cert.issuer)}` : ''}</span><small>${cert.expires_at ? `到期 ${text(time(cert.expires_at))} · ` : ''}检查于 ${text(time(cert.checked_at))}</small></div></div>`;
    }).join('') : '<div class="project-empty compact-empty">添加 HTTPS 网址后显示线上证书，无需上传证书文件</div>';
    const muted = Number(data.muted_until || 0) > Date.now() / 1000;
    el('monitorMute').textContent = muted ? '恢复通知' : '静音 1 小时';
    const delivery = data.delivery || {};
    el('monitorDelivery').textContent = [muted ? `通知静音至 ${time(data.muted_until)}` : '',
      data.notifications_enabled === false ? '通知渠道未启用，异常仍会显示在面板中' : '',
      delivery.at ? `${time(delivery.at)} · ${delivery.detail}` : ''].filter(Boolean).join(' · ');
    el('monitorNotify').textContent = data.notifications_enabled ? '通知设置' : '设置通知';
    if (!settingsLoaded && data.settings) {
      el('monitorSustain').value = data.settings.sustain_seconds;
      el('monitorRepeat').value = data.settings.repeat_seconds / 60;
      el('monitorSlow').value = data.settings.slow_ms;
      el('monitorCertDays').value = data.settings.certificate_days;
      el('monitorRecovery').checked = data.settings.recovery;
      settingsLoaded = true;
    }
    if (activeView === 'events') {
      renderAlerts(data.alerts || []);
      renderEvents(data.events || []);
    }
  }

  async function refresh() {
    window.clearTimeout(timer);
    if (!visible() || loading || busy) return;
    if (el('monitorTargets').querySelector('details[open]')) {
      timer = window.setTimeout(refresh, 15000);
      return;
    }
    loading = true;
    try {
      const data = await fetchJson('monitoring');
      if (!busy && visible()) render(data);
    } catch (error) {
      feedback(`监测加载失败：${error.message}`);
      el('monitorCertificates').textContent = `线上证书加载失败：${error.message}`;
    } finally {
      loading = false;
      if (visible()) timer = window.setTimeout(refresh, 15000);
    }
  }

  async function change(data, message) {
    if (busy) return false;
    busy = true;
    el('monitorSave').disabled = true;
    try {
      const result = await postJsonBody('monitoring', data);
      render({...result, notifications_enabled: snapshot.notifications_enabled});
      feedback(message);
      return true;
    } catch (error) {
      feedback(`操作失败：${error.message}`);
      return false;
    } finally {
      busy = false;
      el('monitorSave').disabled = false;
      window.clearTimeout(timer);
      timer = window.setTimeout(refresh, 2000);
    }
  }

  function resetForm() {
    editing = '';
    el('monitorForm').reset();
    el('monitorSave').textContent = '开始监测';
    el('monitorCancel').hidden = true;
  }
  el('monitorForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (await change({action: 'save', key: editing, url: el('monitorUrl').value}, '监测地址已保存')) resetForm();
  });
  el('monitorCancel').addEventListener('click', resetForm);
  el('monitorTargets').addEventListener('click', async event => {
    const button = event.target.closest('[data-monitor-action]');
    if (!button || busy) return;
    const {monitorAction: action, key} = button.dataset;
    const target = (snapshot.targets || []).find(item => item.key === key);
    if (!target) return;
    button.closest('details').open = false;
    if (action === 'edit') {
      editing = key;
      el('monitorUrl').value = target.url;
      el('monitorSave').textContent = '保存修改';
      el('monitorCancel').hidden = false;
      el('monitorUrl').focus();
      return;
    }
    if (action === 'remove' && !await showConfirmDialog({title: '移除监测', message: `停止监测 ${target.url}。服务器文件、网站服务和证书均会保留。`, confirmText: '移除监测'})) return;
    const ok = await change({action, key}, action === 'check' ? '已安排检查，请稍候查看结果' : action === 'remove' ? '已移除监测记录' : '监测状态已更新');
    if (ok && key === editing) resetForm();
  });
  el('monitorNotify').addEventListener('click', () => setView('notify'));
  el('monitorCertificateAdd').addEventListener('click', () => { setView('events'); el('monitorUrl').focus(); });
  el('monitorMute').addEventListener('click', () => {
    const seconds = Number(snapshot.muted_until || 0) > Date.now() / 1000 ? 0 : 3600;
    change({action: 'mute', seconds}, seconds ? '已暂停异常和恢复通知 1 小时，监测继续运行' : '通知已恢复');
  });
  el('monitorSettingsForm').addEventListener('submit', event => {
    event.preventDefault();
    change({action: 'settings', sustain_seconds: Number(el('monitorSustain').value),
      repeat_seconds: Number(el('monitorRepeat').value) * 60, slow_ms: Number(el('monitorSlow').value),
      certificate_days: Number(el('monitorCertDays').value), recovery: el('monitorRecovery').checked}, '提醒规则已保存');
  });
  window.WebsiteMonitoring = {refresh};
  refresh();
})();
