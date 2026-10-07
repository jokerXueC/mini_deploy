const $ = (id) => document.getElementById(id);
let lastConfig = null;
let activeView = 'server';
let csrfToken = '';
let serverRefreshSeconds = 30;
let serverRefreshTimer = null;
let eventsRefreshTimer = null;
let systemStatusRequestId = 0;
let currentContainerLogName = '';
let currentContainerLogLineLimit = 200;
let containerLogKeywordTimer = null;
let systemTrendRange = 'realtime';
let notificationDirty = false;
const THEME_STORAGE_KEY = 'mini_deploy-theme';
const DISMISSED_NOTICE_STORAGE_KEY = 'mini_deploy-dismissed-notices';
let lastAlerts = [];
let lastEvents = [];
let lastContainerNoticeKeys = [];
let lastSystemPayload = null;
let dismissedNoticeKeys = loadDismissedNoticeKeys();
let trendTooltipEl = null;
const THEME_DAY_START_HOUR = 7;
const THEME_DAY_END_HOUR = 19;

function timeBasedTheme() {
  const hour = new Date().getHours();
  return hour >= THEME_DAY_START_HOUR && hour < THEME_DAY_END_HOUR ? 'light' : 'dark';
}

function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function ensureInitialTheme() {
  const theme = document.documentElement.dataset.theme;
  if (theme === 'light' || theme === 'dark') return;
  document.documentElement.dataset.theme = timeBasedTheme();
}

function updateThemeToggle() {
  const theme = currentTheme();
  const button = $('themeToggleBtn');
  const text = $('themeToggleText');
  if (button) {
    button.setAttribute('aria-pressed', theme === 'light' ? 'true' : 'false');
    button.setAttribute('aria-label', theme === 'light' ? '切换深色模式' : '切换浅色模式');
  }
  if (text) text.textContent = theme === 'light' ? '深色' : '浅色';
}

function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch (_) {}
  updateThemeToggle();
}

function fallbackThemeReveal() {
  const reveal = document.createElement('span');
  reveal.className = 'theme-fallback-reveal';
  document.body.appendChild(reveal);
  reveal.addEventListener('animationend', () => reveal.remove(), { once: true });
}

function toggleTheme() {
  const next = currentTheme() === 'light' ? 'dark' : 'light';
  const revealClass = next === 'light' ? 'theme-reveal-light' : 'theme-reveal-dark';
  if (document.startViewTransition) {
    document.documentElement.classList.add(revealClass);
    const transition = document.startViewTransition(() => applyTheme(next));
    transition.finished.finally(() => {
      document.documentElement.classList.remove('theme-reveal-light', 'theme-reveal-dark');
    });
    return;
  }
  applyTheme(next);
  fallbackThemeReveal();
}

ensureInitialTheme();
updateThemeToggle();

function positiveLineCount(value, fallback = 200) {
  const parsed = Number.parseInt(String(value ?? '').trim(), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function syncCustomLineInput(selectId, inputId, currentValue = 200) {
  const select = $(selectId);
  const input = $(inputId);
  if (!select || !input) return;
  const isCustom = select.value === 'custom';
  input.hidden = !isCustom;
  if (isCustom && !positiveLineCount(input.value, 0)) {
    input.value = String(positiveLineCount(currentValue, 200));
  }
}

function selectedLineCount(selectId, inputId, fallback = 200) {
  const select = $(selectId);
  if (!select) return positiveLineCount(fallback, 200);
  if (select.value === 'custom') {
    return positiveLineCount($(inputId)?.value, fallback);
  }
  return positiveLineCount(select.value, fallback);
}

function selectedDownloadLines(selectId, inputId, currentValue = 200) {
  const select = $(selectId);
  const value = select ? select.value : 'current';
  if (value === 'all') return 'all';
  if (value === 'current') return positiveLineCount(currentValue, 200);
  if (value === 'custom') return selectedLineCount(selectId, inputId, currentValue);
  return positiveLineCount(value, currentValue);
}

function closeLogSelectMenus(except = null) {
  document.querySelectorAll('.log-select').forEach(root => {
    if (root === except) return;
    root.classList.remove('open');
    const button = root.querySelector('.log-select-button');
    const menu = root.querySelector('.log-select-menu');
    if (button) button.setAttribute('aria-expanded', 'false');
    if (menu) menu.hidden = true;
  });
}

function enhanceLogSelect(selectId) {
  const select = $(selectId);
  if (!select || select.dataset.enhanced === '1') return;
  select.dataset.enhanced = '1';

  const root = document.createElement('div');
  root.className = 'log-select';
  root.dataset.selectFor = selectId;

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'log-select-button';
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = '<span></span><span class="select-caret" aria-hidden="true"></span>';

  const menu = document.createElement('div');
  menu.className = 'log-select-menu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;

  function syncLabel() {
    const selected = select.options[select.selectedIndex];
    button.querySelector('span').textContent = selected ? selected.textContent : select.value;
    menu.querySelectorAll('.log-select-option').forEach(option => {
      const active = option.dataset.value === select.value;
      option.classList.toggle('active', active);
      option.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  Array.from(select.options).forEach(item => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'log-select-option';
    option.setAttribute('role', 'option');
    option.dataset.value = item.value;
    option.textContent = item.textContent;
    option.addEventListener('click', () => {
      select.value = item.value;
      syncLabel();
      closeLogSelectMenus();
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    menu.appendChild(option);
  });

  button.addEventListener('click', event => {
    event.stopPropagation();
    const willOpen = menu.hidden;
    closeLogSelectMenus(root);
    root.classList.toggle('open', willOpen);
    menu.hidden = !willOpen;
    button.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  });
  root.addEventListener('click', event => event.stopPropagation());
  select.addEventListener('change', syncLabel);

  root.appendChild(button);
  root.appendChild(menu);
  select.insertAdjacentElement('afterend', root);
  syncLabel();
}

function containerLogFilterOptions() {
  const level = $('containerLogFilterSelect')?.value || 'all';
  const keyword = ($('containerLogKeywordInput')?.value || '').trim();
  const regex = $('containerLogRegexInput')?.checked ? '1' : '';
  const context = positiveLineCount($('containerLogContextSelect')?.value, 0);
  if (level === 'keyword' && !keyword) {
    return { level: 'all', keyword: '', regex: '', context };
  }
  return {
    level,
    keyword: level === 'keyword' ? keyword : '',
    regex: level === 'keyword' && keyword ? regex : '',
    context,
  };
}

function containerLogFilterLabel(options) {
  if (!options || options.level === 'all') return '';
  if (options.level === 'error') return ' · 异常';
  if (options.level === 'warn') return ' · 警告';
  if (options.level === 'keyword' && options.keyword) {
    return options.regex ? ` · 正则 ${options.keyword}` : ` · 关键词 ${options.keyword}`;
  }
  return '';
}

function syncContainerLogFilterControls() {
  const isKeyword = ($('containerLogFilterSelect')?.value || 'all') === 'keyword';
  const keywordInput = $('containerLogKeywordInput');
  const regexControl = $('containerLogRegexControl');
  if (keywordInput) keywordInput.hidden = !isKeyword;
  if (regexControl) regexControl.hidden = !isKeyword;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[ch]));
}

function loadDismissedNoticeKeys() {
  try {
    const raw = localStorage.getItem(DISMISSED_NOTICE_STORAGE_KEY);
    const keys = JSON.parse(raw || '[]');
    return new Set(Array.isArray(keys) ? keys.filter(Boolean).slice(-400) : []);
  } catch (_) {
    return new Set();
  }
}

function saveDismissedNoticeKeys() {
  try {
    const keys = Array.from(dismissedNoticeKeys).slice(-400);
    dismissedNoticeKeys = new Set(keys);
    localStorage.setItem(DISMISSED_NOTICE_STORAGE_KEY, JSON.stringify(keys));
  } catch (_) {}
}

function noticeSignature(type, item = {}) {
  const kind = String(item.kind || '').trim();
  const parts = [
    type,
    kind,
    item.level || '',
    item.title || '',
    item.source || '',
    item.command || '',
  ].map(value => String(value ?? '').trim());
  if (type === 'event' && !['alert', 'docker'].includes(kind)) {
    parts.push(String(item.detail || '').trim(), String(item.at || '').trim());
  }
  return parts.join('|');
}

function containerDisplayName(container = {}) {
  return String(container.name || container.id || '').trim();
}

function containerNoticeEntries(container = {}) {
  const name = containerDisplayName(container);
  if (!name) return [];
  const state = String(container.state || '').toLowerCase();
  const health = String(container.health || '').toLowerCase();
  const errorCount = Number(container.recent_error_count || 0);
  const warnCount = Number(container.recent_warn_count || 0);
  const entries = [];
  if (state && state !== 'running') {
    entries.push({
      category: 'state',
      type: 'alert',
      item: { level: 'critical', title: `容器未运行：${name}`, source: 'docker', command: `docker start ${name}` },
    });
    entries.push({
      category: 'state',
      type: 'event',
      item: { kind: 'docker', level: 'critical', title: `${name} 未运行`, source: 'docker' },
    });
  } else if (health === 'unhealthy') {
    entries.push({
      category: 'health',
      type: 'alert',
      item: { level: 'critical', title: `容器健康检查失败：${name}`, source: 'docker', command: `docker logs --tail=200 ${name}` },
    });
  }
  if (errorCount > 0) {
    entries.push({
      category: 'error',
      type: 'alert',
      item: { level: 'warning', title: `容器近期异常日志：${name}`, source: 'docker', command: `docker logs --tail=200 ${name}` },
    });
    entries.push({
      category: 'error',
      type: 'event',
      item: { kind: 'docker', level: 'warning', title: `${name} 出现异常日志`, source: 'docker' },
    });
  }
  if (warnCount > 0) {
    entries.push({
      category: 'warn',
      type: 'container',
      item: { kind: 'docker', level: 'warning', title: `容器近期警告日志：${name}`, source: 'docker', command: `docker logs --tail=200 ${name}` },
    });
  }
  return entries;
}

function containerNoticeKeys(container = {}, category = null) {
  return containerNoticeEntries(container)
    .filter(entry => !category || entry.category === category)
    .map(entry => noticeSignature(entry.type, entry.item))
    .filter(Boolean);
}

function areNoticeKeysDismissed(keys = []) {
  return keys.length > 0 && keys.every(key => dismissedNoticeKeys.has(key));
}

function visibleNoticeKeys(keys = []) {
  return keys.filter(key => !dismissedNoticeKeys.has(key));
}

function isAbnormalEvent(event = {}) {
  const level = String(event?.level || '').toLowerCase();
  return Boolean(level) && !['success', 'ok', 'completed'].includes(level);
}

function currentNoticeKeys() {
  const alertKeys = lastAlerts.map(alert => noticeSignature('alert', alert));
  const eventKeys = lastEvents
    .filter(isAbnormalEvent)
    .map(event => noticeSignature('event', event));
  return Array.from(new Set([...alertKeys, ...eventKeys, ...lastContainerNoticeKeys])).filter(Boolean);
}

function updateClearAlertsButton() {
  const button = $('clearAlertsBtn');
  if (!button) return;
  const keys = currentNoticeKeys();
  const visibleCount = keys.filter(key => !dismissedNoticeKeys.has(key)).length;
  const dismissedCount = keys.length - visibleCount;
  button.disabled = keys.length === 0;
  button.textContent = visibleCount > 0 ? '清除当前' : dismissedCount > 0 ? '恢复显示' : '清除当前';
  button.title = visibleCount > 0
    ? '清除当前已经看到的警告和异常，新的异常仍会显示'
    : dismissedCount > 0
      ? '恢复显示当前这批已清除的告警'
      : '暂无可清除的告警';
}

function toggleCurrentNoticeDismissal() {
  const keys = currentNoticeKeys();
  if (!keys.length) return;
  const visibleKeys = keys.filter(key => !dismissedNoticeKeys.has(key));
  if (visibleKeys.length) {
    visibleKeys.forEach(key => dismissedNoticeKeys.add(key));
  } else {
    keys.forEach(key => dismissedNoticeKeys.delete(key));
  }
  saveDismissedNoticeKeys();
  renderAlerts(lastAlerts);
  renderEvents(lastEvents);
  if (lastSystemPayload) renderSystemStatus(lastSystemPayload);
}

let confirmDialogResolve = null;

function closeConfirmDialog(result = false) {
  const modal = $('confirmModal');
  if (!modal || modal.hidden) return false;
  modal.hidden = true;
  const resolve = confirmDialogResolve;
  confirmDialogResolve = null;
  if (resolve) resolve(Boolean(result));
  return true;
}

function showConfirmDialog({
  title = '确认操作',
  message = '此操作需要确认。',
  confirmText = '确认',
  cancelText = '取消',
  danger = true,
} = {}) {
  const modal = $('confirmModal');
  const titleEl = $('confirmTitle');
  const messageEl = $('confirmMessage');
  const okBtn = $('confirmOkBtn');
  const cancelBtn = $('confirmCancelBtn');
  if (!modal || !titleEl || !messageEl || !okBtn || !cancelBtn) return Promise.resolve(false);

  if (confirmDialogResolve) closeConfirmDialog(false);
  titleEl.textContent = title;
  messageEl.textContent = message;
  okBtn.textContent = confirmText;
  cancelBtn.textContent = cancelText;
  okBtn.className = `${danger ? 'danger-button' : 'ghost-button'} compact-action`;
  modal.hidden = false;
  confirmDialogResolve = null;
  return new Promise(resolve => {
    confirmDialogResolve = resolve;
    window.setTimeout(() => okBtn.focus(), 0);
  });
}

function formatPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n * 10) / 10}%` : '-';
}

function formatNumber(value, suffix = '') {
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n * 10) / 10}${suffix}` : '-';
}

function resourceLevel(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  if (n >= 90) return 'bad';
  if (n >= 75) return 'warn';
  return '';
}

function resourceBars(value, count = 60) {
  return Array.from({ length: count }, (_, index) => `
    <span class="resource-bar empty" style="--bar-index:${index}"></span>
  `).join('');
}

function measuredTrackWidth(el) {
  const rectWidth = el.getBoundingClientRect().width;
  const ownWidth = Math.max(Number(rectWidth) || 0, Number(el.clientWidth) || 0, Number(el.offsetWidth) || 0);
  if (ownWidth >= 48) return ownWidth;
  if (el.classList.contains('meter-track')) {
    const lineWidth = Number(el.closest('.meter-line')?.getBoundingClientRect().width || 0);
    const estimated = lineWidth - 34 - 42 - 16;
    if (estimated >= 48) return estimated;
  }
  const parentWidth = Number(el.parentElement?.getBoundingClientRect().width || 0);
  return Math.max(ownWidth, parentWidth);
}

function resourceBarCount() {
  return 60;
}

function updateResourceTrack(el, value) {
  if (!el) return;
  const count = resourceBarCount();
  el.dataset.resourceValue = value ?? '';
  if (el.children.length !== count) {
    DashboardMotion.cancel(el);
    el.innerHTML = resourceBars(null, count);
  }
  const n = value == null || value === '' ? NaN : Number(value);
  const valid = Number.isFinite(n);
  if (!valid) DashboardMotion.cancel(el);
  const percent = valid ? Math.max(0, Math.min(100, n)) : 0;
  el.dataset.level = resourceLevel(percent);
  el.setAttribute('role', 'meter');
  el.setAttribute('aria-valuemin', '0');
  el.setAttribute('aria-valuemax', '100');
  el.setAttribute('aria-label', el.id || el.closest('.meter-line')?.firstElementChild?.textContent || '资源占用');
  if (valid) el.setAttribute('aria-valuenow', String(percent));
  else el.removeAttribute('aria-valuenow');
  el.setAttribute('aria-valuetext', valid ? formatPercent(percent) : '暂无数据');
  const bars = Array.from(el.children);
  DashboardMotion.value(el, percent, current => {
    el.dataset.displayValue = String(current);
    bars.forEach((bar, index) => {
      const fill = Math.max(0, Math.min(1, current / 100 * count - index));
      bar.style.setProperty('--fill', fill.toFixed(3));
    });
    const label = el.closest('.meter-line')?.querySelector('strong');
    if (label) label.textContent = valid ? formatPercent(current) : '-';
  }, {initial: 0});
}

function setResourceBar(id, value) {
  updateResourceTrack($(id), value);
}

function animateNumberText(id, value, formatter) {
  const el = $(id);
  if (!el) return;
  const next = value == null || value === '' ? NaN : Number(value);
  if (!Number.isFinite(next)) {
    DashboardMotion.cancel(el);
    el.dataset.value = '';
    el.textContent = '-';
    return;
  }
  el.dataset.value = String(next);
  DashboardMotion.value(el, next, current => {
    el.textContent = formatter(current);
  }, {initial: 0});
}

function setView(view) {
  activeView = ['server', 'events', 'notify', 'certificates', 'requests'].includes(view) ? view : 'server';
  ['server', 'events', 'notify', 'certificates', 'requests'].forEach(name => {
    $(`${name}View`)?.classList.toggle('active', activeView === name);
    $(`${name}ViewTab`)?.classList.toggle('active', activeView === name);
  });
  const titles = {
    server: ['服务器健康面板', '查看服务器资源、网络吞吐和 Docker 容器运行状态。'],
    events: ['监测与告警', '网站可用性、线上证书与服务器异常。'],
    notify: ['通知配置', '异常提醒与恢复通知。'],
    certificates: ['域名与证书', '已发现的网站、证书有效期与线上状态。'],
    requests: ['请求记录', '网站访问、响应状态与耗时'],
  };
  const [title, subtitle] = titles[activeView];
  $('panelTitle').textContent = title;
  $('panelSubtitle').textContent = subtitle;
  clearServerRefresh();
  clearEventsRefresh();
  if (activeView === 'server') {
    refreshServerStatus();
    window.DockerImages?.refresh();
  }
  else if (activeView === 'events') refreshEventsStatus();
  else if (activeView === 'notify') refreshNotificationConfig();
  else if (activeView === 'certificates') refreshCertificates();
  else if (activeView === 'requests') window.NginxRequests?.refresh();
  window.WebsiteMonitoring?.refresh();
}


async function fetchJson(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  if (res.status === 401) location.reload();
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `${path} ${res.status}`);
  }
  return res.json();
}

async function postJson(path) {
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrfToken },
  });
  if (res.status === 401) location.reload();
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `${path} ${res.status}`);
  return data;
}

async function postJsonBody(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(payload || {}),
  });
  if (res.status === 401) location.reload();
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.detail || data.error || `${path} ${res.status}`);
    throw error;
  }
  return data;
}

function severityClass(level) {
  if (level === 'critical' || level === 'failed') return 'failed';
  if (level === 'warning' || level === 'running') return 'running';
  if (level === 'success' || level === 'ok') return 'success';
  return 'neutral';
}

async function copyText(value) {
  const text = String(value || '');
  if (!text || text === '-') return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  document.execCommand('copy');
  input.remove();
}

function defaultNotificationConfig() {
  return {
    wecom: { enabled: false, webhook_url: '' },
    dingtalk: { enabled: false, webhook_url: '', secret: '' },
    email: {
      enabled: false,
      smtp_host: '',
      smtp_port: 465,
      username: '',
      password: '',
      from_addr: '',
      to_addrs: '',
      use_ssl: true,
      use_starttls: false,
    },
  };
}

function mergedNotificationConfig(config = {}) {
  const defaults = defaultNotificationConfig();
  return {
    wecom: { ...defaults.wecom, ...(config.wecom || {}) },
    dingtalk: { ...defaults.dingtalk, ...(config.dingtalk || {}) },
    email: { ...defaults.email, ...(config.email || {}) },
  };
}

function setInputValue(id, value) {
  const input = $(id);
  if (input) input.value = value == null ? '' : String(value);
}

function setInputChecked(id, value) {
  const input = $(id);
  if (input) input.checked = Boolean(value);
}

function countEnabledNotifications(config = {}) {
  const parsed = mergedNotificationConfig(config);
  return ['wecom', 'dingtalk', 'email'].filter(key => Boolean(parsed[key]?.enabled)).length;
}

function syncNotificationBadge(config = collectNotificationConfig()) {
  const badge = $('notificationBadge');
  if (!badge) return;
  const count = countEnabledNotifications(config);
  badge.className = `badge ${count ? 'success' : 'neutral'}`;
  badge.textContent = count ? `${count} 个渠道` : '未启用';
}

function renderNotificationConfig(config = {}) {
  if (notificationDirty) return;
  const parsed = mergedNotificationConfig(config);
  setInputChecked('notifyWecomEnabled', parsed.wecom.enabled);
  setInputValue('notifyWecomWebhook', parsed.wecom.webhook_url);
  setInputChecked('notifyDingtalkEnabled', parsed.dingtalk.enabled);
  setInputValue('notifyDingtalkWebhook', parsed.dingtalk.webhook_url);
  setInputValue('notifyDingtalkSecret', parsed.dingtalk.secret);
  setInputChecked('notifyEmailEnabled', parsed.email.enabled);
  setInputValue('notifyEmailHost', parsed.email.smtp_host);
  setInputValue('notifyEmailPort', parsed.email.smtp_port || 465);
  setInputValue('notifyEmailUsername', parsed.email.username);
  setInputValue('notifyEmailPassword', parsed.email.password);
  setInputValue('notifyEmailFrom', parsed.email.from_addr);
  setInputValue('notifyEmailTo', parsed.email.to_addrs);
  setInputChecked('notifyEmailSsl', parsed.email.use_ssl !== false);
  setInputChecked('notifyEmailStarttls', Boolean(parsed.email.use_starttls));
  syncNotificationBadge(parsed);
}

function collectNotificationConfig() {
  return {
    wecom: {
      enabled: $('notifyWecomEnabled')?.checked || false,
      webhook_url: $('notifyWecomWebhook')?.value || '',
    },
    dingtalk: {
      enabled: $('notifyDingtalkEnabled')?.checked || false,
      webhook_url: $('notifyDingtalkWebhook')?.value || '',
      secret: $('notifyDingtalkSecret')?.value || '',
    },
    email: {
      enabled: $('notifyEmailEnabled')?.checked || false,
      smtp_host: $('notifyEmailHost')?.value || '',
      smtp_port: Number($('notifyEmailPort')?.value || 465),
      username: $('notifyEmailUsername')?.value || '',
      password: $('notifyEmailPassword')?.value || '',
      from_addr: $('notifyEmailFrom')?.value || '',
      to_addrs: $('notifyEmailTo')?.value || '',
      use_ssl: $('notifyEmailSsl')?.checked !== false,
      use_starttls: $('notifyEmailStarttls')?.checked || false,
    },
  };
}

function renderNotificationResults(results = [], fallback = '') {
  const target = $('notificationResult');
  if (!target) return;
  if (!Array.isArray(results) || !results.length) {
    target.innerHTML = escapeHtml(fallback || '暂无通知结果。');
    return;
  }
  target.innerHTML = results.map(item => {
    const label = item.channel === 'wecom' ? '企业微信' : item.channel === 'dingtalk' ? '钉钉' : item.channel === 'email' ? '邮箱' : item.channel;
    const cls = !item.enabled ? 'neutral' : item.ok ? 'success' : 'failed';
    return `
      <div class="notification-result-item ${cls}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(!item.enabled ? '未启用' : item.ok ? '成功' : '失败')}</strong>
        <small>${escapeHtml(item.detail || '-')}</small>
      </div>
    `;
  }).join('');
}

async function saveNotificationConfig() {
  const target = $('notificationResult');
  if (target) target.textContent = '正在保存通知配置...';
  try {
    lastConfig = await postJsonBody('notifications', { notifications: collectNotificationConfig() });
    notificationDirty = false;
    renderNotificationConfig(lastConfig.notifications || {});
    renderNotificationResults([], '通知配置已保存。');
  } catch (err) {
    renderNotificationResults([], `保存失败: ${err.message}`);
  }
}

async function testNotificationConfig() {
  const target = $('notificationResult');
  if (target) target.textContent = '正在发送测试通知...';
  try {
    const result = await postJsonBody('notifications/test', { notifications: collectNotificationConfig() });
    renderNotificationResults(result.results || [], result.ok ? '测试通知已发送。' : '测试通知失败。');
  } catch (err) {
    renderNotificationResults([], `测试失败: ${err.message}`);
  }
}

function updateLiveIndicator(online) {
  const el = $('liveIndicator');
  el.className = `live-indicator ${online ? 'live-ok' : 'live-error'}`;
  el.lastChild.textContent = online ? 'Agent 在线' : 'Agent 连接失败';
}

function containerStateClass(container) {
  const state = String(container.state || '').toLowerCase();
  const health = String(container.health || '').toLowerCase();
  if (health === 'unhealthy' || state === 'exited' || state === 'dead') return 'failed';
  if (state === 'restarting' || health === 'starting') return 'running';
  if (state === 'running') return 'success';
  return 'neutral';
}

function containerStateLabel(container) {
  const state = container.state || 'unknown';
  return container.health ? `${state} · ${container.health}` : state;
}

function containerActions(container) {
  const state = String(container.state || '').toLowerCase();
  const name = container.name || container.id || '';
  const errorDismissed = areNoticeKeysDismissed(containerNoticeKeys(container, 'error'));
  const visibleKeys = visibleNoticeKeys(containerNoticeKeys(container));
  const actions = [{ action: 'logs', label: '日志' }];
  if (Number(container.recent_error_count || 0) > 0 && !errorDismissed) {
    actions.push({ action: 'logs-error', label: `异常 ${container.recent_error_count}`, danger: true });
  }
  if (visibleKeys.length) {
    actions.push({
      action: 'dismiss-notices',
      label: '清除异常',
      noticeKeys: visibleKeys,
    });
  }
  if (state === 'running') {
    actions.push(
      { action: 'restart', label: '重启', danger: true },
      { action: 'stop', label: '停止', danger: true },
      { action: 'pause', label: '暂停' },
    );
  } else if (state === 'paused') {
    actions.push(
      { action: 'unpause', label: '恢复' },
      { action: 'stop', label: '停止', danger: true },
    );
  } else {
    actions.push({ action: 'start', label: '启动' });
  }
  if (['exited', 'created', 'dead'].includes(state)) {
    actions.push({ action: 'remove', label: '删除', danger: true });
  }
  return actions.map(item => `
    <button
      class="ghost-button container-action ${item.danger ? 'danger-text' : ''}"
      type="button"
      data-container-action="${escapeHtml(item.action)}"
      data-container-name="${escapeHtml(name)}"
      ${item.noticeKeys ? `data-notice-keys="${escapeHtml(item.noticeKeys.map(encodeURIComponent).join(','))}"` : ''}
    >${escapeHtml(item.label)}</button>
  `).join('');
}

function renderContainerCard(container) {
  const stateClass = containerStateClass(container);
  const title = container.name || container.id || '-';
  const errorCount = Number(container.recent_error_count || 0);
  const warnCount = Number(container.recent_warn_count || 0);
  const showErrorCount = errorCount > 0 && !areNoticeKeysDismissed(containerNoticeKeys(container, 'error'));
  const showWarnCount = warnCount > 0 && !areNoticeKeysDismissed(containerNoticeKeys(container, 'warn'));
  return `
    <article class="container-row" data-container-key="${escapeHtml(title)}">
      <div class="container-title-block">
        <div class="container-name" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
        <div class="container-image" title="${escapeHtml(container.image || '-')}">${escapeHtml(container.image || '-')}</div>
      </div>
      <div class="container-state-block">
        <div class="container-meta">
          <span class="badge ${stateClass}">${escapeHtml(containerStateLabel(container))}</span>
          ${container.service ? `<span class="stat-pill">${escapeHtml(container.service)}</span>` : ''}
          ${container.memory_usage ? `<span class="stat-pill">${escapeHtml(container.memory_usage)}</span>` : ''}
          ${container.net_io ? `<span class="stat-pill">${escapeHtml(container.net_io)}</span>` : ''}
          ${showErrorCount ? `<span class="badge failed">异常 ${errorCount}</span>` : ''}
          ${showWarnCount ? `<span class="badge running">警告 ${warnCount}</span>` : ''}
        </div>
        <div class="container-ports" title="${escapeHtml(container.ports || '-')}">${escapeHtml(container.ports || '-')}</div>
      </div>
      <div class="container-meters">
        ${renderMeter('CPU', container.cpu_percent)}
        ${renderMeter('MEM', container.memory_percent)}
        <div class="container-actions">${containerActions(container)}</div>
      </div>
    </article>
  `;
}

function renderMeter(label, value) {
  return `
    <div class="meter-line">
      <span>${escapeHtml(label)}</span>
      <div class="meter-track" data-resource-value="${escapeHtml(value)}"></div>
      <strong>${formatPercent(value)}</strong>
    </div>
  `;
}

function rangeCutoffSeconds(range) {
  if (range === '6h') return 6 * 60 * 60;
  if (range === '24h') return 24 * 60 * 60;
  if (range === '7d') return 7 * 24 * 60 * 60;
  return 0;
}

function formatTrendTime(point) {
  const ts = Number(point?.ts);
  if (Number.isFinite(ts) && ts > 0) {
    return new Date(ts * 1000).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      ...(systemTrendRange === 'realtime' ? {second: '2-digit'} : {}),
      hour12: false,
    });
  }
  return point?.at || '-';
}

function trendPointsPerDay(intervalMinutes) {
  const minutes = Number(intervalMinutes);
  if (!Number.isFinite(minutes) || minutes <= 0) return 48;
  return Math.round((24 * 60) / minutes);
}

function trendChartWidth(points, pad) {
  const usableMin = 320 - pad.left - pad.right;
  const pointSpan = Math.max(0, (points.length || 1) - 1) * 18;
  return Math.round(pad.left + pad.right + Math.max(usableMin, pointSpan));
}

function trendPointX(index, total, width, pad) {
  const usableWidth = width - pad.left - pad.right;
  if (total <= 1) return pad.left + usableWidth / 2;
  return pad.left + (index / (total - 1)) * usableWidth;
}

function trendPointValue(point, key, { percent = true } = {}) {
  if (point?.[key] == null || point[key] === '') return null;
  const value = Number(point?.[key]);
  if (!Number.isFinite(value)) return null;
  return percent ? Math.max(0, Math.min(100, value)) : Math.max(0, value);
}

const realtimeTrendScales = new Map();
function trendScale(points, key, options = {}) {
  const values = points
    .map(point => trendPointValue(point, key, options))
    .filter(value => value != null);
  if (!values.length) return null;
  if (options.realtime) {
    const max = options.percent === false
      ? Math.max(realtimeTrendScales.get(key) || 8, 2 ** Math.ceil(Math.log2(Math.max(8, ...values))))
      : 100;
    realtimeTrendScales.set(key, max);
    return {min: 0, max, latest: values[values.length - 1]};
  }
  let min = Math.min(...values);
  let max = Math.max(...values);
  const spread = max - min;
  const configuredMinSpan = Number(options.minSpan ?? (options.percent === false ? 1 : 5));
  const minSpan = Number.isFinite(configuredMinSpan) && configuredMinSpan > 0 ? configuredMinSpan : 5;
  const padding = Math.max(spread * 0.25, minSpan / 2);
  if (spread < 0.1) {
    min -= minSpan / 2;
    max += minSpan / 2;
  } else {
    min -= padding;
    max += padding;
  }
  min = Math.max(0, min);
  max = options.percent === false ? Math.max(max, min + minSpan) : Math.min(100, max);
  if (max - min < minSpan) {
    const center = (max + min) / 2;
    min = Math.max(0, center - minSpan / 2);
    max = options.percent === false ? center + minSpan / 2 : Math.min(100, center + minSpan / 2);
    if (options.percent !== false && max - min < minSpan) {
      if (max >= 100) min = Math.max(0, 100 - minSpan);
      if (min <= 0) max = Math.min(100, minSpan);
    }
  }
  if (max <= min) max = options.percent === false ? min + minSpan : Math.min(100, min + minSpan);
  return { min, max, latest: values[values.length - 1] };
}

function trendPath(points, key, width, height, pad, scale, options = {}) {
  const usableHeight = height - pad.top - pad.bottom;
  const range = Math.max(scale.max - scale.min, 1);
  const segments = [];
  let current = [];
  points.forEach((point, index) => {
    const value = trendPointValue(point, key, options);
    if (value == null) {
      if (current.length) segments.push(current);
      current = [];
      return;
    }
    const x = trendPointX(index, points.length, width, pad);
    const y = pad.top + (1 - ((value - scale.min) / range)) * usableHeight;
    current.push([x, y, value, point]);
  });
  if (current.length) segments.push(current);
  return options.withSamples ? segments : segments.map(trendLinePath);
}

function trendLinePath(segment) {
  return DashboardMotion.curve(segment);
}

function trendDots(points, key, className, label, width, height, pad, scale, options = {}) {
  const usableHeight = height - pad.top - pad.bottom;
  const range = Math.max(scale.max - scale.min, 1);
  const formatter = options.formatter || formatPercent;
  return points.map((point, index) => {
    const value = trendPointValue(point, key, options);
    if (value == null) return '';
    const x = trendPointX(index, points.length, width, pad);
    const y = pad.top + (1 - ((value - scale.min) / range)) * usableHeight;
    const tooltip = typeof options.tooltipFormatter === 'function'
      ? options.tooltipFormatter(point, value)
      : `${formatTrendTime(point)} · ${label} ${formatter(value)}`;
    return `<g class="trend-point" tabindex="0" aria-label="${escapeHtml(tooltip)}" data-tooltip="${escapeHtml(tooltip)}">
      <circle class="trend-dot-hit" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="9"></circle>
      <circle class="trend-dot ${className}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2"></circle>
    </g>`;
  }).join('');
}

function trendAxisLabel(value, options = {}) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  if (options.percent !== false) return formatPercent(n).replace('%', '');
  if (Math.abs(n) >= 1000) return `${Math.round(n / 100) / 10}k`;
  if (Math.abs(n) >= 100) return String(Math.round(n));
  return String(Math.round(n * 10) / 10);
}

function trendTooltipElement() {
  if (trendTooltipEl) return trendTooltipEl;
  trendTooltipEl = document.createElement('div');
  trendTooltipEl.className = 'trend-tooltip';
  trendTooltipEl.setAttribute('role', 'tooltip');
  trendTooltipEl.hidden = true;
  document.body.appendChild(trendTooltipEl);
  return trendTooltipEl;
}

function positionTrendTooltip(event) {
  const tooltip = trendTooltipElement();
  const rect = tooltip.getBoundingClientRect();
  const margin = 10;
  let left = event.clientX + 14;
  let top = event.clientY + 14;
  if (left + rect.width > window.innerWidth - margin) left = event.clientX - rect.width - 14;
  if (top + rect.height > window.innerHeight - margin) top = event.clientY - rect.height - 14;
  tooltip.style.left = `${Math.max(margin, left)}px`;
  tooltip.style.top = `${Math.max(margin, top)}px`;
}

function showTrendTooltip(target, event) {
  const text = target?.dataset?.tooltip || '';
  if (!text) return;
  const tooltip = trendTooltipElement();
  tooltip.textContent = text;
  tooltip.hidden = false;
  tooltip.dataset.visible = '1';
  positionTrendTooltip(event);
}

function hideTrendTooltip() {
  if (!trendTooltipEl) return;
  trendTooltipEl.hidden = true;
  delete trendTooltipEl.dataset.visible;
}

function trendAvailability(points, config, timing = {}) {
  const valid = points.map(point => trendPointValue(point, config.key, config) != null);
  if (valid.some((value, index) => index > 0 && value && valid[index - 1])) {
    return { ready: true, message: '' };
  }
  const configuredInterval = Number(timing.intervalSeconds);
  const interval = Number.isFinite(configuredInterval) && configuredInterval > 0 ? configuredInterval : 1800;
  const now = timing.nowSeconds ?? Date.now() / 1000;
  const lastTs = Number(timing.lastSampleTs);
  const remaining = valid.length && valid[valid.length - 1] ? 1 : 2;
  const minutes = seconds => Math.max(1, Math.ceil(seconds / 60));
  const duration = seconds => seconds < 60 ? `${Math.max(1, Math.ceil(seconds))} 秒` : `${minutes(seconds)} 分钟`;
  if (!Number.isFinite(lastTs) || lastTs <= 0) {
    return { ready: false, message: `等待首次采样；首个有效样本后预计再等 ${duration(interval)}` };
  }
  const nextIn = lastTs + interval - now;
  if (nextIn <= 0) {
    return { ready: false, message: `等待采样更新；还需 ${remaining} 个连续有效样本（采样间隔 ${duration(interval)}）` };
  }
  return { ready: false, message: `预计约 ${duration(nextIn + (remaining - 1) * interval)} 后显示，需连续有效采样` };
}

function renderTrendCard(points, config, availability = trendAvailability(points, config)) {
  const { key, className, label, subLabel, formatter = formatPercent } = config;
  const scale = trendScale(points, key, config);
  if (!availability.ready) {
    return `
      <article class="trend-card ${className}">
        <div class="trend-card-head">
          <span>${escapeHtml(label)}</span>
          <strong>${scale ? escapeHtml(formatter(scale.latest)) : '-'}</strong>
        </div>
        <div class="trend-card-meta"><span>${escapeHtml(subLabel || '')}</span></div>
        <div class="trend-card-empty"><span>样本不足，暂未生成趋势</span><small>${escapeHtml(availability.message)}</small></div>
      </article>
    `;
  }
  const height = 210;
  const pad = { top: 12, right: 12, bottom: 20, left: 42 };
  const width = config.realtime ? 480 : trendChartWidth(points, pad);
  const yTop = pad.top;
  const yMid = pad.top + (height - pad.top - pad.bottom) / 2;
  const yBottom = height - pad.bottom;
  const paths = trendPath(points, key, width, height, pad, scale, {...config, withSamples: true})
    .map(segment => `<path class="trend-line ${className}" data-samples="${escapeHtml(JSON.stringify(segment.map(([x, y, , point]) => [x, y, point.ts])))}" d="${trendLinePath(segment)}"></path>`)
    .join('');
  const dots = trendDots(points, key, className, label, width, height, pad, scale, config);
  const latestPoint = points[points.length - 1] || {};
  return `
    <article class="trend-card ${className}" data-trend-key="${key}" style="--trend-width:${width}px">
      <div class="trend-card-head">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(formatter(scale.latest))}</strong>
      </div>
      <div class="trend-card-meta">
        <span>${escapeHtml(subLabel || '')}</span>
        <span>${escapeHtml(formatter(scale.min))}-${escapeHtml(formatter(scale.max))}</span>
      </div>
      <div class="trend-chart-scroll" tabindex="0">
        <svg class="trend-mini-svg" ${config.realtime ? 'data-realtime="true" preserveAspectRatio="none"' : ''} viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}趋势">
          <line class="trend-grid-line" x1="${pad.left}" y1="${yTop.toFixed(1)}" x2="${width - pad.right}" y2="${yTop.toFixed(1)}"></line>
          <line class="trend-grid-line" x1="${pad.left}" y1="${yMid.toFixed(1)}" x2="${width - pad.right}" y2="${yMid.toFixed(1)}"></line>
          <line class="trend-grid-line" x1="${pad.left}" y1="${yBottom.toFixed(1)}" x2="${width - pad.right}" y2="${yBottom.toFixed(1)}"></line>
          <text class="trend-axis-label" x="${pad.left - 6}" y="${(yMid + 4).toFixed(1)}">${escapeHtml(trendAxisLabel((scale.max + scale.min) / 2, config))}</text>
          <text class="trend-axis-label" x="${pad.left - 5}" y="${(yTop + 3).toFixed(1)}">${escapeHtml(trendAxisLabel(scale.max, config))}</text>
          <text class="trend-axis-label" x="${pad.left - 5}" y="${(yBottom + 3).toFixed(1)}">${escapeHtml(trendAxisLabel(scale.min, config))}</text>
          ${paths}
          ${dots}
        </svg>
      </div>
      <div class="trend-card-foot">
        <span>${escapeHtml(formatTrendTime(points[0]))}</span>
        <span>${escapeHtml(formatTrendTime(latestPoint))}</span>
      </div>
    </article>
  `;
}

function renderSystemTrend(system = {}) {
  const chart = $('systemTrendChart');
  if (!chart) return;
  const realtime = systemTrendRange === 'realtime';
  const source = realtime ? system.realtime_history : system.history;
  const rawHistory = Array.isArray(source) ? source : [];
  const cutoff = rangeCutoffSeconds(systemTrendRange);
  const nowTs = Math.floor(Date.now() / 1000);
  const filtered = cutoff ? rawHistory.filter(point => Number(point?.ts) >= nowTs - cutoff) : rawHistory;
  const points = filtered.slice().reverse();
  const intervalSeconds = realtime ? 1 : system.history_interval_seconds;
  const timing = { intervalSeconds, nowSeconds: nowTs, lastSampleTs: rawHistory[0]?.ts };
  const availability = ['cpu_percent', 'memory_percent', 'network_total_kbps'].map(key =>
    trendAvailability(points, {key, percent: key !== 'network_total_kbps'}, timing));
  const signature = JSON.stringify([systemTrendRange, intervalSeconds, points, availability]);
  if (chart.dataset.signature === signature) return;
  chart.dataset.signature = signature;
  const intervalMinutes = Math.round(Number(system.history_interval_seconds || 1800) / 60);
  const latest = points[points.length - 1] || rawHistory[0] || {};
  const hint = $('systemTrendHint');
  const pointsPerDay = trendPointsPerDay(intervalMinutes || 30);
  if (hint) {
    hint.textContent = realtime
      ? `每秒采样 · 最近 60 个样本 · 已采集 ${points.length} 个${points.length ? ` · 最新 ${formatTrendTime(latest)}` : ''}`
      : points.length
      ? `每 ${intervalMinutes || 30} 分钟采样 · 1 天 ${pointsPerDay} 点 · 当前范围 ${points.length} 点 · 可左右滑动 · 最新 ${formatTrendTime(latest)}`
      : `每 ${intervalMinutes || 30} 分钟采样一次 · 1 天约 ${pointsPerDay} 点 · 等待 CPU、内存、网络样本`;
  }

  chart.classList.remove('skeleton-block');
  chart.classList.toggle('realtime-trend', realtime);
  const markup = `
    <div class="trend-card-grid">
      ${renderTrendCard(points, { realtime, key: 'cpu_percent', className: 'cpu', label: 'CPU', subLabel: '处理器' }, availability[0])}
      ${renderTrendCard(points, { realtime, key: 'memory_percent', className: 'memory', label: '内存', subLabel: '占用率' }, availability[1])}
      ${renderTrendCard(points, {
        realtime,
        key: 'network_total_kbps',
        className: 'network',
        label: '网络',
        subLabel: `RX ${formatNumber(latest.network_rx_kbps, 'KB/s')} / TX ${formatNumber(latest.network_tx_kbps, 'KB/s')}`,
        percent: false,
        minSpan: 8,
        formatter: value => formatNumber(value, 'KB/s'),
        tooltipFormatter: (point, value) => `${formatTrendTime(point)} · 网络 ${formatNumber(value, 'KB/s')} · RX ${formatNumber(point.network_rx_kbps, 'KB/s')} · TX ${formatNumber(point.network_tx_kbps, 'KB/s')}`,
      }, availability[2])}
    </div>
  `;
  const template = document.createElement('template');
  template.innerHTML = markup;
  const cards = Array.from(chart.querySelectorAll('.trend-card'));
  if (cards.length !== 3) {
    chart.replaceChildren(template.content);
    return;
  }
  hideTrendTooltip();
  const nextCards = Array.from(template.content.querySelectorAll('.trend-card'));
  cards.forEach((card, i) => {
    const next = nextCards[i];
    const svg = card.querySelector('svg');
    const nextSvg = next.querySelector('svg');
    if (!svg && !nextSvg) {
      ['.trend-card-head', '.trend-card-meta', '.trend-card-empty'].forEach(selector => {
        card.querySelector(selector).innerHTML = next.querySelector(selector).innerHTML;
      });
      return;
    }
    if (!svg || !nextSvg) {
      card.replaceWith(next);
      return;
    }
    const scroll = card.querySelector('.trend-chart-scroll');
    const atEnd = scroll.scrollWidth - scroll.clientWidth - scroll.scrollLeft < 8;
    card.style.cssText = next.style.cssText;
    ['.trend-card-head', '.trend-card-meta', '.trend-card-foot'].forEach(selector => {
      card.querySelector(selector).innerHTML = next.querySelector(selector).innerHTML;
    });
    DashboardMotion.morph(svg, nextSvg);
    if (atEnd) scroll.scrollLeft = scroll.scrollWidth;
  });
}

function renderAlerts(alerts = []) {
  const list = $('alertList');
  const badge = $('alertBadge');
  if (!list || !badge) return;
  lastAlerts = Array.isArray(alerts) ? alerts : [];
  list.classList.remove('skeleton-block');
  const visibleAlertsAll = lastAlerts.filter(alert => !dismissedNoticeKeys.has(noticeSignature('alert', alert)));
  const dismissedCount = lastAlerts.length - visibleAlertsAll.length;
  const critical = visibleAlertsAll.filter(item => item.level === 'critical').length;
  const warning = visibleAlertsAll.filter(item => item.level === 'warning').length;
  badge.className = `badge ${critical ? 'failed' : warning ? 'running' : dismissedCount ? 'neutral' : 'success'}`;
  badge.textContent = critical ? `${critical} 个严重` : warning ? `${warning} 个提醒` : dismissedCount ? '已清除' : '正常';
  list.classList.toggle('scrollable-list', visibleAlertsAll.length > 20);
  if (!visibleAlertsAll.length) {
    list.innerHTML = dismissedCount
      ? '<div class="project-empty compact-empty">已清除当前告警，新异常会继续显示</div>'
      : '<div class="project-empty compact-empty">暂无告警</div>';
    updateClearAlertsButton();
    return;
  }
  const visibleAlerts = activeView === 'events' ? visibleAlertsAll.slice(0, 40) : visibleAlertsAll.slice(0, 8);
  list.innerHTML = visibleAlerts.map(alert => `
    <div class="alert-item ${severityClass(alert.level)}">
      <div>
        <strong>${escapeHtml(alert.title || '告警')}</strong>
        <span>${escapeHtml(alert.detail || '-')}</span>
        ${alert.muted ? '<small>通知已静音</small>' : ''}
      </div>
      ${alert.command ? `<code>${escapeHtml(alert.command)}</code>` : ''}
    </div>
  `).join('');
  updateClearAlertsButton();
}

function renderEvents(events = []) {
  const target = $('eventTimeline');
  if (!target) return;
  lastEvents = Array.isArray(events) ? events : [];
  target.classList.remove('skeleton-block');
  const hint = $('eventTimelineHint');
  const abnormalEvents = lastEvents;
  const visibleEventsAll = abnormalEvents.filter(event => !dismissedNoticeKeys.has(noticeSignature('event', event)));
  const dismissedCount = abnormalEvents.length - visibleEventsAll.length;
  if (hint) {
    hint.textContent = visibleEventsAll.length
      ? `最近 ${visibleEventsAll.length} 条异常与恢复事件${dismissedCount ? ` · 已清除 ${dismissedCount} 条` : ''}`
      : dismissedCount
        ? `已清除 ${dismissedCount} 条当前异常，新异常会继续显示`
        : '异常与恢复记录';
  }
  target.classList.toggle('scrollable-list', visibleEventsAll.length > 20);
  if (!visibleEventsAll.length) {
    target.innerHTML = dismissedCount
      ? '<div class="project-empty compact-empty">已清除当前事件，新异常会继续显示</div>'
      : '<div class="project-empty compact-empty">暂无事件</div>';
    updateClearAlertsButton();
    return;
  }
  const visibleEvents = activeView === 'events' ? visibleEventsAll.slice(0, 60) : visibleEventsAll.slice(0, 16);
  target.innerHTML = visibleEvents.map(event => `
    <div class="event-item ${severityClass(event.level)}">
      <span class="event-dot"></span>
      <div>
        <strong>${escapeHtml(event.title || '-')}</strong>
        <small>${escapeHtml(event.at || '-')} · ${escapeHtml(event.source || event.kind || '-')}</small>
        <p>${escapeHtml(event.detail || '-')}</p>
      </div>
    </div>
  `).join('');
  updateClearAlertsButton();
}

function renderSystemStatus(system = {}) {
  if (Number(lastSystemPayload?.server?.sampled_ts || 0) > Number(system.server?.sampled_ts || 0)) {
    system = {...system, server: lastSystemPayload.server, realtime_history: lastSystemPayload.realtime_history};
  }
  lastSystemPayload = system;
  const server = system.server || {};
  const memory = server.memory || {};
  const disk = server.disk || {};
  const network = server.network || {};
  const load = server.load || {};
  const docker = system.docker || {};
  const containers = docker.containers || [];

  animateNumberText('cpuValue', server.cpu_percent, formatPercent);
  $('cpuSub').textContent = `Load ${formatNumber(load.one)} / ${formatNumber(load.five)} / ${formatNumber(load.fifteen)}`;
  setResourceBar('cpuBar', server.cpu_percent);

  animateNumberText('memoryValue', memory.percent, formatPercent);
  $('memorySub').textContent = `${formatNumber(memory.used_mb, 'MB')} / ${formatNumber(memory.total_mb, 'MB')}`;
  setResourceBar('memoryBar', memory.percent);

  animateNumberText('diskValue', disk.percent, formatPercent);
  $('diskSub').textContent = `${formatNumber(disk.used_gb, 'GB')} / ${formatNumber(disk.total_gb, 'GB')} · ${disk.path || '/'}`;
  setResourceBar('diskBar', disk.percent);

  animateNumberText('networkValue', network.percent, formatPercent);
  $('networkSub').textContent = `RX ${formatNumber(network.rx_kbps, 'KB/s')} / TX ${formatNumber(network.tx_kbps, 'KB/s')}`;
  setResourceBar('networkBar', network.percent);
  renderSystemTrend(system);

  const dockerBadge = $('dockerBadge');
  dockerBadge.className = `badge ${docker.available ? 'success' : 'failed'}`;
  dockerBadge.textContent = docker.available ? `${containers.length} 个容器` : 'Docker 不可用';
  $('dockerHint').textContent = docker.available
    ? `采样 ${server.sampled_at || '-'} · 缓存 ${server.cache_seconds || 0}s`
    : (docker.error || '无法读取 Docker 状态');

  const list = $('containerList');
  list.classList.remove('skeleton-block');
  if (!docker.available) {
    lastContainerNoticeKeys = [];
    list.innerHTML = `<div class="project-empty">${escapeHtml(docker.error || 'Docker 不可用')}</div>`;
    return;
  }
  if (!containers.length) {
    lastContainerNoticeKeys = [];
    list.innerHTML = '<div class="project-empty">暂无容器</div>';
    return;
  }
  lastContainerNoticeKeys = containers.flatMap(container => containerNoticeKeys(container));
  let grid = list.querySelector('.container-service-grid');
  if (!grid) {
    grid = document.createElement('div');
    grid.className = 'container-service-grid';
    list.replaceChildren(grid);
  }
  const existing = new Map(Array.from(grid.children).map(card => [card.dataset.containerKey, card]));
  containers.forEach((container, index) => {
    const key = container.name || container.id || '-';
    const old = existing.get(key);
    existing.delete(key);
    const signature = JSON.stringify([container, containerNoticeKeys(container).map(k => dismissedNoticeKeys.has(k))]);
    let card = old;
    if (!old || old.dataset.signature !== signature) {
      const template = document.createElement('template');
      template.innerHTML = renderContainerCard(container);
      const next = template.content.firstElementChild;
      if (old) {
        // Keep the meter and its label connected so in-flight animations continue.
        ['.container-title-block', '.container-state-block', '.container-actions'].forEach(selector => {
          const current = old.querySelector(selector);
          const replacement = next.querySelector(selector);
          if (current.innerHTML !== replacement.innerHTML) current.innerHTML = replacement.innerHTML;
        });
        const tracks = Array.from(next.querySelectorAll('.meter-track'));
        old.querySelectorAll('.meter-track').forEach((track, i) => {
          track.dataset.resourceValue = tracks[i].dataset.resourceValue;
        });
      } else {
        card = next;
      }
      card.dataset.signature = signature;
    }
    if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
  });
  existing.forEach(card => card.remove());
  list.querySelectorAll('.meter-track[data-resource-value]').forEach(track => {
    updateResourceTrack(track, track.dataset.resourceValue);
  });
}

function closeContainerLogsModal() {
  const modal = $('containerLogsModal');
  if (modal) modal.hidden = true;
}

function openContainerLogsModal(container, lines, meta = '') {
  currentContainerLogName = container || currentContainerLogName;
  $('containerLogsTitle').textContent = container;
  $('containerLogsMeta').textContent = meta || `${lines.length} 行`;
  $('containerLogCount').textContent = `${lines.length} 行`;
  $('containerLogsContent').innerHTML = lines.length
    ? renderLogLines(lines)
    : '<span class="log-line">暂无日志</span>';
  $('containerLogsModal').hidden = false;
}

async function showContainerLogs(container, tail = currentContainerLogLineLimit) {
  const safeTail = positiveLineCount(tail, 200);
  const filterOptions = containerLogFilterOptions();
  currentContainerLogLineLimit = safeTail;
  currentContainerLogName = container;
  openContainerLogsModal(container, ['加载中...'], `读取最近 ${safeTail} 行${containerLogFilterLabel(filterOptions)}`);
  try {
    const data = await fetchJson(logsUrl('docker/logs', {
      container,
      tail: safeTail,
      ...filterOptions,
    }));
    const lines = data.lines || [];
    const meta = data.filtered
      ? `${data.filter_label || '筛选'}匹配 ${data.matched_count || 0} 条，显示 ${lines.length} 行，上下文 ${data.context || 0} 行`
      : `最近 ${lines.length} 行`;
    openContainerLogsModal(data.container || container, lines, meta);
  } catch (err) {
    openContainerLogsModal(container, [`读取日志失败: ${err.message}`], '读取失败');
  }
}

function downloadCurrentContainerLog() {
  if (!currentContainerLogName) return;
  if ($('containerLogLineSelect')?.value === 'custom') {
    currentContainerLogLineLimit = selectedLineCount(
      'containerLogLineSelect',
      'containerLogLineCustomInput',
      currentContainerLogLineLimit,
    );
  }
  const lines = selectedDownloadLines('containerLogDownloadSelect', 'containerLogDownloadCustomInput', currentContainerLogLineLimit);
  window.location.href = logsUrl('docker/logs/download', {
    container: currentContainerLogName,
    lines,
    ...containerLogFilterOptions(),
  });
}

let containerActionBusy = false;
async function runContainerAction(container, action) {
  if (containerActionBusy) return;
  containerActionBusy = true;
  const item = lastSystemPayload?.docker?.containers?.find(item => item.name === container || item.id === container);
  const labels = {
    restart: '重启',
    stop: '停止',
    start: '启动',
    pause: '暂停',
    unpause: '恢复',
    remove: '删除',
  };
  const label = labels[action] || action;
  const confirmed = await showConfirmDialog({
    title: `${label}容器`,
    message: action === 'remove'
      ? `确认删除容器 ${container}（${item?.id?.slice(0, 12) || '未知 ID'}）？容器自身写入的文件将丢失，数据卷和宿主机挂载目录保留。Compose 下次启动可能重建该容器。`
      : `确认${label}容器 ${container}？`,
    confirmText: label,
    danger: action !== 'start' && action !== 'unpause',
  });
  if (!confirmed) { containerActionBusy = false; return; }
  try {
    $('dockerFeedback').textContent = `正在${label}容器 ${container}…`;
    await postJsonBody('docker/action', { container, action, container_id: item?.id || '' });
    $('dockerFeedback').textContent = `容器 ${container} 已${label}`;
    await refreshServerStatus();
  } catch (err) {
    $('dockerFeedback').textContent = `容器操作失败：${err.message}`;
  } finally {
    containerActionBusy = false;
  }
}

function handleContainerAction(event) {
  const button = event.target.closest('[data-container-action]');
  if (!button) return;
  const container = button.dataset.containerName || '';
  const action = button.dataset.containerAction || '';
  if (!container || !action) return;
  if (action === 'dismiss-notices') {
    const keys = (button.dataset.noticeKeys || '')
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
      .map(item => {
        try {
          return decodeURIComponent(item);
        } catch (_) {
          return item;
        }
      });
    keys.forEach(key => dismissedNoticeKeys.add(key));
    saveDismissedNoticeKeys();
    if (lastSystemPayload) renderSystemStatus(lastSystemPayload);
    renderAlerts(lastAlerts);
    renderEvents(lastEvents);
    return;
  }
  if (action === 'logs') {
    showContainerLogs(container);
    return;
  }
  if (action === 'logs-error') {
    const filter = $('containerLogFilterSelect');
    if (filter) filter.value = 'error';
    syncContainerLogFilterControls();
    showContainerLogs(container);
    return;
  }
  runContainerAction(container, action);
}

function logLineClass(line) {
  const text = String(line).toLowerCase();
  if (/(error|failed|failure|fatal|exception|traceback|health failed|curl: \(22\)| 502| 503|exit code: 1|code=1)/.test(text)) {
    return 'log-error';
  }
  if (/(warn|warning|retrying|timeout|timed out|attempt=|ignored)/.test(text)) {
    return 'log-warn';
  }
  if (/(health ok|healthy|success)/.test(text)) {
    return 'log-ok';
  }
  return '';
}

function renderLogLines(lines) {
  return lines.map(line => {
    const cls = logLineClass(line);
    return `<span class="log-line ${cls}">${escapeHtml(line)}</span>`;
  }).join('');
}

function logsUrl(path, extra = {}) {
  const params = new URLSearchParams();
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  });
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function updateServerRefreshControls() {
  document.querySelectorAll('[data-server-refresh]').forEach(button => {
    button.classList.toggle('active', Number(button.dataset.serverRefresh) === serverRefreshSeconds);
  });
}

function clearServerRefresh() {
  if (serverRefreshTimer) {
    window.clearTimeout(serverRefreshTimer);
    serverRefreshTimer = null;
  }
}

function scheduleServerRefresh() {
  clearServerRefresh();
  if (activeView !== 'server' || document.hidden) return;
  serverRefreshTimer = window.setTimeout(refreshServerStatus, serverRefreshSeconds * 1000);
}

function clearEventsRefresh() {
  if (eventsRefreshTimer) window.clearTimeout(eventsRefreshTimer);
  eventsRefreshTimer = null;
}

async function refreshEventsStatus() {
  if (activeView !== 'events') return;
  clearEventsRefresh();
  try {
    const status = await fetchJson('status');
    csrfToken = status.csrf_token || csrfToken;
    if (activeView !== 'events') return;
    renderAlerts(status.alerts || []);
    renderEvents(status.events || []);
    $('updatedAt').textContent = `事件刷新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  } catch (err) {
    if (activeView === 'events') $('eventTimelineHint').textContent = `事件加载失败: ${err.message}`;
  } finally {
    if (activeView === 'events') eventsRefreshTimer = window.setTimeout(refreshEventsStatus, 30000);
  }
}

async function refreshNotificationConfig() {
  try {
    const [config, status] = await Promise.all([fetchJson('notifications'), fetchJson('status')]);
    csrfToken = status.csrf_token || csrfToken;
    if (activeView !== 'notify') return;
    lastConfig = config;
    renderNotificationConfig(config.notifications || {});
  } catch (err) {
    if (activeView === 'notify') $('notificationResult').textContent = `通知配置加载失败: ${err.message}`;
  }
}

let serverRefreshInFlight = false;
let realtimeRefreshInFlight = false;

async function refreshRealtimeMetrics() {
  if (activeView !== 'server' || document.hidden || realtimeRefreshInFlight) return;
  realtimeRefreshInFlight = true;
  try {
    const metrics = await fetchJson('system-metrics');
    if (activeView !== 'server' || document.hidden) return;
    renderSystemStatus({...lastSystemPayload, ...metrics});
  } catch (err) {
    if (activeView === 'server' && systemTrendRange === 'realtime') {
      $('systemTrendHint').textContent = `实时采样暂不可用，正在重试：${err.message}`;
    }
  } finally {
    realtimeRefreshInFlight = false;
  }
}

window.setInterval(refreshRealtimeMetrics, 1000);
async function refreshServerStatus() {
  if (activeView !== 'server') return;
  if (serverRefreshInFlight || document.hidden) return;
  serverRefreshInFlight = true;
  const requestId = ++systemStatusRequestId;
  clearServerRefresh();
  try {
    const status = await fetchJson('status');
    if (activeView !== 'server' || document.hidden || requestId !== systemStatusRequestId) return;
    csrfToken = status.csrf_token || csrfToken;
    updateLiveIndicator(true);
    renderSystemStatus(status.system || {});
    renderAlerts(status.alerts || []);
    renderEvents(status.events || []);

    $('updatedAt').textContent = `服务器刷新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  } catch (err) {
    updateLiveIndicator(false);
    $('dockerHint').textContent = `服务器状态刷新失败: ${err.message}`;
  } finally {
    serverRefreshInFlight = false;
    scheduleServerRefresh();
  }
}

function setServerRefreshInterval(seconds) {
  const next = Number(seconds);
  if (![1, 5, 10, 30].includes(next)) return;
  serverRefreshSeconds = next;
  updateServerRefreshControls();
  refreshServerStatus();
}

let resourceResizeTimer = null;
function refreshResourceTracks() {
  document.querySelectorAll('.resource-track[data-resource-value], .meter-track[data-resource-value]').forEach(track => {
    updateResourceTrack(track, track.dataset.resourceValue);
  });
}

window.addEventListener('resize', () => {
  if (resourceResizeTimer) window.clearTimeout(resourceResizeTimer);
  resourceResizeTimer = window.setTimeout(refreshResourceTracks, 120);
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearServerRefresh();
  else if (activeView === 'server') refreshServerStatus();
});

(() => {
  const root = $('toolbarMore'), button = $('toolbarMoreButton'), menu = $('toolbarMoreMenu');
  const items = () => [...menu.querySelectorAll('[role="menuitem"]')].filter(item => !item.disabled && item.getClientRects().length);
  const close = (restoreFocus = false) => {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  };
  const open = (last = false) => {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    const choices = items();
    choices[last ? choices.length - 1 : 0]?.focus();
  };
  button.addEventListener('click', () => menu.hidden ? open() : close(true));
  button.addEventListener('keydown', event => {
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation(); open(event.key === 'ArrowUp');
    }
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const choices = items(), index = choices.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
    choices[next]?.focus();
  });
  menu.addEventListener('click', event => { if (event.target.closest('[role="menuitem"]')) close(); });
  root.addEventListener('focusout', () => queueMicrotask(() => { if (!root.contains(document.activeElement)) close(); }));
  document.addEventListener('pointerdown', event => { if (!root.contains(event.target)) close(); });
  document.querySelectorAll('.view-tab').forEach(tab => tab.addEventListener('click', () => close()));
})();

$('refreshBtn').addEventListener('click', () => {
  if (activeView === 'server') refreshServerStatus();
  else if (activeView === 'events') refreshEventsStatus();
  else if (activeView === 'notify') refreshNotificationConfig();
  else if (activeView === 'certificates') refreshCertificates();
  else if (activeView === 'requests') window.NginxRequests?.refresh();
});
$('serverViewTab').addEventListener('click', () => setView('server'));
$('eventsViewTab')?.addEventListener('click', () => setView('events'));
$('notifyViewTab')?.addEventListener('click', () => setView('notify'));
$('certificatesViewTab')?.addEventListener('click', () => setView('certificates'));
$('requestsViewTab')?.addEventListener('click', () => setView('requests'));
$('clearAlertsBtn')?.addEventListener('click', toggleCurrentNoticeDismissal);
document.querySelectorAll('[data-server-refresh]').forEach(button => {
  button.addEventListener('click', () => setServerRefreshInterval(button.dataset.serverRefresh));
});
document.querySelectorAll('[data-trend-range]').forEach(button => {
  button.addEventListener('click', () => {
    systemTrendRange = button.dataset.trendRange || '24h';
    document.querySelectorAll('[data-trend-range]').forEach(item => item.classList.toggle('active', item === button));
    if (activeView === 'server') refreshServerStatus();
  });
});
$('saveNotificationBtn')?.addEventListener('click', saveNotificationConfig);
$('testNotificationBtn')?.addEventListener('click', testNotificationConfig);
[
  'notifyWecomEnabled',
  'notifyWecomWebhook',
  'notifyDingtalkEnabled',
  'notifyDingtalkWebhook',
  'notifyDingtalkSecret',
  'notifyEmailEnabled',
  'notifyEmailHost',
  'notifyEmailPort',
  'notifyEmailUsername',
  'notifyEmailPassword',
  'notifyEmailFrom',
  'notifyEmailTo',
  'notifyEmailSsl',
  'notifyEmailStarttls',
].forEach(id => {
  const input = $(id);
  if (!input) return;
  input.addEventListener('input', () => {
    notificationDirty = true;
    syncNotificationBadge();
  });
  input.addEventListener('change', () => {
    notificationDirty = true;
    if (id === 'notifyEmailSsl' && input.checked) setInputChecked('notifyEmailStarttls', false);
    if (id === 'notifyEmailStarttls' && input.checked) setInputChecked('notifyEmailSsl', false);
    syncNotificationBadge();
  });
});
$('closeContainerLogsBtn').addEventListener('click', closeContainerLogsModal);
$('themeToggleBtn')?.addEventListener('click', toggleTheme);
$('containerLogsModal').addEventListener('click', (event) => {
  if (event.target === $('containerLogsModal')) closeContainerLogsModal();
});
$('confirmModal').addEventListener('click', (event) => {
  if (event.target === $('confirmModal')) closeConfirmDialog(false);
});
$('confirmCancelBtn').addEventListener('click', () => closeConfirmDialog(false));
$('confirmOkBtn').addEventListener('click', () => closeConfirmDialog(true));
$('containerList').addEventListener('click', handleContainerAction);
document.addEventListener('click', async (event) => {
  const code = event.target.closest('.copy-command');
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code.textContent || '');
    code.dataset.copied = '1';
    window.setTimeout(() => { delete code.dataset.copied; }, 900);
  } catch (_) {}
});
document.addEventListener('click', () => {
  closeLogSelectMenus();
});
document.addEventListener('pointermove', (event) => {
  const point = event.target?.closest?.('.trend-point');
  if (!point) return;
  showTrendTooltip(point, event);
});
document.addEventListener('pointerout', (event) => {
  const point = event.target?.closest?.('.trend-point');
  if (!point) return;
  if (event.relatedTarget?.closest?.('.trend-point') === point) return;
  hideTrendTooltip();
});
document.addEventListener('focusin', (event) => {
  const point = event.target?.closest?.('.trend-point');
  if (!point) return;
  const rect = point.getBoundingClientRect();
  showTrendTooltip(point, { clientX: rect.left + rect.width / 2, clientY: rect.top });
});
document.addEventListener('focusout', (event) => {
  if (event.target?.closest?.('.trend-point')) hideTrendTooltip();
});
window.addEventListener('scroll', hideTrendTooltip, true);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    hideTrendTooltip();
    if (closeConfirmDialog(false)) return;
    closeLogSelectMenus();
    closeContainerLogsModal();
  }
});
$('containerLogLineSelect').addEventListener('change', () => {
  syncCustomLineInput('containerLogLineSelect', 'containerLogLineCustomInput', currentContainerLogLineLimit);
  currentContainerLogLineLimit = selectedLineCount('containerLogLineSelect', 'containerLogLineCustomInput', currentContainerLogLineLimit);
  if (currentContainerLogName && !$('containerLogsModal').hidden) {
    showContainerLogs(currentContainerLogName, currentContainerLogLineLimit);
  }
});
$('containerLogLineCustomInput').addEventListener('change', () => {
  currentContainerLogLineLimit = selectedLineCount('containerLogLineSelect', 'containerLogLineCustomInput', currentContainerLogLineLimit);
  if (currentContainerLogName && !$('containerLogsModal').hidden) {
    showContainerLogs(currentContainerLogName, currentContainerLogLineLimit);
  }
});
$('containerLogDownloadSelect').addEventListener('change', () => {
  syncCustomLineInput('containerLogDownloadSelect', 'containerLogDownloadCustomInput', currentContainerLogLineLimit);
});
function refreshOpenContainerLogs() {
  if (currentContainerLogName && !$('containerLogsModal').hidden) {
    showContainerLogs(currentContainerLogName, currentContainerLogLineLimit);
  }
}
$('containerLogFilterSelect').addEventListener('change', () => {
  syncContainerLogFilterControls();
  refreshOpenContainerLogs();
});
$('containerLogContextSelect').addEventListener('change', refreshOpenContainerLogs);
$('containerLogRegexInput').addEventListener('change', refreshOpenContainerLogs);
$('containerLogKeywordInput').addEventListener('input', () => {
  if (containerLogKeywordTimer) window.clearTimeout(containerLogKeywordTimer);
  containerLogKeywordTimer = window.setTimeout(refreshOpenContainerLogs, 350);
});
$('downloadContainerLogBtn').addEventListener('click', downloadCurrentContainerLog);
['containerLogLineSelect', 'containerLogDownloadSelect', 'containerLogFilterSelect', 'containerLogContextSelect'].forEach(enhanceLogSelect);
syncCustomLineInput('containerLogLineSelect', 'containerLogLineCustomInput', currentContainerLogLineLimit);
syncCustomLineInput('containerLogDownloadSelect', 'containerLogDownloadCustomInput', currentContainerLogLineLimit);
syncContainerLogFilterControls();
window.addEventListener('DOMContentLoaded', () => {
  setView(new URLSearchParams(window.location.search).get('view') || 'server');
});
