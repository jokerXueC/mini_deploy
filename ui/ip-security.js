(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let status = {}, token = '', busy = false, loading = false, timer, view = false, historyPage = 0;
  let activityPage = 0;
  let readError = '';
  const drafts = {};
  const text = value => escapeHtml(String(value ?? ''));
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '未知';
  const feedback = text => { $('ipSecurityFeedback').textContent = text; };
  function readSettings() {
    return {maxretry:$('ipSecurityMaxretry').value, findtime:String(Number($('ipSecurityFindtime').value) * 60),
      bantime:String(Number($('ipSecurityBantime').value) * 60), ignoreip:$('ipSecurityIgnore').value,
      port:$('ipSecurityPorts').value === 'web' ? '6868,80,443' : '6868',
      'bantime.increment':String($('ipSecurityIncrement').checked)};
  }
  function renderSettings() {
    const target = $('ipSecuritySettingsTarget').value;
    const settings = drafts[target] || status[target === 'ssh' ? 'ssh_settings' : 'settings'] ||
      {maxretry:'5', findtime:'600', bantime:'3600', ignoreip:'', port:'6868'};
    for (const [id, key, scale] of [['Maxretry','maxretry',1], ['Findtime','findtime',60], ['Bantime','bantime',60]]) $('ipSecurity' + id).value = Number(settings[key]) / scale;
    $('ipSecurityIgnore').value = (settings.ignoreip || '').split(/\s+/).filter(Boolean).join('\n');
    $('ipSecurityPorts').value = (settings.port || '').includes('80') ? 'web' : 'panel';
    $('ipSecurityPorts').closest('label').hidden = target === 'ssh';
    $('ipSecurityIncrement').checked = settings['bantime.increment'] === 'true';
  }
  function controls() {
    const waiting = busy || loading, unavailable = waiting || status.read_only || !!readError;
    $('ipSecurityRefresh').disabled = waiting;
    $('securityActivityPrev').disabled = waiting || activityPage === 0;
    $('securityActivityNext').disabled = waiting || (activityPage + 1) * (status.activity?.page_size || 50) >= (status.activity?.total || 0);
    $('securityActivitySource').disabled = waiting;
    $('ipSecuritySetup').disabled = unavailable || !status.supported;
    $('ipSecurityDisable').disabled = unavailable;
    $('ipSecurityDisable').hidden = !status.panel_enabled;
    const ssh = status.observation?.ssh || {};
    $('ipSecuritySSHSetup').disabled = unavailable || !status.supported || !ssh.ready || status.existing_ssh;
    $('ipSecuritySSHDisable').hidden = !status.ssh_enabled;
    $('ipSecuritySSHDisable').disabled = unavailable;
    $('ipSecurityHistoryPrev').disabled = waiting || historyPage === 0;
    $('ipSecurityHistoryNext').disabled = waiting || (historyPage + 1) * 50 >= (status.history?.total || 0);
    $('ipSecurityJail').disabled = waiting || !status.running;
    $('ipSecurityBan').disabled = unavailable || !status.running || !$('ipSecurityJail').value;
    $('ipSecurityBanForm').querySelector('input').disabled = unavailable;
    document.querySelectorAll('[data-unban-ip]').forEach(button => { button.disabled = unavailable; });
  }
  async function api(data, job = false, scan = false) {
    const query = new URLSearchParams(job ? {job:'1'} : $('ipSecurityJail').value ? {jail:$('ipSecurityJail').value} : {});
    if (!job) {
      query.set('history_page', String(historyPage)); query.set('activity_page', String(activityPage));
      query.set('activity_source', $('securityActivitySource').value); if (scan) query.set('scan','1');
    }
    const response = await fetch(`security/center?${query}`, {method:data ? 'POST' : 'GET', credentials:'same-origin',
      headers:data ? {'Content-Type':'application/json', 'X-CSRF-Token':token} : {}, body:data ? JSON.stringify(data) : undefined});
    const body = await response.json();
    if (!response.ok) throw Object.assign(new Error(body.detail || '无法读取安全中心状态'), {status: response.status});
    token = body.csrf_token || token; return body;
  }
  function render() {
    renderCenter();
    const labels = {login:'登录失败', ip_security_configure:'配置面板防护', ip_security_disable:'暂停面板防护', ip_security_configure_ssh:'配置 SSH 防护', ip_security_disable_ssh:'暂停 SSH 防护', ip_security_ban:'手动封禁', ip_security_unban:'手动解封'};
    $('ipSecurityHistory').innerHTML = (status.recent || []).length ? status.recent.map(item => `<div class="ip-security-history-row"><time>${escapeHtml(item.at || '')}</time><strong>${labels[item.action] || '防护操作'}${item.success === false && item.action !== 'login' ? ' · 未完成' : ''}</strong><span>${escapeHtml(item.ip || item.actor || '')}</span><span>${escapeHtml(item.jail || '')}</span></div>`).join('') : '<p class="muted">暂无相关面板审计记录</p>';
    $('ipSecurityStatus').textContent = !status.supported ? '仅支持 Linux 主机，本地可以预览界面' : !status.installed ? '尚未安装 Fail2ban' : status.running ? 'Fail2ban 运行中' : 'Fail2ban 已安装，服务未就绪';
    $('ipSecurityClient').textContent = status.client_ip ? `当前访问 IP：${status.client_ip} · 不允许手动封禁此地址` : '';
    $('ipSecurityNotice').textContent = status.notice || '';
    $('ipSecuritySetup').textContent = status.panel_enabled ? '保存面板防护设置' : status.installed ? '启用面板防护' : '安装并启用面板防护';
    const facts = status.observation || {}, ssh = facts.ssh || {};
    $('ipSecuritySSHInfo').textContent = [ssh.ports?.length ? `监听端口 ${ssh.ports.join('、')}` : '', ssh.detail || '等待后台检测', status.existing_ssh ? '已有 sshd 规则，沿用原配置' : status.ssh_enabled ? '专属规则运行中' : ''].filter(Boolean).join(' · ');
    $('ipSecuritySSHSetup').textContent = status.existing_ssh ? '沿用已有 SSH 规则' : status.ssh_enabled ? '保存 SSH 防护设置' : '启用 SSH 防护';
    $('ipSecurityCheckedAt').textContent = (facts.checked_at ? `最近检查 ${date(facts.checked_at)}` : '等待首次检查') + (status.scanning ? ' · 检测中' : '') + (facts.scan_error ? ` · ${facts.scan_error}` : '');
    const checks = [...(facts.checks || []), ...(facts.center?.checks || [])];
    $('ipSecurityChecksSummary').textContent = `防护状态核验${checks.some(item => item.state !== 'known') ? ' · 有待核实项' : ''}`;
    $('ipSecurityChecks').innerHTML = checks.map(item => `<div class="ip-security-check"><strong>${text(item.name)}</strong><span class="muted">${text(item.detail)}</span></div>`).join('') || '<p class="muted">正在等待检查结果</p>';
    const ports = facts.ports || [];
    $('ipSecurityPortsSummary').textContent = `端口暴露检查${facts.checked_at ? ` · ${ports.length} 项需核对` : ''}`;
    $('ipSecurityPortFindings').innerHTML = ports.length ? ports.map(item => `<div class="ip-security-check"><strong>${text(item.service)} · ${text(item.host)}:${text(item.port)}</strong><span>${item.kind === 'docker' ? `Docker ${text(item.container)} → ${text(item.container_port)}/${text(item.protocol)}` : '主机监听'}<small>${text(item.detail)}</small></span></div>`).join('') : '<p class="muted">当前可读取范围内未发现上述常见端口，请结合检查状态判断。</p>';
    $('ipSecurityScanWarnings').textContent = (facts.warnings || []).join('；');
    const history = status.history || {};
    $('ipSecurityLogStatus').textContent = [history.detail || '等待后台采集', history.limited ? '达到本轮采集上限，可能存在空档' : '', history.notice || ''].filter(Boolean).join('；');
    $('ipSecurityEvents').innerHTML = (history.items || []).map(item => `<div class="ip-security-event"><time>${text(date(item.ts))}</time><strong>${text(item.ip)}</strong><span>${text(item.jail)} · ${{ban:'封禁',restore:'恢复封禁',unban:'解封'}[item.kind] || '记录'}</span><small>${text(item.reason)}${item.observed_failures_10m ? ` · 前 10 分钟观测到 ${text(item.observed_failures_10m)} 条失败日志` : ''}</small></div>`).join('') || '<p class="muted">暂无已采集的封禁记录；不代表历史上没有攻击。</p>';
    $('ipSecurityHistoryPage').textContent = `第 ${historyPage + 1} 页 · 共 ${history.total || 0} 条`;
    const selected = $('ipSecurityJail').value;
    const options = '<option value="">选择规则</option>' + (status.jails || []).map(jail => `<option value="${escapeHtml(jail)}">${escapeHtml(jail)}</option>`).join('');
    if ($('ipSecurityJail').innerHTML !== options) $('ipSecurityJail').innerHTML = options;
    $('ipSecurityJail').value = (status.jails || []).includes(selected) ? selected : '';
    const rule = status.selected;
    $('ipSecurityRuleInfo').textContent = rule ? `${rule.banned.length} 个封禁 IP · 规则封禁时长 ${rule.bantime} 秒 · ${rule.findtime} 秒内失败 ${rule.maxretry} 次` : '';
    $('ipSecurityRuleWhitelist').textContent = rule ? `规则白名单：${rule.ignoreip?.join('、') || '未提供'}` : '';
    $('ipSecurityBanned').innerHTML = rule ? rule.banned.length ? rule.banned.map(ip => {
      const info = rule.ban_details?.[ip];
      return `<div class="ip-security-ip"><div><code>${escapeHtml(ip)}</code><small>${info ? info.permanent ? '永久封禁' : `到期 ${text(date(info.expires_at))}` : '具体到期时间未提供'}</small></div><button type="button" class="ghost-button compact-action" data-unban-ip="${escapeHtml(ip)}">解封</button></div>`;
    }).join('') : '<p class="muted">所选规则暂无封禁 IP</p>' : '<p class="muted">选择规则后查看封禁 IP</p>';
    renderSettings();
    window.AppSelects?.syncAll(); controls();
  }
  function renderCenter() {
    const facts = status.observation || {}, center = facts.center || {}, activity = status.activity || {};
    const fresh = center.checked_at && Date.now() / 1000 - center.checked_at <= 180 && !facts.scan_error && !readError;
    const unknown = (center.checks || []).filter(item => item.state === 'unknown').length + (facts.host_readable ? 0 : 1) + (facts.docker_readable ? 0 : 1) + (activity.auth?.readable ? 0 : 1);
    const risks = [...(center.risks || [])];
    if (location.protocol === 'http:' && !['localhost','127.0.0.1','[::1]'].includes(location.hostname)) {
      risks.push({id:'panel:http', category:'host', level:'warning', title:'当前通过 HTTP 访问面板', target:location.host,
        detail:'浏览器与面板之间未使用 HTTPS，登录凭据与运维内容可能明文传输。', advice:'可以使用 HTTPS 入口或 SSH 本地隧道；HTTP 访问继续可用。'});
    }
    const authFresh = !readError && activity.auth?.readable && Date.now() / 1000 - activity.auth.checked_at <= 180;
    if (authFresh) for (const item of activity.concentrated_failures || []) risks.push({id:'login:' + item.ip, category:'login', level:'warning', title:'同一来源连续登录失败', target:item.ip,
      detail:`最近 10 分钟采集到 ${item.attempts} 次 SSH 认证失败。`, advice:'核对是否为自己的连接；需要封禁时先选择防护规则，不自动认定为攻击。'});
    const critical = risks.filter(item => item.level === 'critical').length;
    $('ipSecurityTitle').textContent = !fresh ? center.checked_at ? '安全检查待更新' : '正在读取安全状态' : risks.length ? `${risks.length} 项配置与活动需要核对` : unknown ? `${unknown} 项检查尚待核实` : '当前检查范围内未发现风险';
    $('ipSecurityTitle').closest('.security-heading').dataset.state = !fresh ? 'unknown' : critical ? 'critical' : risks.length ? 'warning' : unknown ? 'unknown' : 'known';
    $('securitySummaryNote').textContent = readError ? `读取失败：${readError}。保留上次结果，稍后自动重试。` : center.checked_at ? `最近检查 ${date(center.checked_at)}${status.scanning ? ' · 正在重新检查' : ''}${unknown ? ` · ${unknown} 项检查未完成` : ''} · 不代表完整安全评估` : '首次检查正在后台进行，不会修改服务器设置';
    $('securityRiskCount').textContent = fresh ? risks.length : '—';
    $('securityRiskHint').textContent = fresh ? `${critical} 项高风险${center.limited ? ' · 结果达到上限' : ''}` : '检查未完成或已过期';
    $('securityLoginCount').textContent = activity.auth?.checked_at ? activity.login_failure_24h ?? 0 : '—';
    $('securityBanCount').textContent = activity.bans_24h ?? '—';
    $('securityChangeCount').textContent = center.checked_at ? activity.config_changes_24h ?? 0 : '—';
    const opened = new Set([...$('securityRisks').querySelectorAll('details[open]')].map(el => el.dataset.risk));
    const category = $('securityRiskFilter').value;
    const filtered = risks.filter(item => category === 'all' || item.category === category);
    $('securityRisks').innerHTML = filtered.length ? filtered.map(item => `<details class="security-risk" data-risk="${text(item.id)}" data-level="${item.level === 'critical' ? 'critical' : 'warning'}" ${opened.has(item.id) ? 'open' : ''}><summary><span class="security-risk-dot" aria-hidden="true"></span><div><strong>${text(item.title)}</strong><span>${text(item.target)}</span></div><span class="security-risk-level">${item.level === 'critical' ? '高风险' : '待核对'}</span></summary><div class="security-risk-evidence"><p>${text(item.detail)}</p><p>${text(item.advice)}</p>${item.category === 'login' ? '<button type="button" class="ghost-button compact-action" data-open-protection>查看登录防护</button>' : ''}</div></details>`).join('') : `<p class="security-empty">${center.checked_at ? '当前筛选下没有已发现的风险；请结合采集状态判断。' : '正在等待主机检查，尚不能判断风险。'}</p>`;
    const docker = (center.checks || []).find(item => item.name === 'Docker 权限');
    const sources = [
      ['监听端口', fresh && facts.host_readable && facts.docker_readable ? 'known' : 'unknown', '读取主机与 Docker 已发布端口'],
      ['SSH 登录', authFresh ? 'known' : 'unknown', activity.auth?.detail || '等待日志来源'],
      ['容器权限', fresh ? docker?.state || 'unknown' : 'unknown', docker?.detail || '等待容器检查'],
      ['配置变化', fresh ? (center.checks || []).find(item => item.name === '配置变化')?.state || 'unknown' : 'unknown', (center.checks || []).find(item => item.name === '配置变化')?.detail || '等待配置基线'],
      ['登录防护', readError ? 'unknown' : status.running && (status.panel_enabled || status.ssh_enabled || status.existing_ssh) ? 'known' : 'inactive', `${status.jails?.length || 0} 条活跃规则；实际阻断需验证`]
    ];
    $('securityCoverage').innerHTML = sources.map(([name,state,detail]) => `<div class="security-source" data-state="${state}"><span class="security-source-dot" aria-hidden="true"></span><strong>${text(name)}</strong><span title="${text(detail)}">${state === 'known' ? name === '登录防护' ? '运行中' : '已读取' : state === 'not_installed' ? '未安装' : state === 'inactive' ? '未启用' : '待核实'}</span><small>${text(detail)}</small></div>`).join('');
    $('securityCoverageNote').textContent = facts.scan_error || (center.checks || []).some(item => item.state === 'unknown') ? '部分检查未完成，展开检查明细查看原因。' : center.notice || '等待后台检查';
    const labels = {login_success:'登录成功',login_failure:'登录失败',config_change:'配置变化',ban:'封禁',unban:'解封',restore:'恢复封禁'};
    const sourceLabels = {ssh:'SSH',config:'配置文件',fail2ban:'Fail2ban'};
    const expandedEvents = new Set([...$('securityActivityList').querySelectorAll('details[open]')].map(el => el.dataset.event));
    $('securityActivityNote').textContent = `已采集 SSH 成功 ${activity.login_success_24h || 0} 次、失败 ${activity.login_failure_24h || 0} 次（24 小时）${activity.auth?.limited ? ' · 日志存在采集空档或达到上限' : ''}`;
    $('securityActivityList').innerHTML = (activity.items || []).length ? activity.items.map(item => `<details class="security-event" data-event="${text(item.id)}" data-kind="${text(item.kind)}" ${expandedEvents.has(item.id) ? 'open' : ''}><summary><time>${text(date(item.ts))}</time><span class="security-event-kind">${labels[item.kind] || '记录'}</span><strong>${text(item.ip || item.target)}</strong><span class="security-event-account">${text(item.account || sourceLabels[item.source] || '')}</span></summary><div class="security-event-detail"><span>${text(item.target)}</span><p>${text(item.detail)}</p></div></details>`).join('') : `<p class="security-empty">暂无已采集的安全事件。${text(activity.auth?.detail || '等待后台采集')} 不代表历史上没有活动。</p>`;
    $('securityActivityRetention').textContent = activity.notice || '仅显示已采集事件，停机或日志轮转可能留下空档。';
    const total = activity.total || 0, pageSize = activity.page_size || 50;
    const start = (activity.items || []).length ? activityPage * pageSize + 1 : 0;
    const end = start ? start + activity.items.length - 1 : 0;
    $('securityActivityPage').textContent = `第 ${activityPage + 1} / ${Math.max(1, Math.ceil(total / pageSize))} 页 · ${start}–${end} 条，共 ${total} 条`;
  }
  async function refresh(scan = false) {
    if (loading || busy || !view) return;
    loading = true; controls();
    try {
      status = await api(null, false, scan === true); readError = ''; render();
      if (status.detail) feedback(status.detail);
      if (status.job?.state === 'running') { busy = true; poll(); }
    } catch (error) {
      readError = error.message;
      activityPage = status.activity?.page || 0; historyPage = status.history?.page || 0;
      if (status.activity?.source) $('securityActivitySource').value = status.activity.source;
      renderCenter(); window.AppSelects?.syncAll(); feedback(error.message);
    }
    finally { loading = false; controls(); if (!busy) schedule(); }
  }
  function schedule() { clearTimeout(timer); if (view && !document.hidden) timer = setTimeout(() => refresh(), 15000); }
  async function poll() {
    clearTimeout(timer);
    if (!view || document.hidden) return;
    try {
      const result = await api(null, true), job = result.job;
      if (job?.state === 'running') { feedback(job.stage + '…'); timer = setTimeout(poll, 1500); return; }
      busy = false; feedback(job?.message || '操作已结束');
      if (job?.state === 'succeeded' && job.action?.startsWith('configure')) delete drafts[job.action === 'configure_ssh' ? 'ssh' : 'panel'];
      await refresh();
    } catch (error) {
      feedback(`状态暂不可读：${error.message}。操作可能仍在执行，正在重新读取结果。`);
      if (view && !document.hidden) timer = setTimeout(poll, 5000);
    }
    finally { controls(); }
  }
  async function operate(action, ip = '') {
    if (busy || loading || readError || status.read_only) return;
    const jail = $('ipSecurityJail').value;
    const configuring = action.startsWith('configure'), disabling = action.startsWith('disable');
    const ssh = status.observation?.ssh || {};
    if (configuring) {
      $('ipSecuritySettingsTarget').value = action === 'configure_ssh' ? 'ssh' : 'panel';
      renderSettings(); window.AppSelects?.syncAll();
    }
    const confirmed = await showConfirmDialog({title:configuring ? action === 'configure_ssh' ? '配置 SSH 防护' : '配置面板防护' : disabling ? '暂停专属防护' : action === 'ban' ? '封禁 IP' : '解封 IP',
      message: configuring ? `${action === 'configure_ssh' ? `保护本机 SSH 端口 ${ssh.ports?.join('、')}，日志来源 ${ssh.backend === 'systemd' ? 'systemd' : ssh.logpath}。` : '保护面板及设置中选定的入口。'}使用下方防护设置，当前访问 IP 自动加入白名单；安装或使用 Fail2ban，备份专属配置，不修改 SSH 登录设置。` : disabling ? '暂停所选 mini_deploy 专属规则，其封禁可能随规则停止而解除。配置与备份保留，其他规则保持运行。' : `${action === 'ban' ? '封禁' : '解封'} ${ip}，规则：${jail}。封禁范围和时长由该规则决定，其他规则的封禁不受影响。`,
      confirmText:configuring ? '启用并保存' : disabling ? '确认暂停' : action === 'ban' ? '确认封禁' : '确认解封', danger:action === 'ban' || disabling});
    if (!confirmed) return;
    busy = true; controls(); feedback('正在提交防护操作…');
    try {
      const data = {action, jail, ip, confirmed:true};
      if (configuring) Object.assign(data, {maxretry:Number($('ipSecurityMaxretry').value),
        findtime:Number($('ipSecurityFindtime').value) * 60, bantime:Number($('ipSecurityBantime').value) * 60,
        ignoreip:$('ipSecurityIgnore').value.split(/\s+/).filter(Boolean), ports:$('ipSecurityPorts').value === 'web' ? [6868,80,443] : [6868],
        increment:$('ipSecurityIncrement').checked, expected_ssh:ssh.fingerprint});
      await api(data); if (disabling) $('ipSecurityJail').value = ''; await poll();
    } catch (error) {
      if (!error.status || error.status >= 500) {
        feedback(`提交结果暂未确认：${error.message}。正在查询执行状态，请勿重复提交。`);
        clearTimeout(timer);
        if (view && !document.hidden) timer = setTimeout(poll, 1000);
      } else {
        busy = false; feedback(error.message); schedule();
      }
      controls();
    }
  }
  $('ipSecurityRefresh').onclick = () => refresh(true);
  $('securityRiskFilter').onchange = () => { renderCenter(); window.AppSelects?.syncAll(); };
  $('securityActivitySource').onchange = () => { activityPage = 0; refresh(); };
  $('securityActivityPrev').onclick = () => { activityPage = Math.max(0, activityPage - 1); refresh(); };
  $('securityActivityNext').onclick = () => { activityPage += 1; refresh(); };
  $('securityRisks').onclick = event => { if (event.target.closest('[data-open-protection]')) { $('securityProtection').open = true; $('securityProtection').scrollIntoView({behavior:'smooth',block:'start'}); } };
  $('ipSecuritySetup').onclick = () => operate('configure');
  $('ipSecurityDisable').onclick = () => operate('disable');
  $('ipSecuritySSHSetup').onclick = () => operate('configure_ssh');
  $('ipSecuritySSHDisable').onclick = () => operate('disable_ssh');
  $('ipSecurityHistoryPrev').onclick = () => { historyPage = Math.max(0, historyPage - 1); refresh(); };
  $('ipSecurityHistoryNext').onclick = () => { historyPage += 1; refresh(); };
  $('ipSecurityJail').onchange = refresh;
  $('ipSecurityBanForm').onsubmit = event => { event.preventDefault(); operate('ban', $('ipSecurityAddress').value.trim()); };
  $('ipSecurityBanned').onclick = event => { const button = event.target.closest('[data-unban-ip]'); if (button && !button.disabled) operate('unban', button.dataset.unbanIp); };
  $('ipSecuritySettingsTarget').onchange = () => { renderSettings(); window.AppSelects?.syncAll(); };
  document.querySelectorAll('.ip-security-settings input, .ip-security-settings textarea, #ipSecurityPorts').forEach(input => input.addEventListener('input', () => { drafts[$('ipSecuritySettingsTarget').value] = readSettings(); }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else if (view) { if (busy) poll(); else refresh(); } });
  window.IPSecurity = {viewChanged(name) { view = name === 'security'; clearTimeout(timer); if (view) { if (busy) poll(); else refresh(!status.observation?.checked_at); } }};
  if (activeView === 'security') window.IPSecurity.viewChanged('security');
})();
