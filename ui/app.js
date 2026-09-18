const $ = (id) => document.getElementById(id);
let selectedProjectKey = '';
let lastProjects = [];
let lastConfig = null;
let editingProjectKey = '';
let activeView = 'deploy';
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
let projectPreflightResults = new Map();
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

function fmtTime(value) {
  if (!value) return '-';
  return value;
}

function statusLabel(status) {
  if (status === 'success') return '成功';
  if (status === 'failed') return '失败';
  if (status === 'canceled') return '已取消';
  if (status === 'running') return '部署中';
  return '等待中';
}

function shortSha(value) {
  return value ? String(value).slice(0, 8) : '-';
}

function durationClass(item) {
  if (!item || item.status === 'failed' || item.status === 'canceled') return 'failed';
  if (item.status === 'running') return 'slow';
  const seconds = Number(item.duration_seconds);
  if (!Number.isFinite(seconds)) return 'normal';
  if (seconds <= 30) return 'fast';
  if (seconds <= 90) return 'normal';
  return 'slow';
}

function durationLabel(item) {
  if (!item || item.duration_seconds == null) return '-';
  return `${item.duration_seconds}s`;
}

function phaseLabel(item) {
  if (!item || item.status !== 'running') return '';
  return item.phase_label || item.phase || '部署中';
}

function deploySubtitle(projectLabel, item) {
  if (!item || !item.after) return `${escapeHtml(projectLabel)} · 暂无部署记录`;
  const phase = phaseLabel(item);
  const parts = [
    escapeHtml(projectLabel),
    shortSha(item.after),
    durationBadge(item),
  ];
  if (phase) parts.splice(2, 0, `<span class="phase-chip">${escapeHtml(phase)}</span>`);
  return parts.join(' · ');
}

function badge(status) {
  return `<span class="badge ${status || 'neutral'}">${statusLabel(status)}</span>`;
}

function durationBadge(item) {
  const label = item && item.status === 'failed' ? '失败' : (item && item.status === 'canceled' ? '已取消' : durationLabel(item));
  return `<span class="duration ${durationClass(item)}">${label}</span>`;
}

function historyClass(item) {
  if (!item) return 'empty';
  if (item.status === 'running') return 'running';
  return durationClass(item);
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

function selectedProjectLabel(projects) {
  if (!selectedProjectKey) return '全部';
  const project = (projects || []).find(item => item.key === selectedProjectKey);
  return project ? project.name : selectedProjectKey;
}

function selectedProject(projects = []) {
  if (!selectedProjectKey) return projects.length === 1 ? projects[0] : null;
  return projects.find(item => item.key === selectedProjectKey) || null;
}

function projectState(project) {
  if (!project) return { label: '未知', level: 'neutral' };
  if (!project.enabled) return { label: '已禁用', level: 'neutral' };
  if (project.running) return { label: '部署中', level: 'running' };
  if (Number(project.queue_size || 0) > 0) return { label: '排队中', level: 'running' };
  const last = project.last_deploy || {};
  if (last.status === 'success') return { label: '最近成功', level: 'success' };
  if (last.status === 'failed') return { label: '最近失败', level: 'failed' };
  if (last.status === 'canceled') return { label: '最近取消', level: 'failed' };
  return { label: '等待部署', level: 'neutral' };
}

function projectRepoText(project) {
  return project.repo || project.project_dir || '未配置仓库地址';
}

function projectBadge(label, enabled) {
  return `<span class="badge ${enabled ? 'success' : 'neutral'}">${label}</span>`;
}

function renderProjectLock(project, lock = {}) {
  if (!lock || !lock.locked) return '';
  const lockProject = lock.project_key || '';
  if (lockProject && lockProject !== project.key) return '';
  const activePid = lock.active_pid ? `PID ${lock.active_pid}` : '无活跃进程';
  return `
    <div class="lock-status compact-lock">
      <div class="lock-main">
        <span class="badge running">部署锁定</span>
        <span>${escapeHtml(lock.phase_label || activePid)}</span>
        ${lock.duration_seconds != null ? `<span>${escapeHtml(lock.duration_seconds)}s</span>` : ''}
      </div>
      ${lock.can_force_unlock ? `<button class="danger-button compact-action" type="button" data-force-unlock-project="${escapeHtml(project.key)}">强制解锁</button>` : ''}
      ${lock.active_process ? `<code>部署进程仍在运行：${escapeHtml(activePid)}</code>` : ''}
    </div>
  `;
}

function renderProjectPreflightPanel(project, lock = {}) {
  const data = projectPreflightResults.get(project.key);
  const status = data
    ? `<span class="badge ${severityClass(data.level)}">${data.ok ? '通过' : data.level === 'critical' ? '阻塞' : '提醒'}</span>`
    : '<span class="badge neutral">未体检</span>';
  return `
    <div class="project-preflight">
      <div class="project-preflight-head">
        <div>
          <strong>部署体检</strong>
          <span>${data ? `检查于 ${escapeHtml(data.checked_at || '-')}` : '检查 Git、脚本、Docker、磁盘和日志目录'}</span>
        </div>
        <div class="project-preflight-actions">
          ${status}
          <button class="ghost-button compact-action" type="button" data-project-preflight="${escapeHtml(project.key)}">运行体检</button>
        </div>
      </div>
      ${renderProjectLock(project, lock)}
      <div class="preflight-result project-preflight-result" data-project-preflight-result data-project-key="${escapeHtml(project.key)}">
        ${data ? preflightHtml(data) : '<div class="project-empty compact-empty">点击运行体检后显示结果和修复命令。</div>'}
      </div>
    </div>
  `;
}

function renderProjectRuntimeDetails(project, agent = {}, state = {}) {
  const branch = project.branch || project.git_branch || agent.branch || '-';
  const workdir = project.project_dir || agent.project_dir || '-';
  const script = project.deploy_script || agent.deploy_script || '-';
  const webhook = project.last_webhook_at || state.last_webhook_at || '-';
  return `
    <dl class="project-runtime">
      <div><dt>目标分支</dt><dd class="mono" title="${escapeHtml(branch)}">${escapeHtml(branch)}</dd></div>
      <div><dt>项目目录</dt><dd class="mono" title="${escapeHtml(workdir)}">${escapeHtml(workdir)}</dd></div>
      <div><dt>部署脚本</dt><dd class="mono" title="${escapeHtml(script)}">${escapeHtml(script)}</dd></div>
      <div><dt>最近 WebHook</dt><dd title="${escapeHtml(fmtTime(webhook))}">${escapeHtml(fmtTime(webhook))}</dd></div>
    </dl>
  `;
}

function renderProjectOverview(projects = [], agent = {}, lock = {}, state = {}) {
  const count = projects.length || Number(agent.project_count || 0);
  const multi = count > 1;
  const modeBadge = $('projectModeBadge');
  const modeHint = $('projectModeHint');
  const list = $('projectOverview');
  if (modeBadge) {
    modeBadge.className = `badge ${multi ? 'success' : 'neutral'}`;
    modeBadge.textContent = count ? (multi ? `多项目模式 · ${count} 个` : '单项目模式') : '暂无项目';
  }
  if (modeHint) {
    modeHint.textContent = multi
      ? `已接入 ${count} 个项目，Webhook 会按仓库和分支匹配到对应部署脚本。`
      : projects.length ? '已接入 1 个项目。' : '尚未接入项目。';
  }
  if (!list) return;
  list.classList.remove('skeleton-block');
  if (!projects.length) {
    list.innerHTML = '<div class="project-empty">暂无项目</div>';
    return;
  }
  list.innerHTML = projects.map(project => {
    const state = projectState(project);
    const selected = selectedProjectKey === project.key || (!selectedProjectKey && projects.length === 1);
    const last = project.last_deploy || {};
    const head = project.short_head || shortSha(project.head);
    return `
      <article class="project-row ${selected ? 'active' : ''}" data-project-key="${escapeHtml(project.key)}">
        <button class="project-select-area" type="button" data-project-select="${escapeHtml(project.key)}" aria-label="选择 ${escapeHtml(project.name || project.key)}">
          <div class="project-title-line">
            <span class="project-name">${escapeHtml(project.name || project.key)}</span>
            <span class="badge ${state.level}">${state.label}</span>
          </div>
          <div class="project-repo mono" title="${escapeHtml(projectRepoText(project))}">${escapeHtml(projectRepoText(project))}</div>
        </button>
        <div class="project-tags">
          ${projectBadge(project.enabled ? '启用' : '禁用', project.enabled)}
          ${projectBadge(project.manual_deploy_enabled ? '手动部署' : '禁止手动', project.manual_deploy_enabled)}
          ${projectBadge(project.rollback_available ? '可回滚' : '无回滚', project.rollback_available)}
        </div>
        <div class="project-meta">
          <span>分支 <strong>${escapeHtml(project.branch || '-')}</strong></span>
          <span>HEAD <strong class="mono">${escapeHtml(head || '-')}</strong></span>
          <span>最近 <strong>${escapeHtml(last.finished_at || last.started_at || '-')}</strong></span>
        </div>
        ${renderProjectRuntimeDetails(project, agent, state)}
        ${renderProjectPreflightPanel(project, lock)}
      </article>
    `;
  }).join('');
  list.querySelectorAll('[data-project-select]').forEach(row => {
    row.addEventListener('click', () => {
      selectedProjectKey = row.dataset.projectSelect || '';
      updateProjectSelect(projects);
      renderLogs();
      refresh();
    });
  });
  list.querySelectorAll('[data-project-preflight]').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      runPreflight(button.dataset.projectPreflight || '');
    });
  });
  list.querySelectorAll('[data-force-unlock-project]').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      selectedProjectKey = button.dataset.forceUnlockProject || selectedProjectKey;
      forceUnlockDeploy();
    });
  });
}

function updateProjectActionHint(activeProject, projects = []) {
  const hint = $('projectActionHint');
  if (!hint) return;
  if (!activeProject) {
    hint.textContent = projects.length > 1
      ? '选择具体项目后可重新部署或回滚；全部项目用于总览。'
      : '尚未接入项目。';
    return;
  }
  if (!activeProject.enabled) {
    hint.textContent = '当前项目已禁用，不会响应 WebHook，也不能手动部署。';
    return;
  }
  if (!activeProject.manual_deploy_enabled) {
    hint.textContent = '当前项目允许 WebHook 自动部署，但禁用了手动操作。';
    return;
  }
  hint.textContent = activeProject.rollback_available
    ? '当前项目支持手动重新部署和回滚。'
    : '当前项目支持手动重新部署，暂无可回滚的历史版本。';
}

function updateProjectSelect(projects = []) {
  const button = $('projectSelectButton');
  const label = $('projectSelectLabel');
  const menu = $('projectSelectMenu');
  if (!button || !label || !menu) return;
  const known = new Set(projects.map(item => item.key));
  if (selectedProjectKey && !known.has(selectedProjectKey)) selectedProjectKey = '';
  label.textContent = selectedProjectLabel(projects);
  button.title = selectedProjectKey ? selectedProjectLabel(projects) : '全部项目';
  menu.innerHTML = [
    `<button type="button" class="project-option ${selectedProjectKey ? '' : 'active'}" role="option" data-project-key="" aria-selected="${selectedProjectKey ? 'false' : 'true'}">
      <span>全部</span><small>总览</small>
    </button>`,
    ...projects.map(project => (
      `<button type="button" class="project-option ${selectedProjectKey === project.key ? 'active' : ''}" role="option" data-project-key="${escapeHtml(project.key)}" aria-selected="${selectedProjectKey === project.key ? 'true' : 'false'}">
        <span>${escapeHtml(project.name || project.key)}</span><small>${escapeHtml(project.branch || '-')}</small>
      </button>`
    ))
  ].join('');
  menu.querySelectorAll('.project-option').forEach(option => {
    option.addEventListener('click', () => {
      selectedProjectKey = option.dataset.projectKey || '';
      closeProjectMenu();
      updateProjectSelect(projects);
      renderLogs();
      refresh();
    });
  });
}

function openProjectMenu() {
  const button = $('projectSelectButton');
  const menu = $('projectSelectMenu');
  if (!button || !menu) return;
  menu.hidden = false;
  button.setAttribute('aria-expanded', 'true');
}

function closeProjectMenu() {
  const button = $('projectSelectButton');
  const menu = $('projectSelectMenu');
  if (!button || !menu) return;
  menu.hidden = true;
  button.setAttribute('aria-expanded', 'false');
}

function toggleProjectMenu() {
  const menu = $('projectSelectMenu');
  if (!menu) return;
  if (menu.hidden) openProjectMenu();
  else closeProjectMenu();
}

function filterBySelectedProject(items) {
  if (!selectedProjectKey) return items;
  return items.filter(item => {
    if (!item) return false;
    return (item.project_key || 'default') === selectedProjectKey;
  });
}

function itemTitle(item) {
  if (!item) return '暂无部署';
  const parts = [statusLabel(item.status)];
  if (item.after) parts.push(shortSha(item.after));
  if (item.duration_seconds != null) parts.push(durationLabel(item));
  if (item.started_at) parts.push(fmtTime(item.started_at));
  return parts.join(' · ');
}

function detailValue(value) {
  const text = value == null || value === '' ? '-' : value;
  return escapeHtml(text);
}

function successSummary(items) {
  const finished = items.filter(item => item && item.status !== 'running');
  const success = finished.filter(item => item.status === 'success').length;
  const total = finished.length;
  const percent = total ? Math.round((success / total) * 1000) / 10 : null;
  let level = 'success';
  if (percent == null) level = 'neutral';
  else if (percent < 80) level = 'failed';
  else if (percent < 95) level = 'running';
  return { success, total, percent, level };
}

function renderHistoryBars(items, options = {}) {
  const bars = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!bars.length) {
    return '<div class="history-empty-label">暂无历史</div>';
  }
  return bars.map((item, index) => {
    const detailKey = options.linkDeploy ? deployDetailKey(item, index) : '';
    return `
    <button
      type="button"
      class="history-bar ${historyClass(item)}"
      title="${escapeHtml(itemTitle(item))}"
      aria-label="${escapeHtml(itemTitle(item))}"
      ${detailKey ? `data-detail-key="${escapeHtml(detailKey)}"` : ''}>
    </button>
  `;
  }).join('');
}

function historyTrackStyle(items) {
  const count = Math.max(1, (Array.isArray(items) ? items.filter(Boolean).length : 0));
  const minWidth = Math.max(0, (count * 6.75) - 2);
  return `--history-count:${count};--history-min-width:${minWidth}px`;
}

function findDeployDetailByKey(detailKey) {
  if (!detailKey) return null;
  return Array.from(document.querySelectorAll('#deployStats .deploy-details[data-detail-key]'))
    .find(el => el.dataset.detailKey === detailKey) || null;
}

function focusDeployRecord(detailKey) {
  const detail = findDeployDetailByKey(detailKey);
  if (!detail) return;
  const card = detail.closest('.deploy-stat-card');
  if (!card) return;
  detail.open = true;
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  card.classList.remove('deploy-stat-card-focused');
  window.requestAnimationFrame(() => {
    card.classList.add('deploy-stat-card-focused');
    window.setTimeout(() => card.classList.remove('deploy-stat-card-focused'), 1600);
  });
}

function renderMiniHistory(id, items) {
  const target = $(id);
  if (!target) return;
  target.innerHTML = renderHistoryBars(items);
}

function statusSeries(status, count = 1) {
  return Array.from({ length: Math.max(0, Math.min(60, count)) }, () => ({
    status,
    after: '',
    started_at: '',
    duration_seconds: null
  }));
}

function deployDetailKey(item, index) {
  return [
    item.started_at || '',
    item.before || '',
    item.after || '',
    index
  ].join('|');
}

function collectClosedDeployDetails() {
  return new Set(
    Array.from(document.querySelectorAll('#deployStats .deploy-details[data-detail-key]:not([open])'))
      .map(el => el.dataset.detailKey)
      .filter(Boolean)
  );
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
  activeView = ['server', 'events', 'notify', 'certificates'].includes(view) ? view : 'deploy';
  ['deploy', 'server', 'events', 'notify', 'certificates'].forEach(name => {
    $(`${name}View`)?.classList.toggle('active', activeView === name);
    $(`${name}ViewTab`)?.classList.toggle('active', activeView === name);
  });
  const titles = {
    deploy: ['部署健康面板', '监控 Git 平台 WebHook、部署队列、最近发布和线上健康检查。'],
    server: ['服务器健康面板', '查看服务器资源、网络吞吐和 Docker 容器运行状态。'],
    events: ['告警与事件', '集中查看部署、WebHook、容器和资源异常。'],
    notify: ['通知配置', '配置部署结果、服务器和 Docker 异常的推送渠道。'],
    certificates: ['域名与证书', '项目访问入口、证书有效期与 HTTPS 状态。'],
  };
  const [title, subtitle] = titles[activeView];
  $('panelTitle').textContent = title;
  $('panelSubtitle').textContent = subtitle;
  clearRefresh();
  clearServerRefresh();
  clearEventsRefresh();
  if (activeView === 'server') refreshServerStatus();
  else if (activeView === 'events') refreshEventsStatus();
  else if (activeView === 'notify') refreshNotificationConfig();
  else if (activeView === 'certificates') refreshCertificates();
  else refresh();
}


function renderDeployCard(item, index, historyItems, refreshSeconds, closedDetailKeys) {
  const summary = successSummary(historyItems);
  const projectTitle = item.project_name || item.project_key || '默认项目';
  const title = item.status === 'running' ? `${projectTitle} 部署中` : `${projectTitle} · ${shortSha(item.after)}`;
  const finishedText = item.finished_at ? fmtTime(item.finished_at) : '进行中';
  const percentText = summary.percent == null ? '-' : `${summary.percent}%`;
  const statusClass = item.status || 'neutral';
  const phase = phaseLabel(item);
  const phaseDetail = item.phase_detail || '';
  const detailKey = deployDetailKey(item, index);
  const detailsOpen = closedDetailKeys.has(detailKey) ? '' : ' open';
  return `
    <article class="deploy-stat-card">
      <span class="corner-mark left">⌜</span>
      <span class="corner-mark right">⌝</span>
      <div class="stat-main">
        <div class="stat-title">${escapeHtml(title)}</div>
        <div class="stat-identity">
          <div class="stat-icon">CI</div>
          <div class="stat-meta">
            <span class="stat-pill">Git</span>
            <span class="stat-pill">${escapeHtml(projectTitle)}</span>
            <span class="mono">${escapeHtml(shortSha(item.after))}</span>
            <span class="badge ${statusClass}">${statusLabel(item.status)}</span>
          </div>
        </div>
        ${phase ? `
          <div class="deploy-phase-line">
            <span class="phase-dot"></span>
            <span>${escapeHtml(phase)}</span>
            <small>${escapeHtml(phaseDetail || `已用 ${durationLabel(item)}`)}</small>
          </div>
        ` : ''}
        <div class="stat-metrics">
          <div class="metric-box">
            <div class="metric-label">Deploy Time</div>
            <div class="metric-value">${escapeHtml(durationLabel(item))}</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">Exit Code</div>
            <div class="metric-value">${escapeHtml(item.exit_code ?? '-')}</div>
          </div>
        </div>
        <div class="availability-box">
          <div class="availability-inner">
            <div>
              <div class="availability-label">Availability</div>
              <div class="availability-sub">最近 ${summary.total || 0} 次，成功 ${summary.success || 0} 次</div>
            </div>
            <div class="availability-score badge ${summary.level}">${percentText}</div>
          </div>
        </div>
        <details class="deploy-details" data-detail-key="${escapeHtml(detailKey)}"${detailsOpen}>
          <summary>部署详情</summary>
          <div class="deploy-time-row">
            <div><span>Started</span><strong title="${detailValue(item.started_at)}">${detailValue(item.started_at)}</strong></div>
            <div><span>Finished</span><strong title="${detailValue(item.finished_at)}">${detailValue(item.finished_at)}</strong></div>
          </div>
          <dl class="deploy-detail-list">
            <div><dt>Source</dt><dd title="${detailValue(item.source)}">${detailValue(item.source)}</dd></div>
            <div><dt>Actor</dt><dd title="${detailValue(item.actor)}">${detailValue(item.actor)}</dd></div>
            <div><dt>Before</dt><dd class="mono" title="${detailValue(item.before)}">${detailValue(shortSha(item.before))}</dd></div>
            <div><dt>After</dt><dd class="mono" title="${detailValue(item.after)}">${detailValue(shortSha(item.after))}</dd></div>
            <div><dt>Author</dt><dd title="${detailValue(item.commit_author)}">${detailValue(item.commit_author)}</dd></div>
            <div><dt>Ref</dt><dd title="${detailValue(item.ref)}">${detailValue(item.ref)}</dd></div>
            <div class="wide-detail deploy-message-detail"><dt>Message</dt><dd title="${detailValue(item.commit_message)}">${detailValue(item.commit_message)}</dd></div>
            <div class="wide-detail deploy-insight-detail"><dt>执行摘要</dt><dd>${renderDeployDetailInsights(item)}</dd></div>
          </dl>
        </details>
      </div>
      <div class="stat-history">
        <div class="history-head">
          <span>History (${historyItems.filter(Boolean).length}pts)</span>
          <span class="history-next">Next update in ${refreshSeconds}s</span>
        </div>
        <div class="history-bars">
          <div class="history-track" style="${historyTrackStyle(historyItems)}">${renderHistoryBars(historyItems, { linkDeploy: true })}</div>
        </div>
        <div class="history-axis">
          <span>Oldest</span>
          <span>${escapeHtml(finishedText)}</span>
        </div>
      </div>
    </article>
  `;
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
  if (!res.ok) throw new Error(data.error || `${path} ${res.status}`);
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
  if (!res.ok) throw new Error(data.detail || data.error || `${path} ${res.status}`);
  return data;
}

function severityClass(level) {
  if (level === 'critical' || level === 'failed') return 'failed';
  if (level === 'warning' || level === 'running') return 'running';
  if (level === 'success' || level === 'ok') return 'success';
  return 'neutral';
}

function preflightTarget(projectKey) {
  return Array.from(document.querySelectorAll('[data-project-preflight-result]'))
    .find(item => item.dataset.projectKey === String(projectKey || ''));
}

function preflightHtml(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const level = data?.level || 'unknown';
  const badgeClass = severityClass(level);
  const label = level === 'critical'
    ? '阻塞'
    : level === 'warning'
      ? '提醒'
      : level === 'ok'
        ? '通过'
        : '未知';
  if (!items.length) {
    return `<div class="preflight-head"><span class="badge ${badgeClass}">${label}</span></div>`;
  }
  return `
    <div class="preflight-head">
      <span class="badge ${badgeClass}">${label}</span>
      <strong>${escapeHtml(data.project_name || data.project || '-')}</strong>
      <span>${items.length} 项检查</span>
    </div>
    <div class="preflight-items">
      ${items.map(item => `
        <div class="preflight-item ${severityClass(item.level)}">
          <div>
            <strong>${escapeHtml(item.title || '-')}</strong>
            <span>${escapeHtml(item.detail || '')}</span>
          </div>
          ${item.command ? `<code class="copy-command" title="点击复制">${escapeHtml(item.command)}</code>` : ''}
        </div>
      `).join('')}
    </div>
  `;
}

function renderPreflightData(data, projectKey = selectedProjectKey) {
  const target = $('preflightResult');
  const html = preflightHtml(data);
  if (target) target.innerHTML = html;
  const key = data?.project || projectKey || '';
  if (key) {
    projectPreflightResults.set(key, data);
    const projectTarget = preflightTarget(key);
    if (projectTarget) projectTarget.innerHTML = html;
  }
}

async function fetchPreflightData(projectKey = selectedProjectKey) {
  const projectParam = projectKey ? `?project=${encodeURIComponent(projectKey)}` : '';
  const data = await fetchJson(`preflight${projectParam}`);
  renderPreflightData(data, projectKey);
  return data;
}

async function runPreflight(projectKey = selectedProjectKey) {
  const button = $('preflightBtn');
  const oldText = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = '体检中';
  }
  const target = $('preflightResult');
  if (target) target.textContent = '正在检查项目目录、Git、部署脚本、Docker、磁盘和日志目录...';
  const projectTarget = preflightTarget(projectKey);
  if (projectTarget) projectTarget.innerHTML = '<div class="project-empty compact-empty">体检中...</div>';
  try {
    await fetchPreflightData(projectKey);
  } catch (err) {
    if (target) {
      target.innerHTML = `<div class="preflight-item failed"><strong>体检失败</strong><span>${escapeHtml(err.message)}</span></div>`;
    }
    if (projectTarget) {
      projectTarget.innerHTML = `<div class="preflight-item failed"><strong>体检失败</strong><span>${escapeHtml(err.message)}</span></div>`;
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText || '运行体检';
    }
  }
}

async function forceUnlockDeploy() {
  const confirmed = await showConfirmDialog({
    title: '强制解除部署锁',
    message: '只有确认没有部署脚本在运行时才建议强制解锁。确认继续？',
    confirmText: '强制解锁',
  });
  if (!confirmed) return;
  try {
    const result = await postJson('force-unlock');
    const target = $('preflightResult');
    if (target) target.textContent = result.unlocked ? '部署锁已解除。' : '当前没有可解除的部署锁。';
    const projectTarget = preflightTarget(selectedProjectKey || String(result.project_key || ''));
    if (projectTarget) {
      projectTarget.innerHTML = `<div class="preflight-item success"><strong>已处理</strong><span>${escapeHtml(result.message || '部署锁已清理')}</span></div>`;
    }
    await refresh();
  } catch (err) {
    const target = $('preflightResult');
    if (target) target.textContent = `强制解锁失败: ${err.message}`;
    const projectTarget = preflightTarget(selectedProjectKey || '');
    if (projectTarget) {
      projectTarget.innerHTML = `<div class="preflight-item failed"><strong>无法解锁</strong><span>${escapeHtml(err.message)}</span></div>`;
    }
  }
}

function projectByKey(key) {
  return (lastProjects || []).find(project => project.key === key) || null;
}

function webhookBaseUrl() {
  const path = location.pathname.startsWith('/deploy/') ? '/deploy/webhook' : 'webhook';
  return new URL(path, location.origin + (path.startsWith('/') ? '' : location.pathname.replace(/[^/]*$/, ''))).toString();
}

function projectWebhookUrl(projectKey) {
  const url = new URL(webhookBaseUrl());
  if (projectKey) url.searchParams.set('project', projectKey);
  return url.toString();
}

function shellQuote(value) {
  const text = String(value || '');
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function dirname(path, fallback = '.') {
  const text = String(path || '');
  if (!text.includes('/')) return fallback;
  return text.replace(/\/[^/]*$/, '') || '/';
}

function normalizedProjectKeyFromForm(project) {
  return String(project.key || project.name || '')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'project-key';
}

function projectTemplateLabel(template) {
  return ({
    docker: 'Docker Compose',
    python: 'Python / systemd',
    node: 'Node / PM2',
    java: 'Java / systemd',
    go: 'Go / systemd',
    static: '静态网站',
    custom: '自定义脚本',
  })[template] || '自定义脚本';
}

function deployStepsForTemplate(template, project) {
  const service = project.service_name || normalizedProjectKeyFromForm(project);
  const healthUrl = project.health_url || '';
  const healthLine = healthUrl ? [`curl -fsS --max-time 10 ${shellQuote(healthUrl)}`] : ['# 可选：填写健康检查 URL 后这里会自动检查'];
  const map = {
    docker: [
      'docker compose up -d --build',
      'docker compose ps',
      ...healthLine,
    ],
    node: [
      'if command -v pnpm >/dev/null 2>&1; then pnpm install --frozen-lockfile; else npm ci; fi',
      'if [ -f package.json ]; then npm run build --if-present; fi',
      `pm2 restart ${shellQuote(service)} || pm2 start npm --name ${shellQuote(service)} -- start`,
      ...healthLine,
    ],
    python: [
      'python3 -m venv .venv',
      '. .venv/bin/activate',
      'pip install --upgrade pip',
      'pip install -r requirements.txt',
      `systemctl restart ${shellQuote(service)}`,
      ...healthLine,
    ],
    java: [
      'if [ -f ./gradlew ]; then bash ./gradlew clean build -x test; jar_dir=build/libs; elif [ -f ./mvnw ]; then bash ./mvnw clean package -DskipTests; jar_dir=target; else mvn clean package -DskipTests; jar_dir=target; fi',
      'mkdir -p target/deploy',
      'mapfile -t jars < <(find "$jar_dir" -maxdepth 1 -type f -name \'*.jar\' ! -name \'*sources.jar\' ! -name \'*javadoc.jar\' ! -name \'*-plain.jar\')',
      'if [ "${#jars[@]}" -ne 1 ]; then echo "需要唯一的可执行 JAR，请检查构建产物"; exit 1; fi',
      'cp "${jars[0]}" target/deploy/app.jar',
      `# 确认 /etc/systemd/system/${service}.service 已按你的 Java 项目配置好`,
      `systemctl restart ${shellQuote(service)}`,
      ...healthLine,
    ],
    go: [
      'mkdir -p bin',
      'if [ -d cmd/server ]; then go build -o bin/app ./cmd/server; else go build -o bin/app .; fi',
      `# 确认 /etc/systemd/system/${service}.service 已按你的 Go 项目配置好`,
      `systemctl restart ${shellQuote(service)}`,
      ...healthLine,
    ],
    static: [
      'if command -v pnpm >/dev/null 2>&1; then pnpm install --frozen-lockfile; else npm ci; fi',
      'npm run build',
      '# TODO: 把 dist/ 同步到你的 Nginx 静态目录，例如：',
      `# rsync -a --delete dist/ /var/www/${service}/`,
      ...healthLine,
    ],
    custom: [
      '# TODO: 在这里填写项目自己的部署步骤',
      '# 示例：docker compose up -d --build',
      ...healthLine,
    ],
  };
  return map[template] || map.custom;
}

function applyTemplateDefaults(force = false) {
  const template = $('projectTemplateInput') ? $('projectTemplateInput').value : 'custom';
  const key = normalizedProjectKeyFromForm(collectProjectForm());
  const workdir = $('projectWorkdirInput');
  const script = $('projectScriptInput');
  const log = $('projectLogInput');
  const serviceName = $('projectServiceNameInput');
  const servicePort = $('projectServicePortInput');
  const defaultValue = (field, value) => {
    if (field && (force || !field.value || field.value === field.dataset.autoValue)) {
      field.value = value;
      field.dataset.autoValue = value;
    }
  };
  defaultValue(workdir, `/srv/${key}`);
  defaultValue(script, `${workdir.value}/deploy/deploy.sh`);
  defaultValue(log, `/var/log/mini_deploy/${key}-deploy.log`);
  defaultValue(serviceName, key);
  defaultValue(servicePort, template === 'java' ? '8003' : template === 'go' ? '8002' : '8001');
  globalThis.AppSelects?.syncAll();
}

let projectGuidanceVersion = 0;
let projectPreviewSignature = '';

function invalidateProjectPreview() {
  projectPreviewSignature = '';
  $('projectConfigConfirmed').checked = false;
  $('projectConfigConfirmed').disabled = true;
  $('projectFilePreview').replaceChildren();
}

function renderFailureAdvice(items = []) {
  if (!items.length) return '';
  return `<div class="failure-advice"><strong>可能原因与排查建议</strong>${items.map(item =>
    `<p><b>${escapeHtml(item.title || '')}</b><br>${escapeHtml(item.advice || '')}</p>`).join('')}</div>`;
}

async function inspectCurrentProject() {
  const project = collectProjectForm();
  if (!project.repo.trim()) {
    setProjectFormError('请先填写仓库地址和部署分支。');
    return;
  }
  setProjectFormError('');
  const version = ++projectGuidanceVersion;
  $('previewProjectBtn').disabled = false;
  const button = $('inspectProjectBtn');
  button.disabled = true;
  $('projectInspectStatus').textContent = '正在识别，最长约 60 秒…';
  const target = $('projectDetectionResult');
  target.hidden = false;
  target.replaceChildren();
  try {
    const result = await postJsonBody('projects-config/inspect', {project});
    if (version !== projectGuidanceVersion || $('projectModal').hidden) return;
    const current = collectProjectForm();
    if (current.repo !== project.repo || current.branch !== project.branch) {
      $('projectInspectStatus').textContent = '仓库或分支已更改，请重新识别。';
      return;
    }
    $('projectInspectStatus').textContent = result.ok ? '识别完成' : '无法读取仓库';
    if (!result.ok) {
      target.innerHTML = renderFailureAdvice(result.diagnosis);
      return;
    }
    const candidates = result.candidates || [];
    target.innerHTML = `${candidates.length ? `
      <label>检测到的部署方式<select id="projectDetectedTemplate">${candidates.map((item, index) =>
        `<option value="${index}">${escapeHtml(item.label)} (${escapeHtml(item.evidence.join(', '))})</option>`).join('')}</select></label>
      <button id="applyProjectDetectionBtn" class="ghost-button compact-action" type="button">采用此配置</button>` : ''}
      ${(result.warnings || []).map(text => `<p class="onboarding-note">${escapeHtml(text)}</p>`).join('')}`;
    $('applyProjectDetectionBtn')?.addEventListener('click', () => {
      if (collectProjectForm().repo !== project.repo || collectProjectForm().branch !== project.branch) {
        setProjectFormError('仓库或分支已更改，请重新识别。');
        return;
      }
      const candidate = candidates[Number($('projectDetectedTemplate').value)];
      $('projectTemplateInput').value = candidate.template;
      applyTemplateDefaults(false);
      if (!$('projectStartCommandInput').value && candidate.template === 'python' && candidate.entry) {
        $('projectStartCommandInput').value = `${$('projectWorkdirInput').value}/.venv/bin/uvicorn ${candidate.entry} --host 127.0.0.1 --port ${$('projectServicePortInput').value}`;
      }
      if (result.existing_script) {
        const script = $('projectScriptInput');
        if (!script.value || script.value === script.dataset.autoValue) {
          script.value = `${$('projectWorkdirInput').value}/${result.existing_script}`;
          delete script.dataset.autoValue;
        }
      }
      invalidateProjectPreview();
      updateWebhookPreview();
      $('projectInspectStatus').textContent = '已采用，请核对部署配置。';
    });
  } catch (err) {
    if (version === projectGuidanceVersion) $('projectInspectStatus').textContent = err.message;
  } finally {
    if (version === projectGuidanceVersion) button.disabled = false;
  }
}

async function previewCurrentProject() {
  if (!$('projectForm').reportValidity()) return;
  const project = collectProjectForm();
  const signature = JSON.stringify(project);
  const version = projectGuidanceVersion;
  const button = $('previewProjectBtn');
  button.disabled = true;
  invalidateProjectPreview();
  $('projectFilePreview').textContent = '正在生成预览…';
  try {
    const result = await postJsonBody('projects-config/preview', {project});
    if (version !== projectGuidanceVersion || $('projectModal').hidden) return;
    if (JSON.stringify(collectProjectForm()) !== signature) {
      $('projectFilePreview').textContent = '配置已更改，请重新预览。';
      return;
    }
    $('projectFilePreview').innerHTML = '<p class="onboarding-note">以下为服务器当前文件或初版模板。拉取代码后如出现同名文件，将保留仓库版本；初始化后可再次预览核对。</p>' + (result.files || []).map(file => `
      <details open class="onboarding-file"><summary>${escapeHtml(file.path)} · ${file.exists ? '保留现有文件' : '将生成'}</summary>
        <pre class="command-output">${escapeHtml(file.content)}</pre></details>`).join('');
    projectPreviewSignature = signature;
    $('projectConfigConfirmed').disabled = false;
  } catch (err) {
    if (version === projectGuidanceVersion) $('projectFilePreview').textContent = err.message;
  } finally {
    if (version === projectGuidanceVersion) button.disabled = false;
  }
}

function defaultStartCommand(project) {
  const key = normalizedProjectKeyFromForm(project);
  const servicePort = Number(project.service_port || 8000);
  const workdir = project.workdir || `/srv/${key}`;
  if (project.start_command) return project.start_command;
  if (project.template === 'python') return `${workdir}/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${servicePort || 8000}`;
  if (project.template === 'go') return `${workdir}/bin/app`;
  if (project.template === 'java') return `/usr/bin/java -jar ${workdir}/target/deploy/app.jar --server.port=${servicePort || 8000}`;
  return '';
}

function systemdServiceText(project) {
  const key = normalizedProjectKeyFromForm(project);
  const service = project.service_name || key;
  const command = defaultStartCommand(project);
  if (!command) return '';
  return [
    '[Unit]',
    `Description=${project.name || key} service`,
    'After=network.target',
    '',
    '[Service]',
    'Type=simple',
    `WorkingDirectory=${project.workdir || `/srv/${key}`}`,
    `ExecStart=${command}`,
    'Restart=always',
    'RestartSec=3',
    'KillSignal=SIGINT',
    '',
    '[Install]',
    'WantedBy=multi-user.target',
  ].join('\n');
}

function generateSetupCommands(project) {
  const key = normalizedProjectKeyFromForm(project);
  const repo = project.repo || '<填写仓库 SSH 地址，例如 git@gitee.com:org/repo.git>';
  const branch = project.branch || 'main';
  const template = project.template || 'custom';
  const workdir = project.workdir || `/srv/${key}`;
  const script = project.script || `${workdir}/deploy/deploy.sh`;
  const logFile = project.deploy_log_file || `/var/log/${key}-deploy.log`;
  const scriptDir = dirname(script, '.');
  const logDir = dirname(logFile, '/var/log');
  const deploySteps = deployStepsForTemplate(template, project);
  const service = project.service_name || key;
  const serviceText = systemdServiceText({ ...project, workdir, service_name: service });
  const appDomain = String(project.app_domain || '').replace(/^https?:\/\//, '').split('/')[0].split(':')[0];
  const serviceCommands = serviceText ? [
    '',
    '# 3. 可选：生成 systemd 初版服务文件；如果启动命令不符合你的项目，请先修改',
    `cat > /etc/systemd/system/${service}.service <<'SERVICE'`,
    serviceText,
    'SERVICE',
    'systemctl daemon-reload',
    `systemctl enable ${shellQuote(service)}`,
  ] : [
    '',
    '# 3. 如果项目通过 systemd 运行，请确认对应 service 已存在且能手动 restart',
    `# 示例检查：systemctl status ${shellQuote(key)}`,
  ];
  const nginxCommands = appDomain ? [
    '',
    '# 4. 可选：生成业务域名 Nginx 反向代理；面板里的“配置业务域名”按钮也会做这件事',
    'command -v nginx >/dev/null 2>&1 || { echo "未安装 nginx，请先安装 nginx"; exit 1; }',
    `cat > /etc/nginx/conf.d/mini-deploy-${key}.conf <<'NGINX'`,
    'server {',
    '    listen 80;',
    `    server_name ${appDomain};`,
    '',
    '    client_max_body_size 50m;',
    '    location / {',
    `        proxy_pass http://127.0.0.1:${Number(project.service_port || 8000)};`,
    '        proxy_http_version 1.1;',
    '        proxy_set_header Host $host;',
    '        proxy_set_header X-Real-IP $remote_addr;',
    '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
    '        proxy_set_header X-Forwarded-Proto $scheme;',
    '        proxy_set_header Upgrade $http_upgrade;',
    '        proxy_set_header Connection "upgrade";',
    '    }',
    '}',
    'NGINX',
    'nginx -t',
    'systemctl reload nginx || systemctl restart nginx',
    project.app_https ? `certbot --nginx -d ${shellQuote(appDomain)}` : '# 如需 HTTPS：certbot --nginx -d 你的业务域名',
  ] : [];
  return [
    `# 项目类型：${projectTemplateLabel(template)}`,
    '# 0. 检查服务器是否能访问仓库',
    'command -v git >/dev/null 2>&1 || { echo "未安装 git，请先安装 git"; exit 1; }',
    `git ls-remote ${shellQuote(repo)} HEAD >/dev/null || {`,
    '  echo "服务器无法访问仓库。请先把服务器 SSH 公钥添加到代码平台 Deploy Key / SSH Key。";',
    '  echo "查看服务器公钥：cat ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null";',
    '  echo "如果没有公钥：ssh-keygen -t ed25519 -C deploy@$(hostname)";',
    '  exit 1;',
    '}',
    '',
    '# 1. 准备项目目录和代码',
    `mkdir -p ${shellQuote(workdir)}`,
    `if [ ! -d ${shellQuote(workdir + '/.git')} ]; then`,
    `  git clone --branch ${shellQuote(branch)} ${shellQuote(repo)} ${shellQuote(workdir)}`,
    'else',
    `  git -C ${shellQuote(workdir)} fetch origin ${shellQuote(branch)}`,
    `  git -C ${shellQuote(workdir)} checkout ${shellQuote(branch)}`,
    `  git -C ${shellQuote(workdir)} pull --ff-only origin ${shellQuote(branch)}`,
    'fi',
    '',
    '# 2. 准备部署脚本；下面只是初版模板，启用自动部署前请按你的业务项目检查',
    `mkdir -p ${shellQuote(scriptDir)}`,
    `cat > ${shellQuote(script)} <<'SH'`,
    '#!/usr/bin/env bash',
    'set -Eeuo pipefail',
    `cd ${shellQuote(workdir)}`,
    `git fetch origin ${shellQuote(branch)}`,
    `git checkout ${shellQuote(branch)}`,
    `git pull --ff-only origin ${shellQuote(branch)}`,
    '',
    ...deploySteps,
    '',
    'SH',
    `chmod +x ${shellQuote(script)}`,
    `mkdir -p ${shellQuote(logDir)}`,
    `touch ${shellQuote(logFile)}`,
    ...serviceCommands,
    ...nginxCommands,
    '',
    '# 5. 回到部署面板保存项目配置；再到 Gitee/GitHub/GitLab WebHook 填写页面给出的 URL 和 Token',
  ].join('\n');
}

function generateWebhookGuide(project) {
  const key = normalizedProjectKeyFromForm(project);
  const token = project.webhook_secret || '<保存项目后自动生成 Token>';
  return [
    'WebHook 配置（不要把 Token 拼进 URL）：',
    `URL: ${projectWebhookUrl(key)}`,
    '请求方式: POST',
    '触发事件: Push',
    `分支: ${project.branch || 'master'}`,
    '',
    `Gitee 密码/Token: ${token}`,
    `GitHub Secret: ${token}`,
    `GitLab Secret token: ${token}`,
  ].join('\n');
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

async function copyFromElement(elementId, buttonId, doneText = '已复制') {
  const button = $(buttonId);
  const oldText = button ? button.textContent : '';
  await copyText($(elementId).textContent);
  if (!button) return;
  button.textContent = doneText;
  window.setTimeout(() => {
    button.textContent = oldText;
  }, 1200);
}

function setProjectFormError(message = '') {
  const target = $('projectFormError');
  if (!target) return;
  target.hidden = !message;
  target.textContent = message;
  target.classList.toggle('success-note', message.startsWith('保存成功'));
}

function renderDoctorResult(payload) {
  const target = $('projectDoctorResult');
  if (!target) return;
  const checks = payload.checks || [];
  if (!checks.length) {
    target.textContent = '暂无体检结果。';
    return;
  }
  target.innerHTML = checks.map(item => {
    const level = item.ok ? 'ok' : (item.level || 'fail');
    const icon = item.ok ? '✓' : (level === 'warn' ? '!' : '×');
    return `
      <div class="doctor-item ${escapeHtml(level)}">
        <div class="doctor-icon">${escapeHtml(icon)}</div>
        <div>
          <strong>${escapeHtml(item.title || item.key || '-')}</strong>
          <span>${escapeHtml(item.message || '')}</span>
        </div>
      </div>
    `;
  }).join('');
}

function renderBootstrapResult(payload) {
  const target = $('projectDoctorResult');
  if (!target) return;
  const bootstrap = payload.bootstrap || payload;
  const results = Array.isArray(bootstrap.results) ? bootstrap.results : [];
  const sshKeys = Array.isArray(bootstrap.ssh_public_keys) ? bootstrap.ssh_public_keys : [];
  const statusClass = bootstrap.ok ? 'ok' : 'fail';
  const statusText = bootstrap.ok ? '自动初始化完成' : '自动初始化未完成';
  target.innerHTML = `
    <div class="doctor-item ${statusClass}">
      <div class="doctor-icon">${bootstrap.ok ? '✓' : '×'}</div>
      <div>
        <strong>${escapeHtml(statusText)}</strong>
        <span>${escapeHtml(bootstrap.message || (bootstrap.service_written ? `已生成 ${bootstrap.service_name}.service` : '已执行服务器初始化步骤'))}</span>
      </div>
    </div>
    ${results.map(item => `
      <div class="doctor-item ${item.ok ? 'ok' : 'fail'}">
        <div class="doctor-icon">${item.ok ? '✓' : '×'}</div>
        <div>
          <strong>${escapeHtml(item.step || '-')}</strong>
          <span>${escapeHtml(item.detail || item.output || '')}</span>
          ${renderFailureAdvice(item.diagnosis)}
          ${item.output ? `<details><summary>原始输出</summary><pre class="command-output">${escapeHtml(item.output)}</pre></details>` : ''}
        </div>
      </div>
    `).join('')}
    ${sshKeys.length ? `
      <div class="command-box bootstrap-key-box">
        <div class="webhook-label">服务器 SSH 公钥，复制到代码平台 Deploy Key / SSH Key</div>
        <pre class="command-output">${escapeHtml(sshKeys.join('\n'))}</pre>
      </div>
    ` : ''}
  `;
}

function renderNginxResult(payload) {
  const target = $('projectDoctorResult');
  if (!target) return;
  const nginx = payload.nginx || payload;
  const results = Array.isArray(nginx.results) ? nginx.results : [];
  const statusClass = nginx.ok ? 'ok' : 'fail';
  const statusText = nginx.ok ? '业务域名配置完成' : '业务域名配置未完成';
  target.innerHTML = `
    <div class="doctor-item ${statusClass}">
      <div class="doctor-icon">${nginx.ok ? '✓' : '×'}</div>
      <div>
        <strong>${escapeHtml(statusText)}</strong>
        <span>${escapeHtml(nginx.url || nginx.domain || '请检查下方结果')}</span>
      </div>
    </div>
    ${results.map(item => `
      <div class="doctor-item ${item.ok ? 'ok' : 'fail'}">
        <div class="doctor-icon">${item.ok ? '✓' : '×'}</div>
        <div>
          <strong>${escapeHtml(item.step || '-')}</strong>
          <span>${escapeHtml(item.detail || item.output || '')}</span>
        </div>
      </div>
    `).join('')}
    ${nginx.conf ? `
      <div class="doctor-item info">
        <div class="doctor-icon">i</div>
        <div><strong>Nginx 配置文件</strong><span>${escapeHtml(nginx.conf)}</span></div>
      </div>
    ` : ''}
  `;
}

async function bootstrapCurrentProject() {
  const project = collectProjectForm();
  if (!$('projectForm').reportValidity()) return;
  if (!$('projectConfigConfirmed').checked || projectPreviewSignature !== JSON.stringify(project)) {
    setProjectFormError('请先预览部署文件，并勾选确认。配置变更后需要重新预览。');
    return;
  }
  setProjectFormError('');
  const key = normalizedProjectKeyFromForm(project);
  if (!key) return;
  const confirmed = await showConfirmDialog({
    title: '自动初始化服务器',
    message: `将保存配置、拉取代码，并补齐缺少的 deploy.sh${['python', 'go', 'java'].includes(project.template) ? ' 和 systemd 服务文件' : ''}。已有文件会保留；初始化不会启动业务。确认继续？`,
    confirmText: '开始初始化',
    danger: false,
  });
  if (!confirmed) return;
  const button = $('bootstrapProjectBtn');
  const oldText = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = '初始化中';
  }
  $('projectDoctorResult').textContent = '正在初始化服务器目录、仓库、部署脚本和运行服务...';
  try {
    const payload = {
      original_key: $('projectOriginalKey').value || editingProjectKey,
      project,
      options: { write_service: ['python', 'go', 'java'].includes(project.template) },
    };
    lastConfig = await postJsonBody('projects-config/bootstrap', payload);
    lastProjects = lastConfig.projects || [];
    const saved = projectByKey(key) || lastProjects.find(item => item.name === project.name) || lastProjects[0];
    if (saved) {
      selectedProjectKey = saved.key;
      openProjectModal(saved);
    }
    renderBootstrapResult(lastConfig.bootstrap || {});
    await refresh();
  } catch (err) {
    $('projectDoctorResult').innerHTML = `
      <div class="doctor-item fail">
        <div class="doctor-icon">×</div>
        <div><strong>初始化失败</strong><span>${escapeHtml(err.message)}</span></div>
      </div>
    `;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText || '自动初始化';
    }
  }
}

async function configureProjectNginx() {
  const project = collectProjectForm();
  if (!project.app_domain) {
    setProjectFormError('请先填写业务域名，例如 api.example.com。');
    return;
  }
  if (!await ensureNginxConfigured()) return;
  const confirmed = await showConfirmDialog({
    title: '配置业务域名',
    message: `将使用已选择的 Nginx 实例和后端地址，为 ${project.app_domain} 配置反向代理。业务端口为 ${project.service_port || 8000}${project.app_https ? '；仅本机模式可通过 Certbot 自动申请证书' : ''}。确认继续？`,
    confirmText: '配置域名',
    danger: false,
  });
  if (!confirmed) return;
  const button = $('configureNginxBtn');
  const oldText = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = '配置中';
  }
  $('projectDoctorResult').textContent = '正在写入 Nginx 配置、测试并重载服务...';
  try {
    const payload = {
      original_key: $('projectOriginalKey').value || editingProjectKey,
      project,
      options: { issue_https: Boolean(project.app_https) },
    };
    lastConfig = await postJsonBody('projects-config/nginx', payload);
    lastProjects = lastConfig.projects || [];
    const saved = projectByKey(normalizedProjectKeyFromForm(project));
    if (saved) {
      selectedProjectKey = saved.key;
      openProjectModal(saved);
    }
    renderNginxResult(lastConfig.nginx || {});
    await refresh();
  } catch (err) {
    $('projectDoctorResult').innerHTML = `
      <div class="doctor-item fail">
        <div class="doctor-icon">×</div>
        <div><strong>业务域名配置失败</strong><span>${escapeHtml(err.message)}</span></div>
      </div>
    `;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText || '配置业务域名';
    }
  }
}

async function doctorCurrentProject() {
  const key = $('projectOriginalKey').value || editingProjectKey || normalizedProjectKeyFromForm(collectProjectForm());
  if (!key) return;
  const button = $('doctorProjectBtn');
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = '检查中';
  $('projectDoctorResult').textContent = '正在检查服务器目录、脚本和运行环境...';
  try {
    const data = await fetchJson(`projects-config/doctor?project=${encodeURIComponent(key)}`);
    renderDoctorResult(data);
  } catch (err) {
    $('projectDoctorResult').innerHTML = `
      <div class="doctor-item fail">
        <div class="doctor-icon">×</div>
        <div><strong>检查失败</strong><span>${escapeHtml(err.message)}</span></div>
      </div>
    `;
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

function openProjectModal(project = null) {
  projectGuidanceVersion += 1;
  invalidateProjectPreview();
  $('projectDetectionResult').replaceChildren();
  $('projectDetectionResult').hidden = true;
  $('projectInspectStatus').textContent = '';
  $('inspectProjectBtn').disabled = false;
  $('previewProjectBtn').disabled = false;
  $('projectForm').querySelectorAll('[data-auto-value]').forEach(input => delete input.dataset.autoValue);
  editingProjectKey = project ? project.key : '';
  setProjectFormError('');
  $('projectModalTitle').textContent = project ? '编辑仓库' : '添加仓库';
  $('projectModalHint').textContent = project
    ? '配置保存后立即生效。已有部署脚本和服务文件会保留。'
    : '仓库凭据使用服务器上的 Git 配置。业务域名和 HTTPS 可稍后配置。';
  $('projectOriginalKey').value = project ? project.key : '';
  $('projectKeyInput').value = project ? project.key : '';
  $('projectNameInput').value = project ? project.name : '';
  $('projectTemplateInput').value = project ? (project.template || 'custom') : 'docker';
  $('projectRepoInput').value = project ? project.repo : '';
  $('projectBranchInput').value = project ? project.branch : 'main';
  $('projectTimeoutInput').value = project ? project.timeout_seconds : 900;
  $('projectWorkdirInput').value = project ? project.workdir : '';
  $('projectScriptInput').value = project ? project.script : '';
  $('projectServiceNameInput').value = project ? (project.service_name || project.key || '') : '';
  $('projectServicePortInput').value = project ? (project.service_port || 8000) : '';
  $('projectStartCommandInput').value = project ? (project.start_command || '') : '';
  $('projectAppDomainInput').value = project ? (project.app_domain || '') : '';
  $('projectRollbackInput').value = project ? project.rollback_script : '';
  $('projectHealthInput').value = project ? project.health_url : '';
  $('projectLogInput').value = project ? project.deploy_log_file : '';
  $('projectSecretInput').value = project ? project.webhook_secret : '';
  $('projectEnabledInput').checked = project ? Boolean(project.enabled) : false;
  $('projectManualInput').checked = project ? Boolean(project.manual_deploy_enabled) : true;
  $('projectAppHttpsInput').checked = project ? Boolean(project.app_https) : false;
  $('deleteProjectBtn').hidden = !project;
  $('resetSecretBtn').hidden = !project;
  $('doctorProjectBtn').disabled = !project;
  $('projectDoctorResult').innerHTML = project ? '点击检查项目，查看服务器接入状态。' : '保存项目后可以检查。';
  applyTemplateDefaults(!project);
  updateWebhookPreview();
  $('projectModal').hidden = false;
  $('projectKeyInput').focus();
}

function closeProjectModal() {
  projectGuidanceVersion += 1;
  $('projectModal').hidden = true;
  editingProjectKey = '';
}

function updateWebhookPreview() {
  const project = collectProjectForm();
  const key = normalizedProjectKeyFromForm(project);
  $('projectWebhookUrl').textContent = projectWebhookUrl(key || editingProjectKey || '');
  $('projectWebhookToken').textContent = $('projectSecretInput').value || '保存后生成';
  $('projectSetupCommands').textContent = generateSetupCommands(project);
  $('projectWebhookGuide').textContent = generateWebhookGuide(project);
}

function collectProjectForm() {
  return {
    key: $('projectKeyInput').value,
    name: $('projectNameInput').value,
    template: $('projectTemplateInput').value,
    repo: $('projectRepoInput').value,
    branch: $('projectBranchInput').value,
    timeout_seconds: Number($('projectTimeoutInput').value || 900),
    workdir: $('projectWorkdirInput').value,
    script: $('projectScriptInput').value,
    service_name: $('projectServiceNameInput').value,
    service_port: Number($('projectServicePortInput').value || 8000),
    start_command: $('projectStartCommandInput').value,
    app_domain: $('projectAppDomainInput').value,
    app_https: $('projectAppHttpsInput').checked,
    rollback_script: $('projectRollbackInput').value,
    health_url: $('projectHealthInput').value,
    deploy_log_file: $('projectLogInput').value,
    webhook_secret: $('projectSecretInput').value,
    enabled: $('projectEnabledInput').checked,
    manual_deploy_enabled: $('projectManualInput').checked,
  };
}

async function refreshProjectConfig() {
  try {
    lastConfig = await fetchJson('projects-config');
    lastProjects = lastConfig.projects || [];
    renderNotificationConfig(lastConfig.notifications || {});
    return lastConfig;
  } catch (err) {
    lastConfig = null;
    return null;
  }
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
    lastConfig = await postJsonBody('projects-config/notifications', { notifications: collectNotificationConfig() });
    lastProjects = lastConfig.projects || [];
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

async function initProjectConfig() {
  try {
    lastConfig = await postJson('projects-config/init');
    lastProjects = lastConfig.projects || [];
    await refresh();
    $('logs').textContent = `项目配置已初始化: ${lastConfig.config_file}`;
  } catch (err) {
    $('logs').textContent = `初始化项目配置失败: ${err.message}`;
  }
}

async function saveProjectForm(event) {
  event.preventDefault();
  setProjectFormError('');
  try {
    const payload = {
      original_key: $('projectOriginalKey').value || editingProjectKey,
      project: collectProjectForm(),
    };
    lastConfig = await postJsonBody('projects-config/save', payload);
    lastProjects = lastConfig.projects || [];
    const requestedKey = String(payload.project.key || payload.project.name || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
    const savedKey = (lastProjects.find(project => project.key === requestedKey) || lastProjects.find(project => project.name === payload.project.name) || {}).key || payload.original_key || '';
    selectedProjectKey = savedKey;
    const savedProject = projectByKey(savedKey);
    if (savedProject) openProjectModal(savedProject);
    setProjectFormError('保存成功。下方指令和 WebHook 配置已更新，可直接复制。');
    await refresh();
  } catch (err) {
    setProjectFormError(`保存失败: ${err.message}`);
  }
}

async function deleteCurrentProject() {
  const key = $('projectOriginalKey').value || editingProjectKey;
  if (!key) return;
  const confirmed = await showConfirmDialog({
    title: '删除项目',
    message: `确认删除项目 ${key}？运行中的项目不能删除。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    lastConfig = await postJson(`projects-config/delete?project=${encodeURIComponent(key)}`);
    lastProjects = lastConfig.projects || [];
    selectedProjectKey = '';
    closeProjectModal();
    await refresh();
  } catch (err) {
    setProjectFormError(`删除失败: ${err.message}`);
  }
}

async function resetCurrentSecret() {
  const key = $('projectOriginalKey').value || editingProjectKey;
  if (!key) return;
  const confirmed = await showConfirmDialog({
    title: '重置 WebHook Token',
    message: `确认重置 ${key} 的 WebHook Token？旧 Token 会立即失效。`,
    confirmText: '重置',
  });
  if (!confirmed) return;
  try {
    lastConfig = await postJson(`projects-config/secret?project=${encodeURIComponent(key)}`);
    lastProjects = lastConfig.projects || [];
    const project = projectByKey(key);
    if (project) openProjectModal(project);
    await refresh();
  } catch (err) {
    setProjectFormError(`重置失败: ${err.message}`);
  }
}

function updateLiveIndicator(agent, state, active) {
  const el = $('liveIndicator');
  if (!el) return;
  const failed = active && active.status === 'failed';
  el.className = `live-indicator ${failed ? 'live-error' : state.running ? 'live-busy' : 'live-ok'}`;
  el.lastChild.textContent = failed ? '最近失败' : state.running ? '部署中' : 'Agent 在线';
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
  const abnormalEvents = lastEvents.filter(isAbnormalEvent);
  const visibleEventsAll = abnormalEvents.filter(event => !dismissedNoticeKeys.has(noticeSignature('event', event)));
  const dismissedCount = abnormalEvents.length - visibleEventsAll.length;
  if (hint) {
    hint.textContent = visibleEventsAll.length
      ? `最近 ${visibleEventsAll.length} 条异常事件${dismissedCount ? ` · 已清除 ${dismissedCount} 条` : ''}`
      : dismissedCount
        ? `已清除 ${dismissedCount} 条当前异常，新异常会继续显示`
        : '部署、WebHook、容器和资源异常汇总';
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

function renderDeployLock(lock = {}) {
  const target = $('deployLockStatus');
  if (!target) return;
  const locked = Boolean(lock.locked);
  const canForce = Boolean(lock.can_force_unlock);
  const activePid = lock.active_pid ? `PID ${lock.active_pid}` : '无活跃进程';
  target.innerHTML = `
    <div class="lock-main">
      <span class="badge ${locked ? 'running' : 'success'}">${locked ? '部署锁定' : '未锁定'}</span>
      <span>${escapeHtml(lock.phase_label || activePid)}</span>
      ${lock.duration_seconds != null ? `<span>${escapeHtml(lock.duration_seconds)}s</span>` : ''}
    </div>
    ${canForce ? '<button id="forceUnlockBtn" class="danger-button compact-action" type="button">强制解锁</button>' : ''}
    ${lock.active_process ? `<code>部署进程仍在运行：${escapeHtml(activePid)}</code>` : ''}
  `;
  $('forceUnlockBtn')?.addEventListener('click', forceUnlockDeploy);
}

function renderPhaseDurations(item = {}) {
  const phases = Array.isArray(item.phase_durations) ? item.phase_durations : [];
  if (!phases.length) return '<div class="phase-duration-empty">暂无阶段耗时</div>';
  return `<div class="phase-duration-list">${phases.map(phase => `
    <div class="phase-duration-item">
      <span>${escapeHtml(phase.label || phase.phase || '-')}</span>
      <strong>${escapeHtml(phase.duration_seconds ?? '-')}s</strong>
    </div>
  `).join('')}</div>`;
}

function renderChangedFiles(item = {}) {
  const files = Array.isArray(item.changed_files) ? item.changed_files : [];
  const count = Number(item.changed_file_count || files.length || 0);
  if (!count) return '<div class="changed-file-empty">暂无变更文件摘要</div>';
  return `
    <div class="changed-file-summary">${count} 个文件变更${files.length < count ? `，显示前 ${files.length} 个` : ''}</div>
    <div class="changed-file-list">${files.slice(0, 16).map(file => `<code>${escapeHtml(file)}</code>`).join('')}</div>
  `;
}

function renderDeployDetailInsights(item = {}) {
  return `
    ${item.status === 'failed' ? renderFailureAdvice(item.diagnosis || [{title: '该历史记录暂无诊断', advice: '请打开部署日志查看具体错误。'}]) : ''}
    <div class="deploy-detail-insights">
      <section class="deploy-insight-section">
        <div class="deploy-insight-title">阶段耗时</div>
        ${renderPhaseDurations(item)}
      </section>
      <section class="deploy-insight-section">
        <div class="deploy-insight-title">变更文件</div>
        ${renderChangedFiles(item)}
      </section>
    </div>
  `;
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

async function runContainerAction(container, action) {
  const labels = {
    restart: '重启',
    stop: '停止',
    start: '启动',
    pause: '暂停',
    unpause: '恢复',
  };
  const label = labels[action] || action;
  const confirmed = await showConfirmDialog({
    title: `${label}容器`,
    message: `确认${label}容器 ${container}？`,
    confirmText: label,
    danger: action !== 'start' && action !== 'unpause',
  });
  if (!confirmed) return;
  try {
    await postJsonBody('docker/action', { container, action });
    $('logs').textContent = `容器 ${container} 已${label}`;
    await refresh();
  } catch (err) {
    $('logs').textContent = `容器操作失败: ${err.message}`;
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

function renderStatus(data) {
  const state = data.state || {};
  const agent = data.agent || {};
  const git = data.git || {};
  const projects = data.projects || [];
  renderSystemStatus(data.system || {});
  renderAlerts(data.alerts || []);
  renderEvents(data.events || []);
  renderDeployLock(data.lock || {});
  updateProjectSelect(projects);
  renderProjectOverview(projects, agent, data.lock || {}, state);
  const current = state.current_deploy;
  const last = state.last_deploy;
  const active = current || last || {};
  const activeProject = selectedProject(projects);
  updateProjectActionHint(activeProject, projects);
  const displayGit = activeProject || git;
  const editProjectBtn = $('editProjectBtn');
  if (editProjectBtn) {
    editProjectBtn.disabled = !activeProject && projects.length !== 1;
    editProjectBtn.title = editProjectBtn.disabled ? '请先选择一个具体项目' : '';
  }
  const initProjectsBtn = $('initProjectsBtn');
  if (initProjectsBtn) {
    initProjectsBtn.hidden = Boolean(agent.projects_config_exists);
  }
  const redeployBtn = $('redeployBtn');
  if (redeployBtn) {
    redeployBtn.disabled = !activeProject || !activeProject.enabled || !activeProject.manual_deploy_enabled;
    redeployBtn.title = !activeProject
      ? '请选择一个具体项目'
      : !activeProject.enabled
        ? '当前项目已禁用'
        : !activeProject.manual_deploy_enabled
        ? '当前项目不允许手动部署'
        : '';
  }
  const rollbackBtn = $('rollbackBtn');
  if (rollbackBtn) {
    rollbackBtn.disabled = !activeProject || !activeProject.enabled || !activeProject.manual_deploy_enabled || !activeProject.rollback_available;
    rollbackBtn.title = activeProject && !activeProject.enabled
      ? '当前项目已禁用'
      : activeProject && !activeProject.manual_deploy_enabled
        ? '当前项目不允许手动操作'
        : activeProject && !activeProject.rollback_available
      ? '当前项目暂无可回滚的历史版本'
      : '';
  }
  const cancelDeployBtn = $('cancelDeployBtn');
  if (cancelDeployBtn) {
    const projectCanCancel = Boolean(activeProject && (activeProject.running || Number(activeProject.queue_size || 0) > 0));
    const allCanCancel = Boolean(!selectedProjectKey && (state.running || Number(state.queue_size || 0) > 0));
    const canCancel = projectCanCancel || allCanCancel;
    cancelDeployBtn.disabled = !canCancel;
    cancelDeployBtn.textContent = !selectedProjectKey ? '取消全部' : '取消';
    cancelDeployBtn.title = canCancel
      ? (!selectedProjectKey ? '取消所有项目当前运行或排队的部署任务' : '取消当前项目运行或排队的部署任务')
      : '当前没有可取消的部署任务';
  }

  updateLiveIndicator(agent, state, active);
  const agentOk = agent.status === 'ok';
  $('agentStatus').className = `badge ${agentOk ? 'success' : 'failed'}`;
  $('agentStatus').textContent = agentOk ? '在线' : '异常';
  const deployBadgeStatus = state.running ? 'running' : (active.status || 'neutral');
  $('deployStatus').className = `badge ${deployBadgeStatus}`;
  $('deployStatus').textContent = state.running ? '部署中' : statusLabel(active.status);
  $('deploySub').innerHTML = deploySubtitle('目标', active);
  const queueSize = Number(state.queue_size ?? 0);
  $('queueSize').className = `badge ${queueSize > 0 ? 'running' : 'success'}`;
  $('queueSize').textContent = `${queueSize} 个任务`;
  $('headSha').textContent = shortSha(displayGit.head);
  $('branchName').className = 'badge neutral';
  $('branchName').textContent = displayGit.git_branch || displayGit.branch || '-';
  $('agentHost').textContent = agent.host ? `${agent.host}:${agent.port}` : '-';
  $('targetBranchMini').textContent = activeProject ? activeProject.branch : (agent.branch || '-');
  $('targetBranch').textContent = activeProject ? activeProject.branch : (agent.branch || '-');
  $('projectDir').textContent = activeProject ? activeProject.project_dir : (agent.project_dir || '-');
  $('deployScript').textContent = activeProject ? activeProject.deploy_script : (agent.deploy_script || '-');
  $('lastWebhook').textContent = fmtTime(state.last_webhook_at);
  $('updatedAt').textContent = `刷新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;

  const lastBadge = $('lastBadge');
  lastBadge.className = `badge ${active.status || 'neutral'}`;
  lastBadge.textContent = statusLabel(active.status);

  const history = [];
  if (current) history.push(current);
  history.push(...(state.history || []));
  const scopedHistory = filterBySelectedProject(history);
  const seen = new Set();
  const uniqueHistory = scopedHistory.filter(item => {
    const key = `${item.started_at}-${item.after}-${item.status}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const scopedActive = uniqueHistory[0] || active || {};
  const scopedDeployBadgeStatus = scopedActive.status || 'neutral';
  $('deployStatus').className = `badge ${scopedDeployBadgeStatus}`;
  $('deployStatus').textContent = statusLabel(scopedActive.status);
  $('deploySub').innerHTML = deploySubtitle(selectedProjectLabel(projects), scopedActive);
  lastBadge.className = `badge ${scopedActive.status || 'neutral'}`;
  lastBadge.textContent = statusLabel(scopedActive.status);
  const refreshSeconds = state.running ? 5 : 30;
  const visibleItems = uniqueHistory.slice(0, 60);
  const closedDetailKeys = collectClosedDeployDetails();
  renderMiniHistory('agentHistory', statusSeries(agentOk ? 'success' : 'failed', uniqueHistory.length ? Math.min(60, uniqueHistory.length) : 1));
  renderMiniHistory('deployHistory', uniqueHistory);
  renderMiniHistory('queueHistory', queueSize > 0 ? statusSeries('running', Math.min(60, queueSize)) : statusSeries('success', 1));
  renderMiniHistory('gitHistory', displayGit.head ? statusSeries('success', uniqueHistory.length ? Math.min(60, uniqueHistory.length) : 1) : statusSeries('failed', 1));
  const stats = $('deployStats');
  stats.classList.remove('skeleton-block');
  if (!visibleItems.length) {
    stats.innerHTML = `
      <article class="deploy-stat-card">
        <div class="stat-main">
          <div class="stat-title">暂无部署记录</div>
          <div class="stat-identity">
            <div class="stat-icon">CI</div>
            <div class="stat-meta">
              <span class="stat-pill">Git</span>
              <span class="badge neutral">等待中</span>
            </div>
          </div>
          <div class="stat-metrics">
            <div class="metric-box">
              <div class="metric-label">Deploy Time</div>
              <div class="metric-value">-</div>
            </div>
            <div class="metric-box">
              <div class="metric-label">Exit Code</div>
              <div class="metric-value">-</div>
            </div>
          </div>
          <div class="availability-box">
            <div class="availability-inner">
              <div>
                <div class="availability-label">Availability</div>
                <div class="availability-sub">等待第一次部署</div>
              </div>
              <div class="availability-score badge neutral">-</div>
            </div>
          </div>
        </div>
        <div class="stat-history">
          <div class="history-head">
            <span>History (60pts)</span>
            <span class="history-next">Next update in ${refreshSeconds}s</span>
          </div>
          <div class="history-bars">
            <div class="history-track">${renderHistoryBars([])}</div>
          </div>
          <div class="history-axis">
            <span>Oldest</span>
            <span>Now</span>
          </div>
        </div>
      </article>
    `;
  } else {
    stats.innerHTML = visibleItems
      .map((item, index) => renderDeployCard(item, index, uniqueHistory, refreshSeconds, closedDetailKeys))
      .join('');
  }
}

let currentLogKind = 'deploy';
let currentLogLineLimit = 200;
let lastLogsPayload = { deploy_log: [], agent_log: [] };

function isStatusPollLine(line) {
  return /"GET \/((deploy\/)?(status|logs))(\?| )/.test(line);
}

function logLineClass(line) {
  const text = String(line).toLowerCase();
  if (/(error|failed|failure|fatal|exception|traceback|health failed|curl: \(22\)| 502| 503|exit code: 1|code=1)/.test(text)) {
    return 'log-error';
  }
  if (/(warn|warning|retrying|timeout|timed out|attempt=|ignored)/.test(text)) {
    return 'log-warn';
  }
  if (/(health ok|healthy|deploy finished code=0|success)/.test(text)) {
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

function updateLogTabs() {
  $('deployLogBtn').classList.toggle('active', currentLogKind === 'deploy');
  $('agentLogBtn').classList.toggle('active', currentLogKind === 'agent');
  $('logHint').textContent = currentLogKind === 'deploy'
    ? '部署脚本原始输出'
    : 'Agent 原始输出，已隐藏状态轮询';
}

function renderLogs(data = lastLogsPayload) {
  lastLogsPayload = data || lastLogsPayload;
  updateLogTabs();
  const projectLog = selectedProjectKey
    ? ((lastLogsPayload.project_logs || {})[selectedProjectKey] || [])
    : (lastLogsPayload.deploy_log || []);
  const rawLines = currentLogKind === 'agent'
    ? (lastLogsPayload.agent_log || [])
    : projectLog;
  const lines = rawLines.filter(line => !isStatusPollLine(String(line)));
  $('logCount').textContent = `${lines.length} 行`;
  $('logs').innerHTML = lines.length
    ? renderLogLines(lines)
    : escapeHtml(currentLogKind === 'deploy' ? '暂无部署脚本日志' : '暂无 Agent 日志');
}

function logsUrl(path = 'logs', extra = {}) {
  const params = new URLSearchParams();
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  });
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

async function refreshLogsOnly() {
  try {
    const logs = await fetchJson(logsUrl('logs', { lines: currentLogLineLimit }));
    renderLogs(logs);
  } catch (err) {
    $('logs').textContent = `日志加载失败: ${err.message}`;
  }
}

function downloadCurrentLog() {
  if ($('logLineSelect')?.value === 'custom') {
    currentLogLineLimit = selectedLineCount('logLineSelect', 'logLineCustomInput', currentLogLineLimit);
  }
  const lines = selectedDownloadLines('logDownloadSelect', 'logDownloadCustomInput', currentLogLineLimit);
  const params = { kind: currentLogKind, lines };
  if (currentLogKind === 'deploy' && selectedProjectKey) {
    params.project = selectedProjectKey;
  }
  window.location.href = logsUrl('logs/download', params);
}

async function refresh() {
  const requestId = ++systemStatusRequestId;
  try {
    const [status, logs, config] = await Promise.all([
      fetchJson('status'),
      fetchJson(logsUrl('logs', { lines: currentLogLineLimit })),
      fetchJson('projects-config'),
    ]);
    csrfToken = status.csrf_token || csrfToken;
    lastConfig = config;
    lastProjects = config.projects || [];
    renderNotificationConfig(config.notifications || {});
    if (requestId === systemStatusRequestId) renderStatus(status);
    renderLogs(logs);
    scheduleRefresh(status.state && status.state.running ? 3000 : 30000);
  } catch (err) {
    $('logs').textContent = `加载失败: ${err.message}`;
    scheduleRefresh(30000);
  }
}

let refreshTimer = null;
function clearRefresh() {
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh(delay) {
  clearRefresh();
  if (activeView !== 'deploy') return;
  refreshTimer = window.setTimeout(refresh, delay);
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
    const [config, status] = await Promise.all([fetchJson('projects-config'), fetchJson('status')]);
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
    renderSystemStatus(status.system || {});
    renderAlerts(status.alerts || []);
    renderEvents(status.events || []);
    renderDeployLock(status.lock || {});
    $('updatedAt').textContent = `服务器刷新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  } catch (err) {
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

async function ensurePreflightBeforeDeploy() {
  try {
    const preflight = await fetchPreflightData();
    if (preflight.level === 'critical') {
      const target = $('preflightResult');
      if (target) target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return false;
    }
  } catch (err) {
    const target = $('preflightResult');
    if (target) {
      target.innerHTML = `<div class="preflight-item failed"><strong>体检失败</strong><span>${escapeHtml(err.message)}</span></div>`;
      target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    return false;
  }
  return true;
}

async function manualRedeploy() {
  const projectParam = selectedProjectKey ? `?project=${encodeURIComponent(selectedProjectKey)}` : '';
  if (!(await ensurePreflightBeforeDeploy())) return;
  const confirmed = await showConfirmDialog({
    title: '重新部署',
    message: `确认重新部署 ${selectedProjectKey || '默认项目'}？`,
    confirmText: '重新部署',
  });
  if (!confirmed) return;
  const button = $('redeployBtn');
  button.disabled = true;
  button.textContent = '已加入队列';
  try {
    await postJson(`redeploy${projectParam}`);
    await refresh();
  } catch (err) {
    $('logs').textContent = `重新部署失败: ${err.message}`;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = '重新部署';
    }, 1200);
  }
}

async function manualRollback() {
  const projectParam = selectedProjectKey ? `?project=${encodeURIComponent(selectedProjectKey)}` : '';
  if (!(await ensurePreflightBeforeDeploy())) return;
  const confirmed = await showConfirmDialog({
    title: '回滚部署',
    message: `确认回滚 ${selectedProjectKey || '默认项目'}？`,
    confirmText: '回滚',
  });
  if (!confirmed) return;
  const button = $('rollbackBtn');
  button.disabled = true;
  button.textContent = '已加入队列';
  try {
    await postJson(`rollback${projectParam}`);
    await refresh();
  } catch (err) {
    $('logs').textContent = `回滚失败: ${err.message}`;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = '回滚';
    }, 1200);
  }
}

async function cancelDeploy() {
  const projectParam = selectedProjectKey ? `?project=${encodeURIComponent(selectedProjectKey)}` : '?all=1';
  const targetText = selectedProjectKey || '全部项目';
  const confirmed = await showConfirmDialog({
    title: '取消部署',
    message: `确认取消 ${targetText} 的部署任务？正在运行的脚本会收到终止信号。`,
    confirmText: '取消部署',
  });
  if (!confirmed) return;
  const button = $('cancelDeployBtn');
  button.disabled = true;
  button.textContent = '取消中';
  try {
    const result = await postJson(`cancel${projectParam}`);
    $('logs').textContent = `取消请求已提交：运行中 ${result.running_canceled ? '已终止' : '无'}，队列取消 ${result.queued_canceled || 0} 个`;
    await refresh();
  } catch (err) {
    $('logs').textContent = `取消部署失败: ${err.message}`;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = !selectedProjectKey ? '取消全部' : '取消';
    }, 1200);
  }
}

$('refreshBtn').addEventListener('click', () => {
  if (activeView === 'server') refreshServerStatus();
  else if (activeView === 'events') refreshEventsStatus();
  else if (activeView === 'notify') refreshNotificationConfig();
  else if (activeView === 'certificates') refreshCertificates();
  else refresh();
});
$('deployViewTab').addEventListener('click', () => setView('deploy'));
$('serverViewTab').addEventListener('click', () => setView('server'));
$('eventsViewTab')?.addEventListener('click', () => setView('events'));
$('notifyViewTab')?.addEventListener('click', () => setView('notify'));
$('certificatesViewTab')?.addEventListener('click', () => setView('certificates'));
$('clearAlertsBtn')?.addEventListener('click', toggleCurrentNoticeDismissal);
$('deployStats')?.addEventListener('click', (event) => {
  const bar = event.target.closest('.history-bar[data-detail-key]');
  if (!bar) return;
  focusDeployRecord(bar.dataset.detailKey);
});
document.querySelectorAll('[data-server-refresh]').forEach(button => {
  button.addEventListener('click', () => setServerRefreshInterval(button.dataset.serverRefresh));
});
document.querySelectorAll('[data-trend-range]').forEach(button => {
  button.addEventListener('click', () => {
    systemTrendRange = button.dataset.trendRange || '24h';
    document.querySelectorAll('[data-trend-range]').forEach(item => item.classList.toggle('active', item === button));
    if (activeView === 'server') refreshServerStatus();
    else refresh();
  });
});
$('redeployBtn').addEventListener('click', manualRedeploy);
$('rollbackBtn').addEventListener('click', manualRollback);
$('cancelDeployBtn').addEventListener('click', cancelDeploy);
$('preflightBtn')?.addEventListener('click', runPreflight);
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
$('initProjectsBtn').addEventListener('click', initProjectConfig);
$('addProjectBtn').addEventListener('click', () => openProjectModal(null));
$('editProjectBtn').addEventListener('click', async () => {
  if (!lastConfig) await refreshProjectConfig();
  const key = selectedProjectKey || (lastProjects.length === 1 ? lastProjects[0].key : '');
  const project = projectByKey(key);
  if (project) openProjectModal(project);
});
$('projectForm').addEventListener('submit', saveProjectForm);
$('closeProjectModalBtn').addEventListener('click', closeProjectModal);
$('cancelProjectBtn').addEventListener('click', closeProjectModal);
$('closeContainerLogsBtn').addEventListener('click', closeContainerLogsModal);
$('deleteProjectBtn').addEventListener('click', deleteCurrentProject);
$('resetSecretBtn').addEventListener('click', resetCurrentSecret);
$('bootstrapProjectBtn')?.addEventListener('click', bootstrapCurrentProject);
$('inspectProjectBtn')?.addEventListener('click', inspectCurrentProject);
$('previewProjectBtn')?.addEventListener('click', previewCurrentProject);
$('projectForm').addEventListener('input', event => {
  if (event.target.id !== 'projectConfigConfirmed') invalidateProjectPreview();
});
$('configureNginxBtn')?.addEventListener('click', configureProjectNginx);
$('doctorProjectBtn').addEventListener('click', doctorCurrentProject);
$('copyWebhookBtn').addEventListener('click', () => copyFromElement('projectWebhookUrl', 'copyWebhookBtn'));
$('copyWebhookTokenBtn').addEventListener('click', () => copyFromElement('projectWebhookToken', 'copyWebhookTokenBtn'));
[
  'projectKeyInput',
  'projectNameInput',
  'projectRepoInput',
  'projectBranchInput',
  'projectTimeoutInput',
  'projectWorkdirInput',
  'projectScriptInput',
  'projectServiceNameInput',
  'projectServicePortInput',
  'projectStartCommandInput',
  'projectAppDomainInput',
  'projectRollbackInput',
  'projectHealthInput',
  'projectLogInput',
  'projectSecretInput',
].forEach(id => {
  const input = $(id);
  if (input) input.addEventListener('input', updateWebhookPreview);
});
$('projectTemplateInput').addEventListener('change', () => {
  applyTemplateDefaults(false);
  invalidateProjectPreview();
  updateWebhookPreview();
});
['projectKeyInput', 'projectNameInput'].forEach(id => {
  const input = $(id);
  if (input) input.addEventListener('change', () => {
    applyTemplateDefaults(false);
    updateWebhookPreview();
  });
});
$('projectEnabledInput').addEventListener('change', updateWebhookPreview);
$('projectManualInput').addEventListener('change', updateWebhookPreview);
$('projectAppHttpsInput').addEventListener('change', updateWebhookPreview);
$('copyProjectCommandsBtn').addEventListener('click', () => copyFromElement('projectSetupCommands', 'copyProjectCommandsBtn'));
$('copyWebhookGuideBtn').addEventListener('click', () => copyFromElement('projectWebhookGuide', 'copyWebhookGuideBtn'));
$('themeToggleBtn')?.addEventListener('click', toggleTheme);
$('projectModal').addEventListener('click', (event) => {
  if (event.target === $('projectModal')) closeProjectModal();
});
$('containerLogsModal').addEventListener('click', (event) => {
  if (event.target === $('containerLogsModal')) closeContainerLogsModal();
});
$('confirmModal').addEventListener('click', (event) => {
  if (event.target === $('confirmModal')) closeConfirmDialog(false);
});
$('confirmCancelBtn').addEventListener('click', () => closeConfirmDialog(false));
$('confirmOkBtn').addEventListener('click', () => closeConfirmDialog(true));
$('containerList').addEventListener('click', handleContainerAction);
$('projectSelectButton').addEventListener('click', (event) => {
  event.stopPropagation();
  toggleProjectMenu();
});
$('projectSelectMenu').addEventListener('click', (event) => {
  event.stopPropagation();
});
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
  closeProjectMenu();
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
    closeProjectMenu();
    closeLogSelectMenus();
    closeProjectModal();
    closeContainerLogsModal();
  }
});
$('deployLogBtn').addEventListener('click', () => {
  currentLogKind = 'deploy';
  renderLogs();
});
$('agentLogBtn').addEventListener('click', () => {
  currentLogKind = 'agent';
  renderLogs();
});
$('logLineSelect').addEventListener('change', () => {
  syncCustomLineInput('logLineSelect', 'logLineCustomInput', currentLogLineLimit);
  currentLogLineLimit = selectedLineCount('logLineSelect', 'logLineCustomInput', currentLogLineLimit);
  refreshLogsOnly();
});
$('logLineCustomInput').addEventListener('change', () => {
  currentLogLineLimit = selectedLineCount('logLineSelect', 'logLineCustomInput', currentLogLineLimit);
  refreshLogsOnly();
});
$('logDownloadSelect').addEventListener('change', () => {
  syncCustomLineInput('logDownloadSelect', 'logDownloadCustomInput', currentLogLineLimit);
});
$('downloadLogBtn').addEventListener('click', downloadCurrentLog);
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
['logLineSelect', 'logDownloadSelect', 'containerLogLineSelect', 'containerLogDownloadSelect', 'containerLogFilterSelect', 'containerLogContextSelect'].forEach(enhanceLogSelect);
syncCustomLineInput('containerLogLineSelect', 'containerLogLineCustomInput', currentContainerLogLineLimit);
syncCustomLineInput('containerLogDownloadSelect', 'containerLogDownloadCustomInput', currentContainerLogLineLimit);
syncCustomLineInput('logLineSelect', 'logLineCustomInput', currentLogLineLimit);
syncCustomLineInput('logDownloadSelect', 'logDownloadCustomInput', currentLogLineLimit);
syncContainerLogFilterControls();
refresh();
