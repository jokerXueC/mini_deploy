window.GatewayConnect = (() => {
  let sources = [], inspected = null, plan = null, version = 0, busy = false, baseHelp = [];
  const panel = $('gatewayConnectPanel'), feedback = $('gatewayConnectFeedback');
  const sourceIds = ['connectKind', 'connectMode', 'connectContainer', 'connectConfig'];
  const valueIds = ['connectRoute', 'connectExisting', 'connectKey', 'connectPort', 'connectNetwork', 'connectInternal', 'connectProbe', 'connectImage'];

  function invalidate(sourceChanged = false) {
    version++;
    plan = null;
    $('connectReview').hidden = true;
    if (sourceChanged) {
      inspected = null;
      $('connectRouteFields').hidden = true;
    }
  }

  function help(items) {
    const kind = $('connectKind').value, mode = $('connectMode').value;
    $('connectHelpItems').replaceChildren(...items.filter(item => (!item.kind || item.kind === kind) && (!item.mode || item.mode === mode)).map(item => {
      const name = $('connectContainer').value.trim();
      const template = mode === 'local' && item.local_command ? item.local_command : item.command;
      const command = template.replaceAll('替换为容器名称', /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(name) ? name : '替换为容器名称')
        .replaceAll('替换为代理类型', kind === 'nginx' ? 'nginx' : 'caddy');
      const section = document.createElement('section');
      section.className = 'gateway-command-help';
      const title = document.createElement('h4'); title.textContent = item.title;
      const pre = document.createElement('pre'); pre.textContent = command;
      const text = document.createElement('p'); text.textContent = item.fill;
      const copy = document.createElement('button'); copy.type = 'button';
      copy.className = 'ghost-button compact-action'; copy.textContent = '复制指令';
      copy.addEventListener('click', async () => {
        try { await copyText(command); copy.textContent = '已复制'; }
        catch (_) { feedback.textContent = '复制失败，请选择指令文本复制。'; }
      });
      section.append(title, pre, copy, text);
      return section;
    }));
  }

  function source() {
    return {kind: $('connectKind').value, mode: $('connectMode').value,
      container: $('connectContainer').value.trim(), config_path: $('connectConfig').value.trim()};
  }

  function showSource(item) {
    $('connectKind').value = item.kind;
    $('connectMode').value = item.mode;
    $('connectContainer').value = item.container || '';
    $('connectConfig').value = item.config_path || '';
    $('connectContainerField').hidden = item.mode !== 'docker';
    invalidate(true);
    help(baseHelp);
    window.AppSelects?.syncAll();
  }

  async function work(message, operation) {
    if (busy) return;
    busy = true;
    $('gatewayConnectFields').disabled = true;
    $('gatewayConnectClose').disabled = true;
    feedback.textContent = message;
    try { await operation(); }
    catch (err) {
      feedback.textContent = err.message;
      $('connectHelp').open = true;
      $(inspected ? 'connectOptionsAdvanced' : 'connectSourceAdvanced').open = true;
    }
    finally {
      busy = false;
      $('gatewayConnectFields').disabled = false;
      $('gatewayConnectClose').disabled = false;
      window.AppSelects?.syncAll();
    }
  }

  async function discover() {
    if (busy) return;
    invalidate(true);
    const current = version;
    let autoInspectVersion = null;
    await work('正在检测代理入口…', async () => {
      const result = await fetchJson('gateway-connections/discover');
      if (current !== version) return;
      sources = result.sources || [];
      $('connectDetected').replaceChildren(new Option('手动填写', ''), ...sources.map((item, i) => new Option(item.label, String(i))));
      baseHelp = result.help || [];
      help(baseHelp);
      if (sources.length === 1) {
        $('connectDetected').value = '0';
        showSource(sources[0]);
        autoInspectVersion = version;
      }
      $('connectDetectedField').hidden = sources.length === 1;
      $('connectSourceAdvanced').open = !sources.length;
      feedback.textContent = sources.length ? `发现 ${sources.length} 个入口，请选择后读取网站规则。`
        : '没有找到可自动识别的入口，请根据下方查询结果填写。';
      if (result.errors?.length) feedback.textContent += ` ${result.errors.join('；')}`;
      if (!sources.length) $('connectHelp').open = true;
    });
    if (autoInspectVersion === version && !panel.hidden) await inspectSource();
  }

  function chooseRoute() {
    invalidate();
    const route = inspected?.routes.find(item => item.id === $('connectRoute').value);
    $('connectPreview').disabled = !route?.supported;
    $('connectInternalField').hidden = route?.kind !== 'static';
    adjustInternalPort();
    if (route) {
      $('connectRouteNotice').textContent = route.kind === 'static'
        ? `监控范围：${route.site} 的静态网站请求。`
        : `监控范围：${route.site} · ${route.label}。其他转发规则不包含在内。`;
      if (!$('connectExisting').value) {
        const base = ('web-' + route.site.toLowerCase().replace(/[^a-z0-9-]/g, '-')).slice(0, 35);
        const keys = new Set(window.RequestGateway.list().map(entry => entry.key));
        let key = base, suffix = 2;
        while (keys.has(key)) key = `${base}-${suffix++}`;
        $('connectKey').value = key;
        const match = route.label.match(/\/[A-Za-z0-9/_-]*\*/);
        $('connectProbe').value = match ? match[0].replace(/\*$/, '') : '/';
      }
    }
  }

  $('gatewayConnectOpen').addEventListener('click', () => {
    if (busy) return;
    panel.hidden = false;
    $('gatewayConnectTitle').textContent = '添加网站监控';
    $('gatewayConnectFields').hidden = false;
    $('connectHelp').hidden = false;
    $('requestGatewayManager').hidden = true;
    $('manageRequestGateways').setAttribute('aria-expanded', 'false');
    $('connectOptionsAdvanced').open = false;
    $('connectSourceAdvanced').open = false;
    $('connectHelp').open = false;
    $('requestGatewayForm').hidden = true;
    discover();
  });
  $('gatewayConnectClose').addEventListener('click', () => { if (!busy) { invalidate(true); panel.hidden = true; } });
  $('connectDiscover').addEventListener('click', discover);
  $('connectDetected').addEventListener('change', () => {
    const item = sources[Number($('connectDetected').value)];
    if ($('connectDetected').value !== '' && item) { showSource(item); inspectSource(); }
    else { invalidate(true); $('connectSourceAdvanced').open = true; }
  });
  sourceIds.forEach(id => $(id).addEventListener('input', () => {
    invalidate(true);
    $('connectDetected').value = '';
    $('connectContainerField').hidden = $('connectMode').value !== 'docker';
    if (id === 'connectKind') $('connectConfig').value = $('connectKind').value === 'caddy' ? '/etc/caddy/Caddyfile' : '/etc/nginx/conf.d/default.conf';
    help(baseHelp);
  }));
  valueIds.forEach(id => $(id).addEventListener('input', () => {
    invalidate();
    if (id === 'connectPort') adjustInternalPort();
  }));
  $('connectRoute').addEventListener('change', chooseRoute);
  $('connectExisting').addEventListener('change', () => {
    const entry = window.RequestGateway.list().find(item => item.key === $('connectExisting').value);
    ['connectKey', 'connectPort', 'connectImage'].forEach(id => $(id).disabled = !!entry);
    if (entry) {
      $('connectKey').value = entry.key; $('connectPort').value = entry.port; $('connectImage').value = entry.image;
      $('connectNetwork').value = entry.network;
    } else {
      $('connectPort').value = availablePort();
      chooseRoute();
    }
    adjustInternalPort();
    invalidate();
    window.AppSelects?.syncAll();
  });

  function availablePort() {
    const used = new Set(window.RequestGateway.list().map(entry => entry.port));
    let port = 18080;
    while (used.has(port)) port++;
    return port;
  }

  function adjustInternalPort() {
    if ($('connectInternalField').hidden || Number($('connectInternal').value) !== Number($('connectPort').value)) return;
    const used = new Set(window.RequestGateway.list().map(entry => entry.port));
    used.add(Number($('connectPort').value));
    let port = 18081;
    while (used.has(port)) port++;
    $('connectInternal').value = port;
  }

  async function inspectSource() {
    if (busy) return;
    invalidate(true);
    const current = version, selected = source();
    await work('正在读取网站…', async () => {
      const result = await postJsonBody('gateway-connections', {action: 'inspect', source: selected});
      await window.RequestGateway.refresh();
      if (current !== version) return;
      inspected = result;
      help(result.help || []);
      $('connectRoute').replaceChildren(new Option('请选择网站或规则', ''), ...(result.routes || []).map(route => {
        const option = new Option(`${route.site} · ${route.label}${route.reason ? `（${route.reason}）` : ''}`, route.id);
        option.disabled = !route.supported;
        return option;
      }));
      $('connectNetwork').replaceChildren(new Option('请选择共有网络', ''), ...(result.networks || []).map(name => new Option(name === 'host' ? '服务器本机' : name, name)));
      if (result.networks?.length === 1) $('connectNetwork').value = result.networks[0];
      $('connectOptionsAdvanced').open = result.networks?.length !== 1;
      $('connectExisting').replaceChildren(new Option('新建网关', ''), ...window.RequestGateway.list()
        .filter(entry => !entry.connection || entry.connection.state === 'not_connected')
        .map(entry => new Option(entry.name, entry.key)));
      ['connectKey', 'connectPort', 'connectImage'].forEach(id => $(id).disabled = false);
      $('connectPort').value = availablePort();
      const supported = (result.routes || []).filter(route => route.supported);
      $('connectPreview').disabled = true;
      $('connectRouteFields').hidden = false;
      if (supported.length === 1) { $('connectRoute').value = supported[0].id; chooseRoute(); }
      feedback.textContent = supported.length ? '选择需要记录请求的网站。' : '没有可自动接入的网站，请查看规则说明和查询指引。';
      if (supported.length && result.networks?.length !== 1) feedback.textContent += ' 检测到多个网络，请在高级选项中确认共有网络。';
      if (!supported.length) $('connectHelp').open = true;
    });
  }
  $('connectInspect').addEventListener('click', inspectSource);

  $('connectPreview').addEventListener('click', () => {
    if (busy || !inspected) return;
    invalidate();
    const current = version;
    const data = {action: 'preview', source: inspected.source, source_revision: inspected.revision,
      route_id: $('connectRoute').value, key: $('connectKey').value.trim(), port: Number($('connectPort').value),
      network: $('connectNetwork').value, internal_port: Number($('connectInternal').value),
      image: $('connectImage').value.trim(), probe_path: $('connectProbe').value.trim()};
    work('正在生成接入预览，尚未切换流量…', async () => {
      const result = await postJsonBody('gateway-connections', data);
      if (current !== version) return;
      plan = result;
      $('connectBefore').textContent = result.before_rule;
      $('connectAfter').textContent = result.after_rule;
      $('connectReviewNotice').textContent = result.notice;
      $('connectReviewTitle').textContent = `开启 ${result.site} 的请求记录`;
      $('connectReviewScope').textContent = $('connectRouteNotice').textContent;
      $('connectReview').hidden = false;
      feedback.textContent = '预览已生成，确认后才会改动代理配置。';
    });
  });
  $('connectBack').addEventListener('click', () => { if (!busy) invalidate(); });
  $('connectApply').addEventListener('click', () => {
    if (busy || !plan) return;
    const current = version, reviewed = plan;
    work('正在开启记录…', async () => {
      if (current !== version) return;
      feedback.textContent = '正在启动网关并应用入口配置，请等待…';
      try {
        await postJsonBody('gateway-connections', {action: 'apply', key: reviewed.gateway.key, token: reviewed.token, confirmed: true});
        invalidate(true);
        feedback.textContent = '已开启，请访问网站后查看请求记录。';
      } finally { await window.RequestGateway.refresh(); }
      panel.hidden = true;
      window.NginxRequests?.view(reviewed.gateway.key);
    });
  });

  async function disconnect(entry) {
    if (busy) return;
    panel.hidden = false;
    $('gatewayConnectTitle').textContent = '停止网站记录';
    $('gatewayConnectFields').hidden = true;
    $('connectHelp').hidden = true;
    await work('等待确认撤销…', async () => {
      const confirmed = await showConfirmDialog({title: '停止网站请求记录',
        message: `恢复 ${entry.connection.site} 原有访问路径。${entry.connection.source?.kind === 'nginx' ? 'Nginx 将重载。' : '代理配置将重新应用，已有连接可能短暂中断。'} 网站继续提供服务，采集容器保留。`,
        confirmText: '停止记录', danger: true});
      if (!confirmed) { panel.hidden = true; return; }
      try {
        await postJsonBody('gateway-connections', {action: 'disconnect', key: entry.key, token: entry.connection.token, confirmed: true});
        invalidate(true);
        feedback.textContent = '原入口规则已恢复。网关仍保留，可按需停止或删除。';
      } finally { await window.RequestGateway.refresh(); }
      panel.hidden = true;
      window.NginxRequests?.refresh();
    });
  }
  return {disconnect, isBusy: () => busy};
})();
