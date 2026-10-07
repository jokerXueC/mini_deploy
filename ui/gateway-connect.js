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
    catch (err) { feedback.textContent = err.message; $('connectHelp').open = true; }
    finally {
      busy = false;
      $('gatewayConnectFields').disabled = false;
      $('gatewayConnectClose').disabled = false;
      window.AppSelects?.syncAll();
    }
  }

  async function discover() {
    invalidate(true);
    const current = version;
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
      }
      feedback.textContent = sources.length ? `发现 ${sources.length} 个入口，请选择后读取网站规则。`
        : '没有找到可自动识别的入口，请根据下方查询结果填写。';
      if (result.errors?.length) feedback.textContent += ` ${result.errors.join('；')}`;
      if (!sources.length) $('connectHelp').open = true;
    });
  }

  function chooseRoute() {
    invalidate();
    const route = inspected?.routes.find(item => item.id === $('connectRoute').value);
    $('connectInternalField').hidden = route?.kind !== 'static';
    adjustInternalPort();
    if (route) {
      $('connectRouteNotice').textContent = route.kind === 'static'
        ? '静态文件仍由原 Caddy 提供；新增内部 HTTP 入口，不新增宿主机端口映射。'
        : `原后端：${route.upstream}。Docker 服务名需选择与业务容器共有的网络。`;
      if (!$('connectExisting').value) {
        $('connectKey').value = ('web-' + route.site.toLowerCase().replace(/[^a-z0-9-]/g, '-')).slice(0, 40);
        const match = route.label.match(/\/[A-Za-z0-9/_-]*\*/);
        $('connectProbe').value = match ? match[0].replace(/\*$/, '') : '/';
      }
    }
  }

  $('gatewayConnectOpen').addEventListener('click', () => {
    if (busy) return;
    panel.hidden = false;
    $('requestGatewayForm').hidden = true;
    discover();
  });
  $('gatewayConnectClose').addEventListener('click', () => { if (!busy) { invalidate(true); panel.hidden = true; } });
  $('connectDiscover').addEventListener('click', discover);
  $('connectDetected').addEventListener('change', () => {
    const item = sources[Number($('connectDetected').value)];
    if ($('connectDetected').value !== '' && item) showSource(item);
    else invalidate(true);
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

  $('connectInspect').addEventListener('click', () => {
    if (busy) return;
    invalidate(true);
    const current = version, selected = source();
    work('正在读取配置和网站规则…', async () => {
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
      $('connectExisting').replaceChildren(new Option('新建网关', ''), ...window.RequestGateway.list()
        .filter(entry => !entry.connection || entry.connection.state === 'not_connected')
        .map(entry => new Option(entry.name, entry.key)));
      ['connectKey', 'connectPort', 'connectImage'].forEach(id => $(id).disabled = false);
      $('connectPort').value = availablePort();
      const supported = (result.routes || []).filter(route => route.supported);
      $('connectRouteFields').hidden = false;
      if (supported.length === 1) { $('connectRoute').value = supported[0].id; chooseRoute(); }
      feedback.textContent = supported.length ? '请选择需要监控的规则，并预览接入改动。' : '没有可自动接入的规则，请查看规则说明和下方查询指引。';
      if (!supported.length) $('connectHelp').open = true;
    });
  });

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
      $('connectReview').hidden = false;
      feedback.textContent = '预览已生成，确认后才会改动代理配置。';
    });
  });
  $('connectApply').addEventListener('click', () => {
    if (busy || !plan) return;
    const current = version, reviewed = plan;
    work('等待确认…', async () => {
      const confirmed = await showConfirmDialog({title: '启用网站请求监控',
        message: `${reviewed.after_rule}。${reviewed.notice}`, confirmText: '确认并启用', danger: true});
      if (!confirmed || current !== version) { feedback.textContent = '未执行接入。'; return; }
      feedback.textContent = '正在启动网关并应用入口配置，请等待…';
      try {
        await postJsonBody('gateway-connections', {action: 'apply', key: reviewed.gateway.key, token: reviewed.token, confirmed: true});
        invalidate(true);
        feedback.textContent = '路由已配置接入。请访问所选网站或路径，再查看请求记录；原代理与网关均保留运行。';
      } finally { await window.RequestGateway.refresh(); }
    });
  });

  async function disconnect(entry) {
    if (busy) return;
    panel.hidden = false;
    await work('等待确认撤销…', async () => {
      const confirmed = await showConfirmDialog({title: '撤销网站接入',
        message: `恢复 ${entry.connection.site} 原有入口规则。Caddy 将重启，已有连接可能中断；网关保留运行，成功后可再停止或删除。`,
        confirmText: '撤销接入', danger: true});
      if (!confirmed) { feedback.textContent = '未执行撤销。'; return; }
      try {
        await postJsonBody('gateway-connections', {action: 'disconnect', key: entry.key, token: entry.connection.token, confirmed: true});
        invalidate(true);
        feedback.textContent = '原入口规则已恢复。网关仍保留，可按需停止或删除。';
      } finally { await window.RequestGateway.refresh(); }
    });
  }
  return {disconnect, isBusy: () => busy};
})();
