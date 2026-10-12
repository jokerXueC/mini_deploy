(() => {
  'use strict';
  const el = id => document.getElementById(id), h = escapeHtml;
  const dialog = document.createElement('dialog'); dialog.className = 'ops-dialog'; dialog.id = 'maintenanceDialog';
  dialog.setAttribute('aria-labelledby', 'maintenanceTitle');
  dialog.innerHTML = `<header class="ops-head"><div><h2 id="maintenanceTitle">面板维护</h2><p>mini_deploy</p></div><button type="button" class="toolbar-icon" data-maintenance-close title="关闭" aria-label="关闭面板维护"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></header><p id="maintenanceFeedback" class="ops-feedback" role="status"></p><div class="ops-pane"><div class="maintenance-release"><div><span>当前程序版本</span><strong id="maintenanceVersion">读取中</strong></div><div><span>最新检查结果</span><strong id="maintenanceLatest">尚未检查</strong></div></div><label class="ops-caption">更新来源 <select id="maintenanceChannel"><option value="gitee">Gitee</option><option value="github">GitHub</option></select></label><div class="maintenance-actions"><button id="maintenanceCheck" type="button" class="ghost-button compact-action">检查更新</button><button id="maintenanceUpdate" type="button" class="ghost-button compact-action" disabled>更新面板</button><button id="maintenanceBackup" type="button" class="ghost-button compact-action">创建备份</button></div><p id="maintenanceNotice" class="ops-caption"></p><p id="maintenanceJob" class="ops-caption" role="status"></p><h3 class="ops-incident-title">保留的备份</h3><p id="maintenanceBackupPath" class="ops-caption"></p><ul id="maintenanceBackups" class="maintenance-backups"></ul></div>`;
  document.body.append(dialog); window.AppSelects?.syncAll();
  let state = {}, checked = null, timer, busy = false, generation = 0, controller;
  const date = at => at ? new Date(at * 1000).toLocaleString('zh-CN', {hour12:false}) : '-';
  const message = text => { el('maintenanceFeedback').textContent = text; };
  const running = () => (state.jobs || []).some(j => j.state === 'queued' || j.state === 'running');
  function controls() {
    ['maintenanceCheck', 'maintenanceBackup'].forEach(id => el(id).disabled = busy || running() || !state.supported);
    el('maintenanceUpdate').disabled = busy || running() || !state.supported || !checked || checked.channel !== el('maintenanceChannel').value || checked.revision === state.version?.revision;
    el('maintenanceChannel').disabled = busy || running();
    dialog.querySelectorAll('[data-maintenance-action]').forEach(b => b.disabled = busy || running() || !state.supported);
    window.AppSelects?.syncAll();
  }
  async function refresh() {
    clearTimeout(timer); const run = ++generation; controller?.abort(); controller = new AbortController();
    try {
      const response = await fetch('panel-maintenance', {credentials:'same-origin', signal:controller.signal});
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || data.error);
      if (run !== generation || !dialog.open) return;
      state = data; csrfToken = data.csrf_token || csrfToken;
      const version = data.version || {};
      el('maintenanceVersion').textContent = version.revision ? version.revision.slice(0, 12) : version.fingerprint ? `程序 ${version.fingerprint.slice(0, 12)}` : '未识别安装';
      el('maintenanceVersion').title = version.revision || version.fingerprint || '';
      el('maintenanceNotice').textContent = data.notice;
      el('maintenanceBackupPath').textContent = `${data.backup_directory || ''} · 不自动删除备份`;
      const latest = data.jobs?.[0];
      el('maintenanceJob').textContent = latest ? `${date(latest.started_at)} · ${latest.phase}${latest.error ? ` · ${latest.error}` : ''}` : '';
      el('maintenanceBackups').innerHTML = data.backups?.length ? data.backups.map(b => `<li><div>${h(b.kind === 'program' ? '程序快照' : '完整安装备份')} · ${date(b.at)}<small>${h(b.id)} · 使用前会重新校验</small></div><div class="maintenance-actions"><button type="button" class="ghost-button compact-action" data-maintenance-action="verify" data-maintenance-backup="${h(b.id)}">校验</button>${b.kind === 'program' ? `<button type="button" class="ghost-button compact-action" data-maintenance-action="restore" data-maintenance-backup="${h(b.id)}">恢复程序</button>` : ''}</div></li>`).join('') : '<li>尚无已完成的备份</li>';
      message(data.warning || (running() ? '维护任务正在独立运行；重启期间页面会自动重连。' : ''));
      controls(); if (running()) timer = setTimeout(refresh, 2500);
    } catch (error) {
      if (run !== generation || error.name === 'AbortError' || !dialog.open) return;
      message(`面板暂不可访问，正在等待重连：${error.message}`); timer = setTimeout(refresh, 5000);
    }
  }
  async function act(action, backup) {
    const prompts = {backup: '创建并校验完整安装备份与程序快照？不会删除现有备份。', verify:'校验所选备份？',
      restore:'恢复所选面板程序版本？面板会短暂断开。操作前会备份当前程序；配置、请求历史和业务文件保留。',
      update:'更新面板程序？面板会短暂断开。操作前会保存当前版本；未通过健康检查时尝试恢复旧程序。配置、数据及业务文件保留。'};
    if (!confirm(prompts[action])) return;
    busy = true; controls(); message('提交维护任务…');
    try {
      await postJsonBody('panel-maintenance', {action, backup, confirm:true, channel:checked?.channel, revision:checked?.revision});
      await refresh();
    } catch (error) { message(error.message); }
    finally { busy = false; controls(); }
  }
  el('panelMaintenanceOpen')?.addEventListener('click', () => { dialog.showModal(); refresh(); });
  dialog.addEventListener('close', () => { generation++; controller?.abort(); clearTimeout(timer); window.AppSelects?.close(); });
  dialog.addEventListener('click', event => {
    if (event.target.closest('[data-maintenance-close]')) dialog.close();
    const button = event.target.closest('[data-maintenance-action]');
    if (button) act(button.dataset.maintenanceAction, button.dataset.maintenanceBackup);
  });
  el('maintenanceCheck').addEventListener('click', async () => {
    busy = true; checked = null; controls(); message('检查官方仓库版本…');
    try { checked = await postJsonBody('panel-maintenance/check', {channel:el('maintenanceChannel').value}); el('maintenanceLatest').textContent = checked.revision === state.version?.revision ? '已是所检查版本' : checked.revision.slice(0, 12); message('版本检查完成，更新只使用本次检查的版本。'); }
    catch (error) { el('maintenanceLatest').textContent = '检查失败'; message(error.message); }
    finally { busy = false; controls(); }
  });
  el('maintenanceChannel').addEventListener('change', () => { checked = null; el('maintenanceLatest').textContent = '尚未检查'; controls(); });
  el('maintenanceUpdate').addEventListener('click', () => act('update'));
  el('maintenanceBackup').addEventListener('click', () => act('backup'));
})();
