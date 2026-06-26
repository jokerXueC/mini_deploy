const $ = (id) => document.getElementById(id);
let selectedProjectKey = '';
let lastProjects = [];
let lastConfig = null;
let editingProjectKey = '';
let activeView = 'deploy';
let csrfToken = '';
let serverRefreshSeconds = 30;
let serverRefreshTimer = null;
let currentContainerLogName = '';
let currentContainerLogLineLimit = 200;

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

function renderProjectOverview(projects = [], agent = {}) {
  const count = projects.length || Number(agent.project_count || 0);
  const multi = count > 1;
  const modeBadge = $('projectModeBadge');
  const modeHint = $('projectModeHint');
  const list = $('projectOverview');
  if (modeBadge) {
    modeBadge.className = `badge ${multi ? 'success' : 'neutral'}`;
    modeBadge.textContent = multi ? `多项目模式 · ${count} 个` : '单项目模式';
  }
  if (modeHint) {
    modeHint.textContent = multi
      ? `已接入 ${count} 个项目，Webhook 会按仓库和分支匹配到对应部署脚本。`
      : '当前只加载默认项目；点击“初始化配置”后可直接在网页添加和管理多个仓库。';
  }
  if (!list) return;
  list.classList.remove('skeleton-block');
  if (!projects.length) {
    list.innerHTML = '<div class="project-empty">暂无项目配置。点击“初始化配置”，再通过“添加仓库”录入项目。</div>';
    return;
  }
  list.innerHTML = projects.map(project => {
    const state = projectState(project);
    const selected = selectedProjectKey === project.key || (!selectedProjectKey && projects.length === 1);
    const last = project.last_deploy || {};
    const head = project.short_head || shortSha(project.head);
    return `
      <button class="project-row ${selected ? 'active' : ''}" type="button" data-project-key="${escapeHtml(project.key)}">
        <div>
          <div class="project-title-line">
            <span class="project-name">${escapeHtml(project.name || project.key)}</span>
            <span class="badge ${state.level}">${state.label}</span>
          </div>
          <div class="project-repo mono" title="${escapeHtml(projectRepoText(project))}">${escapeHtml(projectRepoText(project))}</div>
        </div>
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
      </button>
    `;
  }).join('');
  list.querySelectorAll('.project-row[data-project-key]').forEach(row => {
    row.addEventListener('click', () => {
      selectedProjectKey = row.dataset.projectKey || '';
      updateProjectSelect(projects);
      renderLogs();
      refresh();
    });
  });
}

function updateProjectActionHint(activeProject, projects = []) {
  const hint = $('projectActionHint');
  if (!hint) return;
  if (!activeProject) {
    hint.textContent = projects.length > 1
      ? '选择具体项目后可重新部署或回滚；全部项目用于总览。'
      : '项目配置加载中。';
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

function renderHistoryBars(items) {
  const bars = items.slice(0, 60);
  while (bars.length < 60) bars.push(null);
  return bars.map(item => `
    <button
      type="button"
      class="history-bar ${historyClass(item)}"
      title="${escapeHtml(itemTitle(item))}"
      aria-label="${escapeHtml(itemTitle(item))}">
    </button>
  `).join('');
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

function resourceBarCount(el) {
  const width = measuredTrackWidth(el);
  const barWidth = 4.75;
  const gap = 2;
  if (!Number.isFinite(width) || width < 48) {
    return Number(el.dataset.barCount) || (el.classList.contains('meter-track') ? 30 : 60);
  }
  return Math.max(1, Math.floor((width + gap) / (barWidth + gap)));
}

function updateResourceTrack(el, value) {
  if (!el) return;
  el.dataset.resourceValue = value ?? '';
  const count = resourceBarCount(el);
  el.dataset.barCount = String(count);
  if (el.children.length !== count) {
    el.innerHTML = resourceBars(null, count);
  }
  const n = Number(value);
  const percent = Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
  const active = Math.round((percent / 100) * count);
  const level = resourceLevel(percent);
  window.requestAnimationFrame(() => {
    Array.from(el.children).forEach((bar, index) => {
      bar.className = `resource-bar ${index < active ? `active ${level}` : 'empty'}`;
      bar.style.setProperty('--bar-index', index);
    });
    window.requestAnimationFrame(() => {
      const nextCount = resourceBarCount(el);
      if (nextCount !== count) updateResourceTrack(el, value);
    });
  });
}

function setResourceBar(id, value) {
  updateResourceTrack($(id), value);
}

function animateNumberText(id, value, formatter) {
  const el = $(id);
  if (!el) return;
  const next = Number(value);
  if (!Number.isFinite(next)) {
    el.dataset.value = '';
    el.textContent = formatter(value);
    return;
  }
  const previous = Number(el.dataset.value);
  const start = Number.isFinite(previous) ? previous : 0;
  el.dataset.value = String(next);
  if (Math.abs(start - next) < 0.05) {
    el.textContent = formatter(next);
    return;
  }
  const startedAt = performance.now();
  const duration = 520;
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  const step = (now) => {
    if (el.dataset.value !== String(next)) return;
    const progress = Math.min(1, (now - startedAt) / duration);
    const current = start + (next - start) * easeOut(progress);
    el.textContent = formatter(current);
    if (progress < 1) window.requestAnimationFrame(step);
    else el.textContent = formatter(next);
  };
  window.requestAnimationFrame(step);
}

function setView(view) {
  activeView = view === 'server' ? 'server' : 'deploy';
  $('deployView')?.classList.toggle('active', activeView === 'deploy');
  $('serverView')?.classList.toggle('active', activeView === 'server');
  $('deployViewTab')?.classList.toggle('active', activeView === 'deploy');
  $('serverViewTab')?.classList.toggle('active', activeView === 'server');
  $('panelTitle').textContent = activeView === 'server' ? '服务器健康面板' : '部署健康面板';
  $('panelSubtitle').textContent = activeView === 'server'
    ? '查看服务器资源、网络吞吐和 Docker 容器运行状态。'
    : '监控 Gitee WebHook、部署队列、最近发布和线上健康检查。';
  if (activeView === 'server') {
    clearRefresh();
    refreshServerStatus();
  } else {
    clearServerRefresh();
    refresh();
  }
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
            <span class="stat-pill">Gitee</span>
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
          <dl class="deploy-detail-list">
            <div><dt>Source</dt><dd title="${detailValue(item.source)}">${detailValue(item.source)}</dd></div>
            <div><dt>Actor</dt><dd title="${detailValue(item.actor)}">${detailValue(item.actor)}</dd></div>
            <div><dt>Started</dt><dd title="${detailValue(item.started_at)}">${detailValue(item.started_at)}</dd></div>
            <div><dt>Finished</dt><dd title="${detailValue(item.finished_at)}">${detailValue(item.finished_at)}</dd></div>
            <div><dt>Before</dt><dd class="mono" title="${detailValue(item.before)}">${detailValue(shortSha(item.before))}</dd></div>
            <div><dt>After</dt><dd class="mono" title="${detailValue(item.after)}">${detailValue(shortSha(item.after))}</dd></div>
            <div><dt>Author</dt><dd title="${detailValue(item.commit_author)}">${detailValue(item.commit_author)}</dd></div>
            <div><dt>Ref</dt><dd title="${detailValue(item.ref)}">${detailValue(item.ref)}</dd></div>
            <div class="wide-detail"><dt>Message</dt><dd title="${detailValue(item.commit_message)}">${detailValue(item.commit_message)}</dd></div>
          </dl>
        </details>
      </div>
      <div class="stat-history">
        <div class="history-head">
          <span>History (60pts)</span>
          <span class="history-next">Next update in ${refreshSeconds}s</span>
        </div>
        <div class="history-bars">
          <div class="history-track">${renderHistoryBars(historyItems)}</div>
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
    node: 'Node / PM2',
    java: 'Java / systemd',
    go: 'Go / systemd',
    static: '静态网站',
    custom: '自定义脚本',
  })[template] || '自定义脚本';
}

function deployStepsForTemplate(template, project) {
  const service = normalizedProjectKeyFromForm(project);
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
    java: [
      'if [ -x ./gradlew ]; then ./gradlew clean build -x test; else mvn clean package -DskipTests; fi',
      `systemctl restart ${shellQuote(service)}`,
      ...healthLine,
    ],
    go: [
      'go build -o app ./cmd/server',
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
  const health = $('projectHealthInput');
  if (force || !workdir.value || workdir.value === '/root/' || workdir.value.includes('/root/example')) {
    workdir.value = `/srv/${key}`;
  }
  if (force || !script.value || script.value.includes('/root/example')) {
    script.value = `/srv/${key}/deploy/deploy.sh`;
  }
  if (force || !log.value || log.value.includes('example-deploy')) {
    log.value = `/var/log/vibepilot/${key}-deploy.log`;
  }
  if ((force || !health.value) && template !== 'custom') {
    health.value = health.value || `https://${key}.example.com/health`;
  }
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
  return [
    `# 项目类型：${projectTemplateLabel(template)}`,
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
    '# 2. 准备部署脚本；下面已按项目类型生成，可按需微调',
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
    '',
    '# 3. 回到部署面板保存项目配置；再到 Gitee WebHook 填写页面给出的 URL 和 Token',
  ].join('\n');
}

function generateWebhookGuide(project) {
  const key = normalizedProjectKeyFromForm(project);
  const token = project.webhook_secret || '<保存项目后自动生成 Token>';
  return [
    'Gitee WebHook 配置：',
    `URL: ${projectWebhookUrl(key)}`,
    '请求方式: POST',
    '触发事件: Push',
    `分支: ${project.branch || 'master'}`,
    `密码/Token: ${token}`,
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
  editingProjectKey = project ? project.key : '';
  setProjectFormError('');
  $('projectModalTitle').textContent = project ? '编辑仓库' : '添加仓库';
  $('projectModalHint').textContent = project
    ? '修改会写入项目配置并立即热加载。脚本路径变更后请先执行下方服务器指令。'
    : '填写仓库与部署脚本。先执行下方服务器指令，再保存并配置 WebHook。';
  $('projectOriginalKey').value = project ? project.key : '';
  $('projectKeyInput').value = project ? project.key : '';
  $('projectNameInput').value = project ? project.name : '';
  $('projectTemplateInput').value = project ? (project.template || 'custom') : 'docker';
  $('projectRepoInput').value = project ? project.repo : '';
  $('projectBranchInput').value = project ? project.branch : 'main';
  $('projectTimeoutInput').value = project ? project.timeout_seconds : 900;
  $('projectWorkdirInput').value = project ? project.workdir : '';
  $('projectScriptInput').value = project ? project.script : '';
  $('projectRollbackInput').value = project ? project.rollback_script : '';
  $('projectHealthInput').value = project ? project.health_url : '';
  $('projectLogInput').value = project ? project.deploy_log_file : '';
  $('projectSecretInput').value = project ? project.webhook_secret : '';
  $('projectEnabledInput').checked = project ? Boolean(project.enabled) : false;
  $('projectManualInput').checked = project ? Boolean(project.manual_deploy_enabled) : true;
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
    return lastConfig;
  } catch (err) {
    lastConfig = null;
    return null;
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
  const actions = [{ action: 'logs', label: '日志' }];
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
    >${escapeHtml(item.label)}</button>
  `).join('');
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

function renderSystemStatus(system = {}) {
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

  const dockerBadge = $('dockerBadge');
  dockerBadge.className = `badge ${docker.available ? 'success' : 'failed'}`;
  dockerBadge.textContent = docker.available ? `${containers.length} 个容器` : 'Docker 不可用';
  $('dockerHint').textContent = docker.available
    ? `采样 ${server.sampled_at || '-'} · 缓存 ${server.cache_seconds || 0}s`
    : (docker.error || '无法读取 Docker 状态');

  const list = $('containerList');
  list.classList.remove('skeleton-block');
  if (!docker.available) {
    list.innerHTML = `<div class="project-empty">${escapeHtml(docker.error || 'Docker 不可用')}</div>`;
    return;
  }
  if (!containers.length) {
    list.innerHTML = '<div class="project-empty">暂无容器</div>';
    return;
  }
  list.innerHTML = containers.map(container => {
    const stateClass = containerStateClass(container);
    const title = container.name || container.id || '-';
    return `
      <article class="container-row">
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
  }).join('');
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
  $('containerLogCount').textContent = `${lines.length} 行`;
  $('containerLogsMeta').textContent = meta || `${lines.length} 行`;
  $('containerLogsContent').innerHTML = lines.length
    ? renderLogLines(lines)
    : '<span class="log-line">暂无日志</span>';
  $('containerLogsModal').hidden = false;
}

async function showContainerLogs(container) {
  openContainerLogsModal(container, ['加载中...'], '读取最近 200 行');
  try {
    const data = await fetchJson(`docker/logs?container=${encodeURIComponent(container)}&tail=200`);
    openContainerLogsModal(data.container || container, data.lines || [], `最近 ${(data.lines || []).length} 行`);
  } catch (err) {
    openContainerLogsModal(container, [`读取日志失败: ${err.message}`], '读取失败');
  }
}

async function showContainerLogs(container, tail = currentContainerLogLineLimit) {
  const safeTail = Number(tail || 200);
  currentContainerLogName = container;
  openContainerLogsModal(container, ['加载中...'], `读取最近 ${safeTail} 行`);
  try {
    const data = await fetchJson(`docker/logs?container=${encodeURIComponent(container)}&tail=${encodeURIComponent(safeTail)}`);
    openContainerLogsModal(data.container || container, data.lines || [], `最近 ${(data.lines || []).length} 行`);
  } catch (err) {
    openContainerLogsModal(container, [`读取日志失败: ${err.message}`], '读取失败');
  }
}

function downloadCurrentContainerLog() {
  if (!currentContainerLogName) return;
  const selected = $('containerLogDownloadSelect').value || 'current';
  const lines = selected === 'current' ? currentContainerLogLineLimit : selected;
  window.location.href = logsUrl('docker/logs/download', {
    container: currentContainerLogName,
    lines,
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
  if (action === 'logs') {
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
  updateProjectSelect(projects);
  renderProjectOverview(projects, agent);
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
    const canCancel = Boolean((activeProject && (activeProject.running || Number(activeProject.queue_size || 0) > 0)) || state.running || Number(state.queue_size || 0) > 0);
    cancelDeployBtn.disabled = !canCancel;
    cancelDeployBtn.title = canCancel ? '取消当前运行或排队的部署任务' : '当前没有可取消的部署任务';
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
              <span class="stat-pill">Gitee</span>
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
  const selected = $('logDownloadSelect').value || 'current';
  const lines = selected === 'current' ? currentLogLineLimit : selected;
  const params = { kind: currentLogKind, lines };
  if (currentLogKind === 'deploy' && selectedProjectKey) {
    params.project = selectedProjectKey;
  }
  window.location.href = logsUrl('logs/download', params);
}

async function refresh() {
  try {
    const [status, logs, config] = await Promise.all([
      fetchJson('status'),
      fetchJson(logsUrl('logs', { lines: currentLogLineLimit })),
      fetchJson('projects-config'),
    ]);
    csrfToken = status.csrf_token || csrfToken;
    lastConfig = config;
    lastProjects = config.projects || [];
    renderStatus(status);
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
  if (activeView !== 'server') return;
  serverRefreshTimer = window.setTimeout(refreshServerStatus, serverRefreshSeconds * 1000);
}

async function refreshServerStatus() {
  if (activeView !== 'server') return;
  clearServerRefresh();
  try {
    const status = await fetchJson('status');
    renderSystemStatus(status.system || {});
    $('updatedAt').textContent = `服务器刷新于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  } catch (err) {
    $('dockerHint').textContent = `服务器状态刷新失败: ${err.message}`;
  } finally {
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

async function manualRedeploy() {
  const projectParam = selectedProjectKey ? `?project=${encodeURIComponent(selectedProjectKey)}` : '';
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
  const projectParam = selectedProjectKey ? `?project=${encodeURIComponent(selectedProjectKey)}` : '';
  const confirmed = await showConfirmDialog({
    title: '取消部署',
    message: `确认取消 ${selectedProjectKey || '当前'} 部署任务？正在运行的脚本会收到终止信号。`,
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
      button.textContent = '取消';
    }, 1200);
  }
}

$('refreshBtn').addEventListener('click', () => {
  if (activeView === 'server') refreshServerStatus();
  else refresh();
});
$('deployViewTab').addEventListener('click', () => setView('deploy'));
$('serverViewTab').addEventListener('click', () => setView('server'));
document.querySelectorAll('[data-server-refresh]').forEach(button => {
  button.addEventListener('click', () => setServerRefreshInterval(button.dataset.serverRefresh));
});
$('redeployBtn').addEventListener('click', manualRedeploy);
$('rollbackBtn').addEventListener('click', manualRollback);
$('cancelDeployBtn').addEventListener('click', cancelDeploy);
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
  'projectRollbackInput',
  'projectHealthInput',
  'projectLogInput',
  'projectSecretInput',
].forEach(id => {
  const input = $(id);
  if (input) input.addEventListener('input', updateWebhookPreview);
});
$('projectTemplateInput').addEventListener('change', () => {
  applyTemplateDefaults(true);
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
$('copyProjectCommandsBtn').addEventListener('click', () => copyFromElement('projectSetupCommands', 'copyProjectCommandsBtn'));
$('copyWebhookGuideBtn').addEventListener('click', () => copyFromElement('projectWebhookGuide', 'copyWebhookGuideBtn'));
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
document.addEventListener('click', () => {
  closeProjectMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (closeConfirmDialog(false)) return;
    closeProjectMenu();
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
  currentLogLineLimit = Number($('logLineSelect').value || 200);
  refreshLogsOnly();
});
$('downloadLogBtn').addEventListener('click', downloadCurrentLog);
$('containerLogLineSelect').addEventListener('change', () => {
  currentContainerLogLineLimit = Number($('containerLogLineSelect').value || 200);
  if (currentContainerLogName && !$('containerLogsModal').hidden) {
    showContainerLogs(currentContainerLogName, currentContainerLogLineLimit);
  }
});
$('downloadContainerLogBtn').addEventListener('click', downloadCurrentContainerLog);
refresh();
