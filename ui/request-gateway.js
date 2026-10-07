window.RequestGateway = (() => {
  let entries = [], loaded = false, loading = null, busy = false, editing = null, formVersion = 0;
  const feedback = $('gatewayFeedback');
  const states = {running: '运行中', exited: '已停止', created: '未启动', not_created: '待启动', restarting: '重启中', paused: '已暂停', unknown: '状态不可用'};

  function draw() {
    const selected = $('requestGatewayKey').value;
    $('requestGatewayKey').replaceChildren(...entries.map(entry => {
      const option = document.createElement('option');
      option.value = entry.key;
      option.textContent = entry.connection?.site ? `${entry.upstream} · ${entry.connection.site}` : `自定义入口 · ${entry.name}`;
      return option;
    }));
    if (entries.some(entry => entry.key === selected)) $('requestGatewayKey').value = selected;
    else {
      const connected = entries.find(entry => entry.connection?.state === 'connected');
      if (connected) $('requestGatewayKey').value = connected.key;
    }
    if (!entries.length) $('requestGatewayKey').innerHTML = '<option value="">尚未接入服务</option>';
    $('gatewayEntries').innerHTML = entries.length ? entries.map(entry => `
      <article class="request-gateway-entry">
        <div class="request-gateway-title"><strong>${escapeHtml(entry.name)}</strong><span class="badge ${entry.state === 'running' ? 'success' : 'neutral'}">${escapeHtml(states[entry.state] || entry.state)}</span></div>
        <div class="request-gateway-route"><code>${escapeHtml(entry.caddy_upstream)}</code><span aria-hidden="true"> → </span><code>${escapeHtml(entry.upstream)}</code></div>
        <div class="muted">本机地址：${escapeHtml(entry.local_address)} · 网络：${escapeHtml(entry.network)}</div>
        <p class="gateway-connection-state">${entry.connection?.state === 'connected' ? `路由已接入 · ${escapeHtml(entry.connection.site)}` : entry.connection && entry.connection.state !== 'not_connected' ? '接入操作待恢复，请撤销接入后重试' : '尚未通过向导接入网站流量'}</p>
        <code class="gateway-caddy-command">reverse_proxy ${escapeHtml(entry.caddy_upstream)}</code>
        ${entry.error ? `<p class="form-error">${escapeHtml(entry.error)}</p>` : ''}
        <div class="request-actions">
          ${entry.connection && entry.connection.state !== 'not_connected' ? `<button type="button" class="ghost-button compact-action" data-gateway-action="disconnect" data-key="${escapeHtml(entry.key)}">撤销接入</button>` : ''}
          <button type="button" class="ghost-button compact-action" data-gateway-action="edit" data-key="${escapeHtml(entry.key)}" ${entry.connection && entry.connection.state !== 'not_connected' ? 'disabled title="请先撤销网站接入"' : ''}>编辑</button>
          <button type="button" class="ghost-button compact-action" data-gateway-action="records" data-key="${escapeHtml(entry.key)}">查看请求</button>
          <button type="button" class="ghost-button compact-action" data-gateway-action="${['running', 'paused', 'restarting'].includes(entry.state) ? 'stop' : 'start'}" data-key="${escapeHtml(entry.key)}" ${entry.connection && entry.connection.state !== 'not_connected' && ['running', 'paused', 'restarting'].includes(entry.state) ? 'disabled title="请先撤销网站接入"' : ''}>${['running', 'paused', 'restarting'].includes(entry.state) ? '停止' : '启动'}</button>
          <button type="button" class="ghost-button compact-action" data-gateway-action="delete" data-key="${escapeHtml(entry.key)}" ${!['exited', 'created', 'not_created', 'dead'].includes(entry.state) || (entry.connection && entry.connection.state !== 'not_connected') ? 'disabled' : ''}>删除</button>
        </div>
      </article>`).join('') : '<p class="request-empty">尚未创建入口</p>';
  }

  async function refresh() {
    if (loading) return loading;
    loading = (async () => {
      const result = await fetchJson('request-gateways');
      entries = result.entries || [];
      loaded = true;
      draw();
    })();
    try { await loading; } finally { loading = null; }
  }

  function networkNotice() {
    $('gatewayNetworkNotice').textContent = $('gatewayNetwork').value === 'host'
      ? '后端运行在服务器本机，可填写 http://127.0.0.1:端口。'
      : '网关与业务服务、前置 Caddy/Nginx 需处于同一网络。后端使用服务名；前置代理使用保存后显示的容器地址。';
  }

  async function openForm(entry = null) {
    const version = ++formVersion;
    editing = entry;
    $('requestGatewayForm').hidden = false;
    $('gatewayFormTitle').textContent = entry ? '编辑入口' : '新增入口';
    for (const [id, value] of Object.entries({gatewayKey: entry?.key || '', gatewayName: entry?.name || '',
      gatewayUpstream: entry?.upstream || '', gatewayPort: entry?.port || 18080,
      gatewayBind: entry?.bind || '127.0.0.1', gatewayImage: entry?.image || 'nginx:stable-alpine'})) $(id).value = value;
    $('gatewayTrust').checked = entry?.trust_proxy || false;
    $('gatewayKey').disabled = !!entry;
    ['gatewayPort', 'gatewayBind', 'gatewayImage', 'gatewayNetwork'].forEach(id => $(id).disabled = !!entry && entry.state !== 'not_created');
    $('gatewayNetwork').innerHTML = '<option value="host">服务器本机</option>';
    const addNetwork = name => {
      if ([...$('gatewayNetwork').options].some(option => option.value === name)) return;
      const option = document.createElement('option'); option.value = name; option.textContent = name;
      $('gatewayNetwork').append(option);
    };
    if (entry) addNetwork(entry.network);
    $('gatewayNetwork').value = entry?.network || 'host';
    networkNotice();
    window.AppSelects?.syncAll();
    if (!entry || entry.state === 'not_created') {
      try {
        const result = await fetchJson('request-gateways/networks');
        if (version !== formVersion || $('requestGatewayForm').hidden) return;
        (result.networks || []).forEach(addNetwork);
      } catch (err) { if (version === formVersion && !$('requestGatewayForm').hidden) feedback.textContent = `网络检测失败：${err.message}`; }
    }
  }

  async function act(data, message) {
    if (busy || window.GatewayConnect?.isBusy()) return;
    busy = true;
    let disabledButtons = [];
    try {
      const confirmed = await showConfirmDialog({title: '确认网关操作', message, confirmText: '确认', danger: ['stop', 'delete'].includes(data.action)});
      if (!confirmed) return;
      disabledButtons = [...$('requestGatewayManager').querySelectorAll('button')].map(button => [button, button.disabled]);
      disabledButtons.forEach(([button]) => button.disabled = true);
      feedback.textContent = data.action === 'start' ? '正在准备镜像并启动网关，首次启动可能需要几分钟…' : '正在处理…';
      await postJsonBody('request-gateways', {...data, confirmed: true});
      feedback.textContent = data.action === 'save' ? '配置已保存。运行中的网关已检查并重载；新入口请点击启动。' : '操作完成';
      if (data.action === 'save') $('requestGatewayForm').hidden = true;
      await refresh();
      window.NginxRequests?.refresh();
    } catch (err) { feedback.textContent = `操作失败：${err.message}`; }
    finally {
      busy = false;
      disabledButtons.forEach(([button, disabled]) => button.disabled = disabled);
      $('gatewaySave').disabled = false; $('gatewayCancel').disabled = false;
      $('gatewayRefresh').disabled = false; $('gatewayAdd').disabled = false;
      draw();
    }
  }

  $('manageRequestGateways').addEventListener('click', async () => {
    const manager = $('requestGatewayManager');
    manager.hidden = !manager.hidden;
    $('manageRequestGateways').setAttribute('aria-expanded', String(!manager.hidden));
    if (!manager.hidden) { try { await refresh(); } catch (err) { feedback.textContent = err.message; } }
  });
  $('gatewayAdd').addEventListener('click', () => {
    if (!busy && !window.GatewayConnect?.isBusy()) {
      $('gatewayConnectPanel').hidden = true;
      openForm();
    }
  });
  $('gatewayCancel').addEventListener('click', () => { formVersion++; $('requestGatewayForm').hidden = true; editing = null; });
  $('gatewayNetwork').addEventListener('change', networkNotice);
  $('gatewayRefresh').addEventListener('click', async () => { try { await refresh(); } catch (err) { feedback.textContent = err.message; } });
  $('requestGatewayForm').addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    const spec = {key: $('gatewayKey').value.trim(), name: $('gatewayName').value.trim(),
      upstream: $('gatewayUpstream').value.trim(), port: Number($('gatewayPort').value), bind: $('gatewayBind').value,
      network: $('gatewayNetwork').value, image: $('gatewayImage').value.trim(), trust_proxy: $('gatewayTrust').checked};
    act({action: 'save', spec, revision: editing?.revision || ''},
      `入口 ${spec.name}，后端 ${spec.upstream}，端口 ${spec.port}，网络 ${spec.network}。${spec.bind === '0.0.0.0' ? '所有网卡监听可能允许外部直接访问。' : ''}${spec.trust_proxy ? '仅应允许可信前置代理连接此入口。' : ''}${editing ? '运行中将平滑重载，已有长连接继续使用原后端。' : '保存后还需启动，再切换前置代理上游。'}`);
  });
  $('gatewayEntries').addEventListener('click', event => {
    const button = event.target.closest('[data-gateway-action]');
    if (!button || busy) return;
    const entry = entries.find(item => item.key === button.dataset.key);
    if (!entry) return;
    const action = button.dataset.gatewayAction;
    if (action === 'disconnect') return void window.GatewayConnect.disconnect(entry);
    if (action === 'edit') return void openForm(entry);
    if (action === 'records') {
      $('requestBackend').value = 'gateway'; $('requestGatewayKey').value = entry.key;
      $('requestBackend').dispatchEvent(new Event('change')); return;
    }
    const message = action === 'start' ? `启动入口 ${entry.name}，必要时下载网关镜像。启动后请测试本机地址 ${entry.local_address} 再切换上游。`
      : action === 'stop' ? `停止 ${entry.name} 会中断经过此入口的业务请求和长连接。请先把前置代理切回原业务上游。`
        : `删除 ${entry.name} 的网关容器、配置及容器日志。请确认前置代理已切回原业务上游。`;
    act({action, key: entry.key, revision: entry.revision}, message);
  });
  return {refresh, list: () => entries.slice(), ensure: async () => { if (!loaded) await refresh(); }};
})();
