(() => {
  const root = document.getElementById('setupView');
  if (!root) return;
  const el = id => document.getElementById(id);
  const labels = {verified: '已核验', available: '依赖就绪', pending: '待验证', setup: '按需配置', error: '需处理', unsupported: '当前环境不支持'};
  const views = new Set(['server', 'requests', 'terminal', 'security', 'notify', 'events', 'certificates']);
  const h = escapeHtml;
  let visible = false, timer = null, controller = null, sequence = 0;
  const expanded = new Set();

  function render(data) {
    const summary = data.summary || {};
    el('setupSummary').textContent = `${summary.verified || 0} 项已核验 · ${summary.error || 0} 项需处理 · ${summary.pending || 0} 项待验证 · ${summary.setup || 0} 项可按需配置`;
    const items = Array.isArray(data.items) ? data.items : [];
    const existing = [...el('setupItems').querySelectorAll('[data-setup-row]')];
    const byKey = new Map(items.map(item => [String(item.key), item]));
    if (existing.length && existing.length === items.length && existing.every(row => byKey.has(row.dataset.setupRow))) {
      // Update evidence in place so polling never replaces a focused instruction.
      existing.forEach(row => {
        const item = byKey.get(row.dataset.setupRow);
        const state = Object.hasOwn(labels, item.state) ? item.state : 'pending';
        row.dataset.state = state;
        const badge = row.querySelector('.setup-state'), detail = row.querySelector('.setup-evidence > p');
        badge.dataset.state = state;
        if (badge.textContent !== labels[state]) badge.textContent = labels[state];
        if (detail.textContent !== item.detail) detail.textContent = item.detail;
      });
    } else {
      el('setupItems').innerHTML = [false, true].map(optional => {
        const rows = items.filter(item => item.optional === optional);
        return `<section class="setup-group"><h3>${optional ? '按需启用' : '基础运行'}</h3>${rows.map(item => {
          const state = Object.hasOwn(labels, item.state) ? item.state : 'pending';
          const key = String(item.key || '');
          return `<article class="setup-row" data-state="${state}" data-setup-row="${h(key)}">
          <div class="setup-feature"><span class="setup-state" data-state="${state}">${labels[state]}</span><h4>${h(item.title)}</h4></div>
          <div class="setup-evidence"><p>${h(item.detail)}</p><details data-setup-detail="${h(key)}" ${expanded.has(key) ? 'open' : ''}><summary>配置与验收步骤</summary><ol>${(item.steps || []).map(step => `<li>${h(step)}</li>`).join('')}</ol></details></div>
          <button class="ghost-button compact-action" type="button" data-setup-view="${h(views.has(item.view) ? item.view : 'server')}" data-setup-key="${h(key)}">${h(item.action || '查看设置')}</button>
          </article>`;
        }).join('')}</section>`;
      }).join('');
      el('setupItems').querySelectorAll('details').forEach(detail => detail.addEventListener('toggle', () => {
        if (detail.open) expanded.add(detail.dataset.setupDetail);
        else expanded.delete(detail.dataset.setupDetail);
      }));
    }
    el('setupCheckedAt').textContent = `${data.environment || ''} · 检查于 ${new Date(data.generated_at * 1000).toLocaleTimeString('zh-CN', {hour12: false})}`;
  }

  function pause() {
    clearTimeout(timer);
    sequence++;
    controller?.abort(); controller = null;
    el('setupRefresh').disabled = false;
    el('setupItems').setAttribute('aria-busy', 'false');
  }

  async function refresh() {
    if (!visible || document.hidden) return;
    pause();
    const current = sequence;
    const request = new AbortController(); controller = request;
    const timeout = setTimeout(() => request.abort(), 10000);
    el('setupRefresh').disabled = true;
    el('setupItems').setAttribute('aria-busy', 'true');
    el('setupFeedback').textContent = '正在核验后台采样与已保存配置…';
    try {
      const response = await fetch('setup/checks', {credentials: 'same-origin', signal: request.signal});
      if (response.status === 401) { location.reload(); return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '检查失败，请重试。');
      if (!Array.isArray(data.items) || !data.items.length || !Number.isFinite(data.generated_at)) throw new Error('检查结果格式异常，请重试。');
      if (sequence !== current || !visible) return;
      render(data);
      el('setupFeedback').textContent = '';
    } catch (error) {
      if (sequence !== current || !visible) return;
      const retained = el('setupItems').children.length ? ' 下方保留上次结果。' : '';
      el('setupFeedback').textContent = (request.signal.aborted ? '检查超时，稍后自动重试。' : `检查未完成：${error.message}`) + retained;
    } finally {
      clearTimeout(timeout);
      if (sequence === current && visible) {
        el('setupRefresh').disabled = false;
        el('setupItems').setAttribute('aria-busy', 'false');
        controller = null;
        timer = setTimeout(refresh, 15000);
      }
    }
  }

  el('setupRefresh').addEventListener('click', refresh);
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-setup-view]');
    if (!button) return;
    setView(button.dataset.setupView);
    if (button.dataset.setupKey === 'ai') el('aiSettingsOpen')?.click();
    if (button.dataset.setupKey === 'history') el('serverPerformanceOpen')?.click();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) pause();
    else if (visible) refresh();
  });
  window.SetupChecks = {refresh, viewChanged(view) {
    visible = view === 'setup';
    if (visible) refresh();
    else pause();
  }};
})();
