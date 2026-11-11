(() => {
  const root = document.getElementById('databasesView');
  if (!root) return;
  const el = id => document.getElementById(id);
  let connections = [], tables = [], selected = null, currentTab = 'data', initialized = false;
  let dependencies = {}, csrf = '', allowed = false, busy = false, jobId = '', page = 0, sort = '', direction = 'asc';
  let editing = null, dataResult = null, sqlResult = null, tableLoaded = false, structureLoaded = false;
  let workspaceMode = 'browse', graphLoaded = false, graphSequence = 0;
  const connection = () => connections.find(item => item.id === el('dbConnection').value);
  const database = () => el('dbDatabase').value;

  function feedback(message = '', error = false) {
    el('dbFeedback').textContent = message;
    el('dbFeedback').dataset.error = String(error);
  }
  function options(select, values, placeholder, current = '') {
    select.replaceChildren();
    if (placeholder) select.add(new Option(placeholder, ''));
    values.forEach(item => select.add(new Option(item.label ?? item, item.value ?? item)));
    if ([...select.options].some(option => option.value === current)) select.value = current;
    window.AppSelects?.syncAll();
  }
  function updateControls() {
    const profile = connection(), ready = allowed && !!profile && !!dependencies[profile.engine]?.available;
    ['dbConnection', 'dbNew', 'dbDiscover', 'dbEngine', 'dbTest', 'dbSave'].forEach(id => { el(id).disabled = busy || !allowed; });
    el('dbDatabase').disabled = busy || !ready || !el('dbDatabase').options.length;
    ['dbConnect', 'dbEdit', 'dbRemove', 'dbCatalogRefresh'].forEach(id => { el(id).disabled = busy || !ready; });
    // Editing and removing a profile remain available when its optional driver is missing.
    ['dbEdit', 'dbRemove'].forEach(id => { el(id).disabled = busy || !allowed || !profile; });
    ['dbFilterColumn', 'dbFilterOperator', 'dbFilterValue', 'dbFilterApply', 'dbDataRefresh'].forEach(id => {
      el(id).disabled = busy || !ready || !selected;
    });
    el('dbFilterValue').disabled ||= el('dbFilterOperator').value === 'is_null';
    el('dbRun').disabled = busy || !ready || !database() || !dependencies.sql || !el('dbSql').value.trim();
    el('dbPrevious').disabled = busy || !ready || !dataResult || page === 0;
    el('dbNext').disabled = busy || !ready || !dataResult?.has_more;
    el('dbDataExport').disabled = busy || !dataResult?.rows?.length;
    el('dbSqlExport').disabled = busy || !sqlResult?.rows?.length;
    el('dbCancel').hidden = !busy;
    el('dbCancel').disabled = !jobId;
    el('dbGraphCancel').hidden = !busy;
    el('dbGraphCancel').disabled = !jobId;
    root.querySelectorAll('[data-db-table], [data-db-tab], [data-db-mode]').forEach(button => { button.disabled = busy; });
    window.DatabaseGraph?.setBusy(busy, ready && !!database());
    el('dbFeedback').dataset.running = String(busy);
    root.setAttribute('aria-busy', String(busy));
    el('dbAddress').textContent = profile ? `${profile.host}:${profile.port} · ${profile.user}` : '';
    window.AppSelects?.syncAll();
  }
  async function request(path, data) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 18000);
    try {
      const response = await fetch(path, {method: data ? 'POST' : 'GET', credentials: 'same-origin',
        signal: controller.signal, headers: data ? {'Content-Type': 'application/json', 'X-CSRF-Token': csrf} : {},
        body: data ? JSON.stringify(data) : undefined});
      if (response.status === 401) { location.reload(); throw Error('登录已过期。'); }
      const payload = await response.json();
      if (!response.ok) throw Error(payload.detail || '数据库操作失败。');
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw Error('面板请求超时，请刷新任务状态或稍后重试。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  async function run(operation, extra = {}, draft = false) {
    if (busy) throw Error('请等待当前查询完成或取消后再继续。');
    busy = true; jobId = ''; updateControls();
    feedback(draft ? '正在测试只读连接…' : '正在读取数据库…');
    try {
      let job = await request(draft ? 'databases/test' : 'databases/query', draft ? extra : {
        id: connection()?.id, operation, database: database(), ...extra});
      jobId = job.id; updateControls();
      while (job.state === 'running') {
        await new Promise(resolve => setTimeout(resolve, 350));
        job = await request(`databases/job?id=${encodeURIComponent(job.id)}`);
      }
      if (job.state !== 'succeeded') throw Error(job.error || '查询未完成。');
      feedback(`读取完成 · ${job.elapsed_ms ?? 0} ms`);
      return {...job.result, elapsed_ms: job.elapsed_ms};
    } finally { busy = false; jobId = ''; updateControls(); }
  }
  async function safely(action) {
    try { await action(); } catch (error) {
      feedback(error.message, true);
      ['dbTree', 'dbDataGrid', 'dbFieldsGrid'].forEach(id => {
        if (el(id).textContent.startsWith('正在读取')) emptyGrid(id, '读取未完成，请重试');
      });
    }
  }
  function emptyGrid(id, message) {
    const p = document.createElement('p'); p.className = 'db-empty'; p.textContent = message;
    el(id).replaceChildren(p);
  }
  function clearSelection() {
    selected = null; page = 0; sort = ''; direction = 'asc'; dataResult = sqlResult = null;
    tableLoaded = structureLoaded = false;
    el('dbObjectTitle').textContent = '选择一张表'; el('dbObjectMeta').textContent = '';
    emptyGrid('dbDataGrid', '选择数据表后查看数据'); emptyGrid('dbFieldsGrid', '选择数据表后查看结构');
    el('dbIndexesGrid').replaceChildren(); emptyGrid('dbSqlGrid', '尚未执行查询');
    el('dbDataSummary').textContent = ''; el('dbSqlSummary').textContent = ''; el('dbPage').textContent = '第 1 页';
    options(el('dbFilterColumn'), [], '全部记录'); el('dbFilterValue').value = '';
    updateControls();
  }
  function resetGraph() { graphSequence++; graphLoaded = false; el('dbGraphSchema').value = ''; window.DatabaseGraph?.reset(); }
  async function graph() {
    if (!connection() || !database() || !allowed) return;
    const sequence = ++graphSequence;
    await window.DatabaseGraph.ensure();
    if (sequence !== graphSequence || workspaceMode !== 'graph') return;
    const data = await run('relationships', {schema: el('dbGraphSchema').value});
    if (sequence !== graphSequence) return;
    await window.DatabaseGraph.render(data, (item, tab) => safely(async () => {
      showMode('browse'); await selectTable(item, tab);
    }));
    graphLoaded = true; updateControls();
  }
  function showMode(mode) {
    workspaceMode = mode;
    root.querySelectorAll('[data-db-mode]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.dbMode === mode)));
    el('dbBrowseView').hidden = mode !== 'browse'; el('dbGraphMount').hidden = mode !== 'graph';
    if (mode === 'graph') requestAnimationFrame(() => window.DatabaseGraph?.resize());
  }
  async function selectTable(item, tab = currentTab) {
    clearSelection(); selected = item;
    if (!tables.some(table => table.schema === item.schema && table.name === item.name)) tables.push(item);
    el('dbObjectTitle').textContent = item.name;
    el('dbObjectMeta').textContent = `${item.schema} · ${item.type === 'VIEW' ? '视图' : '数据表'}`;
    tree(); updateControls();
    if (!el('dbSql').value.trim()) el('dbSql').value = starterSql(item);
    currentTab = tab;
    root.querySelectorAll('[data-db-tab]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.dbTab === tab)));
    ['data', 'structure', 'sql'].forEach(name => { el(`db${name[0].toUpperCase() + name.slice(1)}Pane`).hidden = tab !== name; });
    if (tab === 'structure') await structure(); else if (tab === 'data') await browse();
  }
  function tree() {
    const target = el('dbTree'), filter = el('dbTableSearch').value.toLowerCase();
    target.replaceChildren();
    const groups = new Map();
    tables.filter(item => `${item.schema}.${item.name}`.toLowerCase().includes(filter)).forEach((item) => {
      if (!groups.has(item.schema)) {
        const detail = document.createElement('details'); detail.open = true;
        const summary = document.createElement('summary'); summary.textContent = item.schema;
        detail.append(summary); groups.set(item.schema, detail); target.append(detail);
      }
      const button = document.createElement('button'), label = document.createElement('span');
      button.type = 'button'; button.dataset.dbTable = 'true'; button.title = `${item.schema}.${item.name}`;
      button.dataset.selected = String(selected?.schema === item.schema && selected?.name === item.name);
      button.disabled = busy; label.textContent = item.name; button.append(label);
      button.addEventListener('click', () => safely(() => selectTable(item)));
      groups.get(item.schema).append(button);
    });
    if (!groups.size) emptyGrid('dbTree', filter ? '没有匹配的数据表' : '暂无可访问的数据表');
  }
  function starterSql(item) {
    const mark = connection()?.engine === 'postgres' ? '"' : '`';
    const q = value => mark + value.replaceAll(mark, mark + mark) + mark;
    return `SELECT * FROM ${q(item.schema)}.${q(item.name)} LIMIT 50;`;
  }
  function grid(id, result, sortable = false) {
    if (!result?.columns?.length) { emptyGrid(id, '没有返回字段'); return; }
    const table = document.createElement('table'), head = table.createTHead().insertRow(), body = table.createTBody();
    result.columns.forEach((name) => {
      const th = document.createElement('th');
      if (sortable) {
        const button = document.createElement('button'); button.type = 'button';
        button.textContent = name + (sort === name ? direction === 'asc' ? ' ↑' : ' ↓' : '');
        button.title = `按 ${name} 排序`; button.disabled = busy;
        button.addEventListener('click', () => safely(async () => {
          direction = sort === name && direction === 'asc' ? 'desc' : 'asc'; sort = name; page = 0; await browse();
        }));
        th.append(button); if (sort === name) th.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : 'descending');
      } else th.textContent = name;
      head.append(th);
    });
    result.rows.forEach(row => {
      const tr = body.insertRow();
      row.forEach((value, index) => {
        const td = tr.insertCell(); td.dataset.null = String(value === null);
        const content = value === null ? 'NULL' : String(value);
        if (content.length > 80 || content.includes('\n')) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = content;
          button.title = `展开 ${result.columns[index]}`;
          button.addEventListener('click', () => {
            el('dbCellTitle').textContent = result.columns[index];
            el('dbCellContent').textContent = content; el('dbCellDialog').showModal();
          }); td.append(button);
        } else td.textContent = content;
      });
    });
    el(id).replaceChildren(table);
    if (!result.rows.length) {
      const row = body.insertRow(), cell = row.insertCell(); cell.colSpan = result.columns.length;
      cell.textContent = '没有匹配的记录'; cell.className = 'db-empty';
    }
  }
  async function browse() {
    if (!selected) return;
    dataResult = null; updateControls(); emptyGrid('dbDataGrid', '正在读取…');
    const result = await run('browse', {schema: selected.schema, table: selected.name, page, sort, direction,
      filter_column: el('dbFilterColumn').value, filter_operator: el('dbFilterOperator').value,
      filter_value: el('dbFilterValue').value});
    dataResult = result; sort = result.sort; tableLoaded = true;
    const filter = el('dbFilterColumn').value;
    options(el('dbFilterColumn'), result.columns, '全部记录', filter);
    grid('dbDataGrid', result, true);
    el('dbDataSummary').textContent = `本页 ${result.rows.length} 条 · ${result.elapsed_ms} ms${result.truncated && !result.has_more ? ' · 结果已截断' : ''}`;
    el('dbDataSummary').title = result.notice || '';
    if (result.truncation_reason === 'bytes') feedback(result.notice, true);
    el('dbPage').textContent = `第 ${page + 1} 页`; updateControls();
  }
  async function structure() {
    if (!selected) return;
    emptyGrid('dbFieldsGrid', '正在读取字段与索引…'); el('dbIndexesGrid').replaceChildren();
    const result = await run('structure', {schema: selected.schema, table: selected.name});
    structureLoaded = true; grid('dbFieldsGrid', result.fields); grid('dbIndexesGrid', result.indexes);
  }
  async function catalog() {
    resetGraph();
    clearSelection(); tables = []; emptyGrid('dbTree', '正在读取库表…');
    el('dbConnectionState').textContent = '正在连接…';
    let result;
    try { result = await run('catalog'); }
    catch (error) { el('dbConnectionState').textContent = '连接失败'; throw error; }
    options(el('dbDatabase'), result.databases, '选择数据库', result.database);
    if (result.database && !result.databases.includes(result.database)) el('dbDatabase').add(new Option(result.database, result.database));
    el('dbDatabase').value = result.database;
    tables = result.tables; tree(); updateControls();
    options(el('dbGraphSchema'), [...new Set(tables.map(item => item.schema))], '全部 Schema');
    el('dbConnectionState').textContent = '已连接';
    if (result.truncated) feedback('目录已截断，请缩小范围；初版最多显示 500 个数据库、1000 张表。');
    if (workspaceMode === 'graph') await graph();
  }
  async function loadConnections() {
    const previous = el('dbConnection').value;
    const data = await request('databases');
    connections = data.connections || []; dependencies = data.dependencies || {}; csrf = data.csrf_token || '';
    allowed = !!(data.secure || data.http_enabled);
    options(el('dbConnection'), connections.map(item => ({value: item.id, label: item.name})), '选择连接', previous);
    el('dbHttpNote').hidden = allowed;
    const missing = ['mysql', 'postgres'].filter(engine => !dependencies[engine]?.available);
    el('dbDependencyNote').hidden = !missing.length && dependencies.sql;
    el('dbDependencyNote').querySelector('span').textContent = missing.length ?
      `可选组件尚未就绪：${missing.map(e => e === 'mysql' ? 'MySQL / MariaDB' : 'PostgreSQL').join('、')}。` : '只读 SQL 解析组件尚未安装，库表浏览仍可使用。';
    initialized = true; updateControls();
  }
  async function refresh() {
    if (busy) return;
    feedback('正在读取连接配置…');
    await loadConnections(); feedback();
    if (workspaceMode === 'graph') { await graph(); return; }
    if (connection() && selected) { if (currentTab === 'structure') await structure(); else if (currentTab === 'data') await browse(); }
  }
  function edit(profile = null) {
    editing = profile?.id ? profile : null;
    el('dbConnectionForm').reset();
    const data = profile || {engine: 'postgres', host: '127.0.0.1', port: 5432, tls: true};
    const names = {name: 'dbName', engine: 'dbEngine', host: 'dbHost', port: 'dbPort', user: 'dbUser', database: 'dbDefaultDatabase', ca_pem: 'dbCa'};
    Object.entries(names).forEach(([key, id]) => { el(id).value = data[key] ?? ''; });
    el('dbTls').checked = data.tls !== false;
    el('dbDialogTitle').textContent = editing ? '编辑数据库连接' : '新建数据库连接';
    el('dbPassword').placeholder = editing ? '留空沿用已保存密码' : '填写数据库账号密码';
    el('dbConnectionFeedback').textContent = '';
    el('dbConnectionDialog').showModal(); updateControls();
  }
  function draft() {
    return {id: editing?.id, revision: editing?.revision, name: el('dbName').value.trim(),
      engine: el('dbEngine').value, host: el('dbHost').value.trim(), port: Number(el('dbPort').value),
      user: el('dbUser').value, password: el('dbPassword').value, database: el('dbDefaultDatabase').value,
      tls: el('dbTls').checked, ca_pem: el('dbCa').value.trim()};
  }
  function exportResult(result, filename) {
    if (!result) return;
    const quoted = value => {
      let str = value === null ? '' : String(value);
      if (typeof value === 'string' && /^[\s]*[=+\-@]/.test(str)) str = "'" + str;
      return '"' + str.replaceAll('"', '""') + '"';
    };
    const csv = '\ufeff' + [result.columns, ...result.rows].map(row => row.map(quoted).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = filename.replace(/[^\p{L}\p{N}_.-]/gu, '_') + '.csv';
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  el('dbNew').addEventListener('click', () => edit());
  el('dbEdit').addEventListener('click', () => edit(connection()));
  el('dbDialogClose').addEventListener('click', () => el('dbConnectionDialog').close());
  el('dbConnectionDialog').addEventListener('close', () => { el('dbPassword').value = ''; });
  el('dbCellClose').addEventListener('click', () => el('dbCellDialog').close());
  el('dbTest').addEventListener('click', () => safely(async () => {
    if (!el('dbConnectionForm').reportValidity()) return;
    try { const result = await run('test', draft(), true); el('dbConnectionFeedback').textContent = result.detail; }
    catch (error) { el('dbConnectionFeedback').textContent = error.message; throw error; }
  }));
  el('dbConnectionForm').addEventListener('submit', event => {
    event.preventDefault(); safely(async () => {
      try {
        busy = true; updateControls();
        const result = await request('databases', {action: 'save', ...draft()});
        el('dbConnectionDialog').close(); await loadConnections();
        el('dbConnection').value = result.connection.id;
        options(el('dbDatabase'), result.connection.database ? [result.connection.database] : [], '选择数据库', result.connection.database);
        clearSelection(); resetGraph(); tables = []; tree(); el('dbConnectionState').textContent = '尚未连接';
        feedback('连接已保存，点击连接读取库表。');
      } catch (error) { el('dbConnectionFeedback').textContent = error.message; throw error; }
      finally { busy = false; updateControls(); }
    });
  });
  el('dbEngine').addEventListener('change', () => { el('dbPort').value = el('dbEngine').value === 'postgres' ? 5432 : 3306; });
  el('dbConnection').addEventListener('change', () => {
    clearSelection(); resetGraph(); tables = []; tree();
    options(el('dbGraphSchema'), [], '全部 Schema');
    const db = connection()?.database || '';
    options(el('dbDatabase'), db ? [db] : [], '选择数据库', db);
    el('dbConnectionState').textContent = '尚未连接'; updateControls();
  });
  el('dbConnect').addEventListener('click', () => safely(catalog));
  el('dbCatalogRefresh').addEventListener('click', () => safely(catalog));
  el('dbDatabase').addEventListener('change', () => safely(catalog));
  el('dbTableSearch').addEventListener('input', tree);
  el('dbFilterOperator').addEventListener('change', updateControls);
  el('dbFilterForm').addEventListener('submit', event => { event.preventDefault(); page = 0; safely(browse); });
  el('dbDataRefresh').addEventListener('click', () => safely(browse));
  el('dbPrevious').addEventListener('click', () => { page--; safely(browse); });
  el('dbNext').addEventListener('click', () => { page++; safely(browse); });
  root.querySelectorAll('[data-db-tab]').forEach(button => button.addEventListener('click', () => safely(async () => {
    currentTab = button.dataset.dbTab;
    root.querySelectorAll('[data-db-tab]').forEach(tab => tab.setAttribute('aria-selected', String(tab === button)));
    ['data', 'structure', 'sql'].forEach(name => { el(`db${name[0].toUpperCase() + name.slice(1)}Pane`).hidden = currentTab !== name; });
    if (currentTab === 'structure' && !structureLoaded) await structure();
    else if (currentTab === 'data' && !tableLoaded) await browse();
  })));
  el('dbSql').addEventListener('input', updateControls);
  el('dbRun').addEventListener('click', () => safely(async () => {
    sqlResult = null; updateControls(); emptyGrid('dbSqlGrid', '正在执行只读查询…');
    el('dbSqlSummary').textContent = '正在查询…';
    try {
      sqlResult = await run('query', {sql: el('dbSql').value}); grid('dbSqlGrid', sqlResult);
      el('dbSqlSummary').textContent = `${sqlResult.rows.length} 条 · ${sqlResult.elapsed_ms} ms${sqlResult.truncated ? ' · 结果已截断' : ''}`;
    } catch (error) { el('dbSqlSummary').textContent = error.message; emptyGrid('dbSqlGrid', '没有取得查询结果'); throw error; }
    finally { updateControls(); }
  }));
  ['dbCancel', 'dbGraphCancel'].forEach(id => el(id).addEventListener('click', () => safely(async () => {
    if (!jobId) return;
    await request('databases/cancel', {job: jobId}); feedback('正在取消查询…');
  })));
  el('dbDataExport').addEventListener('click', () => exportResult(dataResult, `${selected?.name || 'table'}-page-${page + 1}`));
  el('dbSqlExport').addEventListener('click', () => exportResult(sqlResult, 'query-result'));
  el('dbRemove').addEventListener('click', () => safely(async () => {
    const profile = connection();
    if (!profile || !confirm(`移除连接“${profile.name}”？仅移除面板保存的连接，不修改数据库。`)) return;
    await request('databases', {action: 'remove', id: profile.id, revision: profile.revision});
    await loadConnections(); clearSelection(); resetGraph(); tables = []; tree();
    options(el('dbDatabase'), [], '选择数据库'); el('dbConnectionState').textContent = '尚未连接'; updateControls();
  }));
  el('dbDiscover').addEventListener('click', () => safely(async () => {
    busy = true; updateControls(); feedback('正在发现本机数据库…');
    try {
      const result = await request('databases/discover', {}); el('dbDiscovery').hidden = false;
      el('dbDiscoveryNote').textContent = result.detail;
      el('dbCandidates').replaceChildren();
      (result.candidates || []).forEach(candidate => {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'ghost-button compact-action';
        button.textContent = `${candidate.name} · ${candidate.host}:${candidate.port}`;
        button.addEventListener('click', () => edit(candidate)); el('dbCandidates').append(button);
      }); feedback();
    } finally { busy = false; updateControls(); }
  }));
  el('dbDiscoveryClose').addEventListener('click', () => { el('dbDiscovery').hidden = true; });
  root.querySelectorAll('[data-db-mode]').forEach(button => button.addEventListener('click', () => safely(async () => {
    showMode(button.dataset.dbMode);
    if (workspaceMode === 'graph' && !graphLoaded) await graph();
  })));
  el('dbGraphReload').addEventListener('click', () => safely(graph));
  el('dbGraphSchema').addEventListener('change', () => safely(graph));
  el('dbHttpConsent').addEventListener('change', () => { el('dbAllowHttp').disabled = !el('dbHttpConsent').checked; });
  el('dbAllowHttp').addEventListener('click', () => safely(async () => {
    await request('databases/allow-http', {consent: el('dbHttpConsent').checked});
    await loadConnections(); feedback('已允许本次登录通过 HTTP 访问数据库。');
  }));
  // Tabs are initialized by app.js after all scripts have loaded.
  window.Databases = {refresh: () => safely(refresh), viewChanged(view) {
    if (view === 'databases' && !initialized) safely(refresh);
    else if (view === 'databases' && workspaceMode === 'graph') requestAnimationFrame(() => window.DatabaseGraph?.resize());
    if (view !== 'databases') window.DatabaseGraph?.close();
  }};
  updateControls();
})();
