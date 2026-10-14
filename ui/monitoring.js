(() => {
  let editing = '';
  let timer = null;
  let loading = false;
  let busy = false;
  let settingsLoaded = false;
  let snapshot = {};
  let generation = 0;
  let loaded = false;
  let loadError = '';
  const pageSize = 50;
  let historyPage = 0;
  let operationPage = 0;
  const el = id => document.getElementById(id);
  const visible = () => ['events', 'certificates'].includes(activeView);
  const text = value => escapeHtml(String(value ?? ''));
  const time = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}) : '等待检查';
  const duration = value => Number.isFinite(value) ? (value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(2)} ms`) : '-';
  const feedback = message => { el('monitorFeedback').textContent = message; };
  const sources = {docker: '容器', container: '容器', server: '服务器资源', cpu: '处理器', memory: '内存', disk: '磁盘',
    resource: '服务器资源', website: '网站探测', health: '服务健康检查', certificate: '证书', monitoring: '监测服务', docker_events: '容器生命周期'};
  const sourceName = source => sources[source] || (source ? `其他来源（${source}）` : '来源未提供');
  const severityName = level => ({critical: '严重', warning: '警告', info: '提示', success: '恢复'}[level] || '提示');
  const activeAlerts = () => (snapshot.alerts || []).filter(item => item.active !== false);
  const operationKinds = new Set(['audit', 'operation', 'config', 'configured', 'settings', 'mute', 'save', 'remove', 'toggle', 'check']);
  const isOperation = item => operationKinds.has(item.kind) || String(item.kind || '').startsWith('monitoring_');
  const operationNames = {operation: '面板操作', audit: '操作审计', config: '配置变更', configured: '配置变更',
    settings: '提醒规则', mute: '通知静音', save: '保存监测地址', remove: '移除监测', toggle: '切换监测', check: '手动检查',
    monitoring_save: '保存监测地址', monitoring_remove: '移除监测', monitoring_toggle: '切换监测', monitoring_settings: '提醒规则', monitoring_mute: '通知静音', monitoring_check: '手动检查'};
  const reasonNames = {oom: '内存溢出', oom_killed: '内存溢出退出', restarts: '反复重启', restart_storm: '反复重启',
    health: '健康检查失败', unhealthy: '健康检查失败', exit: '异常退出', abnormal_exit: '异常退出',
    configured_container_missing: '配置容器未找到', container_disappeared: '容器消失', missing: '容器未找到', not_running: '容器未运行',
    cpu:'CPU 持续超限', memory:'内存持续超限', disk:'根分区空间不足', inode:'inode 不足', swap:'Swap 占用高', iowait:'I/O 等待高', load:'系统负载高', memory_growth:'内存持续增长'};
  const metadata = item => {
    const meta = item.metadata || {};
    return {...meta, container: meta.container ?? meta.name, container_id: meta.container_id ?? meta.id,
      project: meta.project ?? meta.compose_project, service: meta.service ?? meta.compose_service,
      health_exit_code: meta.health_last_check?.exit_code, health_checked_at: meta.health_last_check?.end,
      restart_window_seconds: meta.restart_window_seconds ?? meta.window_seconds};
  };

  function historyStatus(item) {
    const kind = String(item.kind || '');
    if (kind === 'lifecycle' || item.source === 'docker_events') return 'lifecycle';
    if (['recovery', 'recovered', 'resolved'].includes(kind)) return 'recovery';
    if (['acknowledged', 'acknowledge'].includes(kind)) return 'acknowledged';
    if (['disappeared', 'container_missing', 'container_disappeared'].includes(kind)
      || /configured_container_missing|container_disappeared/.test(String(item.metadata?.reason || ''))
      || /配置.*容器.*(消失|不存在|未找到)/.test(item.title || '')) return 'disappeared';
    return 'alert';
  }

  function syncSources(id, items) {
    const select = el(id), selected = select.value;
    const available = new Set(items.map(item => item.source).filter(Boolean));
    // Keep a selected source even when its last incident recovers during refresh.
    if (selected !== 'all') available.add(selected);
    const ordered = [...available].sort((a, b) => sourceName(a).localeCompare(sourceName(b), 'zh-CN'));
    const signature = JSON.stringify(ordered);
    if (select.dataset.sources !== signature) {
      select.innerHTML = '<option value="all">全部来源</option>' + ordered.map(source => `<option value="${text(source)}">${text(sourceName(source))}</option>`).join('');
      select.value = selected;
      select.dataset.sources = signature;
    }
  }

  function elapsed(seconds) {
    if (!Number.isFinite(seconds)) return '';
    const value = Math.max(0, Math.floor(seconds));
    return value >= 86400 ? `${Math.floor(value / 86400)} 天 ${Math.floor(value % 86400 / 3600)} 小时`
      : value >= 3600 ? `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分钟`
      : value >= 60 ? `${Math.floor(value / 60)} 分钟` : `${value} 秒`;
  }

  function guidance(item) {
    const meta = metadata(item);
    if (meta.oom_killed) return '检查容器内存限制、宿主机余量及退出前的日志，排查内存峰值后再恢复服务。';
    if (meta.reason === 'configured_container_missing' || historyStatus(item) === 'disappeared') return '核对配置的容器名称与项目部署状态，确认容器是否重建或被移除。';
    if (item.source === 'certificate') return '检查证书到期时间、续期任务及线上证书是否已替换。';
    if (meta.health_output || meta.health_last_check || meta.reason === 'unhealthy') return '根据健康检查退出码核对探测地址、依赖服务与容器日志。';
    if (meta.restart_count || /restart|重启/.test(String(meta.reason || '') + (item.title || ''))) return '查看每次退出前的日志、退出码和重启策略，排查进程反复退出的原因。';
    if (item.source === 'docker' || item.source === 'container') return '查看容器日志和退出码，核对启动配置及依赖服务。';
    if (/disk|磁盘/.test(String(item.id || '') + (item.title || ''))) return '检查磁盘占用、日志增长和可清理文件，保留业务数据及恢复所需备份。';
    if (['server', 'cpu', 'memory', 'resource'].includes(item.source)) return '核对资源趋势及高占用进程，确认是否持续超限。';
    if (['website', 'health'].includes(item.source)) return '核对地址、响应状态及服务日志，检查网络和依赖服务。';
    return '核对最近采样及服务日志，确认异常原因。';
  }

  function evidence(item, current) {
    const meta = metadata(item);
    const fields = [['container', '容器'], ['container_id', '容器 ID'], ['project', '项目'], ['service', '服务'],
      ['exit_code', '退出码'], ['oom_killed', '内存溢出'], ['restart_count', '重启次数'], ['restart_delta_5m', '近 5 分钟重启增量'],
      ['restart_window_seconds', '重启统计窗口（秒）'], ['restart_policy', '重启策略'], ['started_at', '启动时间'], ['finished_at', '退出时间'],
      ['health_exit_code', '健康检查退出码'], ['health_checked_at', '健康检查时间'], ['health_output', '健康检查输出'], ['action', '容器事件'], ['reason', '原因'],
      ['value', '采样值'], ['threshold', '阈值'], ['unit', '单位'], ['recovering', '正在确认恢复']];
    const entries = fields.filter(([key]) => meta[key] !== undefined && meta[key] !== null && meta[key] !== '')
      .map(([key, label]) => `<div><dt>${label}</dt><dd>${text(key === 'reason' ? reasonNames[meta[key]] || meta[key] : typeof meta[key] === 'boolean' ? meta[key] ? '是' : '否' : meta[key])}</dd></div>`);
    if (item.incident_id) entries.push(`<div><dt>异常 ID</dt><dd>${text(item.incident_id)}</dd></div>`);
    const container = meta.container || meta.container_id;
    return `<div class="incident-evidence">${item.detail ? `<p>${text(item.detail)}</p>` : ''}
      ${entries.length ? `<dl>${entries.join('')}</dl>` : '<p>暂无更多采样证据</p>'}
      ${item.command ? `<code>${text(item.command)}</code>` : ''}
      <p class="incident-guidance">${text(guidance(item))}</p>
      ${current && item.stale ? '<p>采样待更新，当前异常尚未恢复。</p>' : ''}
      <div class="certificate-actions">${item.id ? `<button type="button" class="ghost-button compact-action" data-incident-diagnose="${text(item.id)}">排查异常</button><button type="button" class="ghost-button compact-action" data-incident-ai="${text(item.id)}">AI 诊断</button>` : ''}${container ? `<button type="button" class="ghost-button compact-action" data-incident-log="${text(container)}">查看容器日志</button>` : ''}
      ${item.source === 'certificate' ? '<a class="ghost-button compact-action" href="?view=certificates" data-incident-certificate>查看证书</a>' : ''}</div></div>`;
  }

  function replaceRows(id, html) {
    const root = el(id);
    const opened = new Set([...root.querySelectorAll('details[open]')].map(node => node.dataset.incidentRow));
    const focused = root.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.closest('.incident-row')?.querySelector('details')?.dataset.incidentRow;
    const focusAction = focused?.tagName === 'SUMMARY' ? 'summary' : focused?.hasAttribute('data-incident-ack') ? '[data-incident-ack]' : focused?.hasAttribute('data-incident-log') ? '[data-incident-log]' : null;
    root.classList.remove('skeleton-block');
    root.innerHTML = html;
    root.querySelectorAll('[data-incident-row]').forEach(node => {
      if (opened.has(node.dataset.incidentRow)) node.open = true;
      if (focusKey === node.dataset.incidentRow && focusAction) (node.parentElement.querySelector(focusAction) || node.querySelector('summary'))?.focus({preventScroll: true});
    });
  }

  function row(item, index, current) {
    const status = current ? item.acknowledged_at ? 'acknowledged' : 'alert' : historyStatus(item);
    const state = current ? item.acknowledged_at ? '已知晓 · 仍异常' : '待处理' : {alert: '异常', recovery: '已恢复', acknowledged: '已知晓', disappeared: '配置容器消失', lifecycle: '容器生命周期'}[status];
    const key = current ? item.incident_id || item.id || index : item.id || `${item.incident_id || ''}:${item.kind}:${item.at_ts || item.at || index}`;
    const since = current && item.first_seen ? `首次发现 ${time(item.first_seen)} · 持续 ${elapsed(Date.now() / 1000 - item.first_seen)}` : item.at_ts ? time(item.at_ts) : item.at || '时间未提供';
    const totalDuration = !current && Number.isFinite(item.duration_seconds) ? ` · 持续 ${elapsed(item.duration_seconds)}` : '';
    const acknowledged = current && item.acknowledged_at ? ` · 知晓于 ${time(item.acknowledged_at)}` : '';
    return `<div class="incident-row ${status === 'recovery' ? 'recovered' : item.level === 'critical' ? 'critical' : item.level === 'warning' ? 'warning' : 'neutral'}">
      <details data-incident-row="${text(key)}"><summary><div class="incident-row-title"><strong>${text(item.title || '异常')}</strong>
        <span class="incident-state">${text(state)}${item.stale ? ' · 采样待更新' : ''}</span></div>
        <small>${text(sourceName(item.source))} · ${text(severityName(item.level))} · ${text(since + totalDuration + acknowledged)}${current && item.muted ? ' · 通知已静音' : ''}</small></summary>
        ${evidence(item, current)}</details>
      ${current && !item.acknowledged_at ? `<button type="button" class="ghost-button compact-action incident-ack" data-incident-ack="${text(item.id)}" data-incident-id="${text(item.incident_id)}" ${busy || !item.id || !item.incident_id ? 'disabled' : ''} title="${item.incident_id ? '标为已知晓后停止本次异常的重复提醒，恢复前继续显示' : '等待服务提供异常 ID'}">已知晓</button>` : ''}
    </div>`;
  }

  function renderIncidents() {
    const alerts = activeAlerts(), history = (snapshot.events || []).filter(item => !isOperation(item));
    const operations = (snapshot.events || []).filter(isOperation);
    syncSources('incidentSource', alerts);
    syncSources('historySource', history);
    const critical = alerts.filter(item => item.level === 'critical').length;
    const acknowledged = alerts.filter(item => item.acknowledged_at).length;
    el('alertBadge').className = `badge ${alerts.length ? critical ? 'failed' : 'running' : 'neutral'}`;
    el('alertBadge').textContent = alerts.length ? `${alerts.length} 项异常` : loaded ? '暂无当前异常' : '等待采样';
    el('eventTimelineHint').textContent = alerts.length ? `${critical} 项严重 · ${alerts.length - critical} 项其他异常 · ${acknowledged} 项已知晓` : loaded ? '暂无当前异常，采样覆盖见下方' : '等待监测采样';
    const coverage = snapshot.coverage || {};
    const coverageKeys = ['server', 'docker', ...(coverage.docker_events ? ['docker_events'] : [])];
    const latestSample = Math.max(0, ...coverageKeys.map(key => Number(coverage[key]?.checked_at) || 0));
    el('incidentCoverage').innerHTML = coverageKeys.map(key => {
      const sample = coverage[key] || {};
      const fresh = Number(sample.checked_at) > 0 && Date.now() / 1000 - sample.checked_at <= 120;
      const available = !loadError && sample.status === 'healthy' && fresh;
      return `<span class="incident-coverage-item ${available ? 'available' : ''}" title="${text(sample.detail || '服务未提供采样详情')}">${{server: '服务器', docker: '容器', docker_events: '容器生命周期'}[key]} · ${available ? '采样可用' : sample.status === 'disabled' ? '告警暂停' : sample.status === 'not_installed' ? '未安装' : '采样未知'}</span>`;
    }).join('') + (latestSample ? `<small>最近采样 ${text(time(latestSample))}</small>` : '');
    el('acknowledgeAllBtn').disabled = busy || !alerts.some(item => !item.acknowledged_at);
    const severity = el('incidentSeverity').value, source = el('incidentSource').value, state = el('incidentStatus').value;
    const filtered = alerts.filter(item => (severity === 'all' || (item.level || 'info') === severity) && (source === 'all' || item.source === source)
      && (state === 'all' || state === 'acknowledged' && item.acknowledged_at || state === 'unacknowledged' && !item.acknowledged_at || state === 'stale' && item.stale));
    el('resetIncidentFilters').hidden = severity === 'all' && source === 'all' && state === 'all';
    replaceRows('alertList', filtered.length ? filtered.map((item, i) => row(item, i, true)).join('') : `<p class="incident-empty">${loadError ? '监测暂不可用；保留最近一次结果，等待重试。' : !loaded ? '正在读取监测数据…' : alerts.length ? '没有符合筛选的异常，可重置筛选查看全部。' : '暂无当前异常。采样未知或待更新时，尚不能确认已恢复。'}</p>`);
    const historySource = el('historySource').value, historyState = el('historyStatus').value;
    const selected = history.filter(item => (historySource === 'all' || item.source === historySource) && (historyState === 'all' || historyStatus(item) === historyState))
      .sort((a, b) => (b.at_ts || 0) - (a.at_ts || 0));
    const retention = Number(snapshot.history_retention_days), limit = Number(snapshot.history_limit);
    el('incidentHistoryHint').textContent = `${history.length} 条异常、恢复、知晓与生命周期记录${retention > 0 ? ` · 保留 ${retention} 天` : ''}${limit > 0 ? ` · 最多 ${limit} 条` : ''}`;
    historyPage = updatePager('history', historyPage, selected.length, history.length);
    replaceRows('eventTimeline', selected.length ? selected.slice(historyPage * pageSize, (historyPage + 1) * pageSize).map((item, i) => row(item, i, false)).join('') : `<p class="incident-empty">${history.length ? '没有符合筛选的历史记录。' : '暂无异常历史记录'}</p>`);
    el('incidentOperationCount').textContent = operations.length ? `· ${operations.length} 条` : '';
    if (el('incidentOperations').open) renderOperations();
    else el('incidentOperationList').replaceChildren();
    window.AppSelects?.syncAll();
  }

  function updatePager(prefix, page, filtered, total) {
    const pages = Math.max(1, Math.ceil(filtered / pageSize));
    const current = Math.min(page, pages - 1);
    el(`${prefix}PageInfo`).textContent = `筛选 ${filtered} / 共 ${total} 条 · 第 ${current + 1} / ${pages} 页`;
    el(`${prefix}Prev`).disabled = current === 0;
    el(`${prefix}Next`).disabled = current >= pages - 1;
    return current;
  }

  function renderOperations() {
    if (!el('incidentOperations').open) { el('incidentOperationList').replaceChildren(); return; }
    const operations = (snapshot.events || []).filter(isOperation);
    const selectedOperation = el('operationKind').value;
    const kinds = new Set(operations.map(item => item.kind));
    if (selectedOperation !== 'all') kinds.add(selectedOperation);
    const ordered = [...kinds].sort(), signature = JSON.stringify(ordered);
    if (el('operationKind').dataset.kinds !== signature) {
      el('operationKind').innerHTML = '<option value="all">全部操作</option>' + ordered.map(kind => `<option value="${text(kind)}">${text(operationNames[kind] || '其他操作')}</option>`).join('');
      el('operationKind').value = selectedOperation;
      el('operationKind').dataset.kinds = signature;
    }
    const selectedOperations = operations.filter(item => selectedOperation === 'all' || item.kind === selectedOperation);
    operationPage = updatePager('operation', operationPage, selectedOperations.length, operations.length);
    el('incidentOperationList').innerHTML = selectedOperations.length ? selectedOperations.slice(operationPage * pageSize, (operationPage + 1) * pageSize).map(item => `<div class="incident-operation"><strong>${text(item.title || '操作记录')}</strong><small>${text(sourceName(item.source))} · ${text(item.at_ts ? time(item.at_ts) : item.at || '')}</small><p>${text(item.detail)}</p></div>`).join('') : `<p class="incident-empty">${operations.length ? '没有符合筛选的操作记录。' : '当前接口未提供操作记录'}</p>`;
    window.AppSelects?.syncAll();
  }

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
    loaded = true;
    loadError = '';
    csrfToken = data.csrf_token || csrfToken;
    const rows = data.targets || [];
    el('monitorTargetCount').textContent = rows.length ? `· ${rows.length} 个` : '';
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
      const inputs = {monitorCpuThreshold:'cpu_threshold', monitorMemoryThreshold:'memory_threshold', monitorDiskThreshold:'disk_threshold', monitorInodeThreshold:'inode_threshold', monitorSwapThreshold:'swap_threshold', monitorIowaitThreshold:'iowait_threshold', monitorLoadThreshold:'load_threshold_percent', monitorMemoryGrowth:'memory_growth_mb', monitorResourceRecovery:'resource_recovery_seconds'};
      for (const [id, key] of Object.entries(inputs)) if (data.settings[key] != null) el(id).value = data.settings[key];
      el('monitorResourceEnabled').checked = data.settings.resource_enabled !== false;
      settingsLoaded = true;
    }
    if (activeView === 'events') {
      renderIncidents();
      el('updatedAt').textContent = `异常中心刷新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
    }
  }

  async function refresh() {
    window.clearTimeout(timer);
    if (!visible() || document.hidden || loading || busy) return;
    if (el('monitorTargets').querySelector('details[open]')) {
      timer = window.setTimeout(refresh, 15000);
      return;
    }
    loading = true;
    const requestedGeneration = generation;
    try {
      if (!csrfToken) {
        const status = await fetchJson('status');
        csrfToken = status.csrf_token || csrfToken;
      }
      const data = await fetchJson('monitoring');
      if (!busy && visible() && requestedGeneration === generation) {
        render(data);
        el('incidentFeedback').textContent = '';
      }
    } catch (error) {
      if (requestedGeneration !== generation || !visible()) return;
      loadError = error.message;
      feedback(`监测加载失败：${error.message}`);
      el('incidentFeedback').textContent = `监测加载失败：${error.message}。最近结果尚未更新，正在重试。`;
      if (activeView === 'events') renderIncidents();
      el('monitorCertificates').textContent = `线上证书加载失败：${error.message}`;
    } finally {
      loading = false;
      if (visible()) timer = window.setTimeout(refresh, 15000);
    }
  }

  async function change(data, message) {
    if (busy) return false;
    busy = true;
    generation += 1;
    el('monitorSave').disabled = true;
    if (activeView === 'events') renderIncidents();
    try {
      const result = await postJsonBody('monitoring', data);
      render({...snapshot, ...result});
      feedback(message);
      el('incidentFeedback').textContent = message;
      return true;
    } catch (error) {
      feedback(`操作失败：${error.message}`);
      el('incidentFeedback').textContent = `操作失败：${error.message}`;
      return false;
    } finally {
      busy = false;
      el('monitorSave').disabled = false;
      if (activeView === 'events') renderIncidents();
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
  el('monitorCertificateAdd').addEventListener('click', () => { setView('events'); el('monitorWebsiteSection').open = true; el('monitorUrl').focus(); });
  el('monitorMute').addEventListener('click', () => {
    const seconds = Number(snapshot.muted_until || 0) > Date.now() / 1000 ? 0 : 3600;
    change({action: 'mute', seconds}, seconds ? '已暂停异常和恢复通知 1 小时，监测继续运行' : '通知已恢复');
  });
  el('monitorSettingsForm').addEventListener('submit', event => {
    event.preventDefault();
    change({action: 'settings', sustain_seconds: Number(el('monitorSustain').value),
      repeat_seconds: Number(el('monitorRepeat').value) * 60, slow_ms: Number(el('monitorSlow').value),
      certificate_days: Number(el('monitorCertDays').value), recovery: el('monitorRecovery').checked,
      resource_enabled: el('monitorResourceEnabled').checked, cpu_threshold:Number(el('monitorCpuThreshold').value),
      memory_threshold:Number(el('monitorMemoryThreshold').value), disk_threshold:Number(el('monitorDiskThreshold').value),
      inode_threshold:Number(el('monitorInodeThreshold').value), swap_threshold:Number(el('monitorSwapThreshold').value),
      iowait_threshold:Number(el('monitorIowaitThreshold').value), load_threshold_percent:Number(el('monitorLoadThreshold').value),
      memory_growth_mb:Number(el('monitorMemoryGrowth').value), resource_recovery_seconds:Number(el('monitorResourceRecovery').value)}, '提醒规则已保存');
  });
  const acknowledgeAll = () => change({action: 'acknowledge_all'}, '当前异常已标为已知晓；恢复前仍保留显示');
  el('acknowledgeAllBtn').addEventListener('click', acknowledgeAll);
  ['incidentSeverity', 'incidentSource', 'incidentStatus'].forEach(id => el(id).addEventListener('change', renderIncidents));
  ['historySource', 'historyStatus'].forEach(id => el(id).addEventListener('change', () => { historyPage = 0; renderIncidents(); }));
  el('operationKind').addEventListener('change', () => { operationPage = 0; renderOperations(); });
  el('incidentOperations').addEventListener('toggle', renderOperations);
  el('historyPrev').addEventListener('click', () => { historyPage = Math.max(0, historyPage - 1); renderIncidents(); });
  el('historyNext').addEventListener('click', () => { historyPage += 1; renderIncidents(); });
  el('operationPrev').addEventListener('click', () => { operationPage = Math.max(0, operationPage - 1); renderOperations(); });
  el('operationNext').addEventListener('click', () => { operationPage += 1; renderOperations(); });
  el('resetIncidentFilters').addEventListener('click', () => {
    ['incidentSeverity', 'incidentSource', 'incidentStatus'].forEach(id => { el(id).value = 'all'; });
    renderIncidents();
  });
  el('eventsView').addEventListener('click', event => {
    const ack = event.target.closest('[data-incident-ack]');
    if (ack && !ack.disabled && ack.dataset.incidentId) change({action: 'acknowledge', key: ack.dataset.incidentAck, incident_id: ack.dataset.incidentId}, '异常已标为已知晓；恢复前仍保留显示');
    const log = event.target.closest('[data-incident-log]');
    if (log) showContainerLogs(log.dataset.incidentLog);
    const diagnose = event.target.closest('[data-incident-diagnose]');
    if (diagnose) window.ServerOperations.open('incident', diagnose.dataset.incidentDiagnose);
    const ai = event.target.closest('[data-incident-ai]');
    if (ai) window.PanelAI.open({kind:'incident', id:ai.dataset.incidentAi});
    if (event.target.closest('[data-incident-certificate]')) { event.preventDefault(); setView('certificates'); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) window.clearTimeout(timer);
    else refresh();
  });
  window.WebsiteMonitoring = {refresh, acknowledgeAll};
  refresh();
})();
