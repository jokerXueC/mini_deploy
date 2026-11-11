/* Database diagrams use bundled Cytoscape + Dagre; no metadata leaves the panel. */
(() => {
  const el = id => document.getElementById(id);
  if (!el('dbGraphPane')) return;
  const WIDTH = 264, HEADER = 54, ROW = 21, SHOWN = 10;
  let cy = null, model = null, library = null, busy = false, exporting = false, openTable = null, selectedId = '';
  const cards = new Map(), tables = new Map(), edges = new Map();
  const pane = el('dbGraphPane'), modal = el('dbGraphModal'), stage = el('dbGraphStage');
  const create = (tag, className, text) => {
    const node = document.createElement(tag); node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function script(src) {
    return new Promise((resolve, reject) => {
      const node = document.createElement('script');
      const timer = setTimeout(() => { node.remove(); reject(Error('关系图组件加载超时，请刷新重试。')); }, 15000);
      node.src = src;
      node.onload = () => { clearTimeout(timer); resolve(); };
      node.onerror = () => { clearTimeout(timer); node.remove(); reject(Error('关系图组件加载失败，请刷新重试。')); };
      document.head.append(node);
    });
  }
  const cancel = create('button', 'toolbar-icon'); cancel.type = 'button'; cancel.id = 'dbGraphCancel'; cancel.hidden = true;
  cancel.title = '取消关系图读取'; cancel.setAttribute('aria-label', cancel.title);
  const cancelIcon = create('img', ''); cancelIcon.src = 'ui/previews/ssh-ai/icons/pause.svg'; cancelIcon.alt = '';
  cancel.append(cancelIcon); pane.querySelector('.db-graph-actions').prepend(cancel);
  function ensure() {
    if (!library) library = Promise.all([
      window.cytoscape ? Promise.resolve() : script('ui/vendor/cytoscape.min.js'),
      window.dagre ? Promise.resolve() : script('ui/vendor/dagre.min.js'),
    ]).then(() => script('ui/vendor/cytoscape-dagre.js')).catch(error => { library = null; throw error; });
    return library;
  }
  const height = item => HEADER + Math.min(SHOWN, item.columns.length) * ROW + (item.columns.length > SHOWN ? 25 : 10);
  function card(item) {
    const node = create('div', 'db-graph-card'); node.dataset.graphTable = item.id;
    node.style.width = WIDTH + 'px'; node.style.height = height(item) + 'px';
    const head = create('header', 'db-graph-card-head');
    head.append(create('strong', '', item.name), create('span', '', item.schema + (item.type === 'VIEW' ? ' / 视图' : '')));
    node.append(head);
    item.columns.slice(0, SHOWN).forEach(field => {
      const row = create('div', 'db-graph-field');
      row.append(create('span', 'db-graph-key', field.primary_key ? 'PK' : field.foreign_key ? 'FK' : ''),
        create('span', 'db-graph-column', field.name), create('span', 'db-graph-type', field.type));
      if (field.primary_key) row.dataset.key = 'primary';
      else if (field.foreign_key) row.dataset.key = 'foreign';
      node.append(row);
    });
    if (item.columns.length > SHOWN) node.append(create('div', 'db-graph-more', `另有 ${item.columns.length - SHOWN} 个字段`));
    cards.set(item.id, node); el('dbGraphCards').append(node);
  }
  function paint() {
    if (!cy) return;
    const pan = cy.pan(), zoom = cy.zoom();
    el('dbGraphCards').style.transform = `translate(${pan.x}px,${pan.y}px) scale(${zoom})`;
    cy.nodes().forEach(node => {
      const item = cards.get(node.id()); if (!item) return;
      const pos = node.position();
      item.style.left = (pos.x - WIDTH / 2) + 'px'; item.style.top = (pos.y - node.data('height') / 2) + 'px';
      item.hidden = node.hidden();
      item.dataset.selected = String(node.hasClass('focused'));
      item.dataset.dim = String(node.hasClass('dim'));
    });
    el('dbGraphScale').textContent = Math.round(zoom * 100) + '%';
  }
  function colors() {
    const css = getComputedStyle(pane), color = name => css.getPropertyValue(name).trim();
    return {line: color('--muted'), accent: color('--control-accent'), bg: color('--bg'), fg: color('--fg')};
  }
  function theme() {
    if (!cy) return;
    const color = colors();
    cy.style().selector('edge').style({'line-color': color.line, 'target-arrow-color': color.line,
      color: color.fg, 'text-background-color': color.bg}).selector('edge.focused').style({
      'line-color': color.accent, 'target-arrow-color': color.accent}).update();
  }
  function fit(nodes = null) {
    if (!cy) return;
    const visible = nodes || cy.elements(':visible');
    if (visible.length) { cy.fit(visible, 44); if (cy.zoom() > 1) cy.zoom({level: 1, renderedPosition: {x: stage.clientWidth / 2, y: stage.clientHeight / 2}}); }
    paint();
  }
  function resize(refit = false) {
    if (!cy || !stage.clientWidth || !stage.clientHeight) return;
    cy.resize(); if (refit) fit(); else paint();
  }
  function detailButton(label, action) {
    const button = create('button', 'ghost-button compact-action', label); button.type = 'button'; button.disabled = busy;
    button.addEventListener('click', action); return button;
  }
  function hideDetails() {
    el('dbGraphDetails').hidden = true; el('dbGraphBoard').classList.remove('has-details');
    requestAnimationFrame(() => resize());
  }
  function showDetails(title) {
    const aside = el('dbGraphDetails'); aside.replaceChildren(); aside.hidden = false;
    el('dbGraphBoard').classList.add('has-details');
    const header = create('header', 'db-graph-detail-head'); header.append(create('h3', '', title));
    const close = create('button', 'toolbar-icon'); close.type = 'button'; close.title = '关闭详情'; close.setAttribute('aria-label', close.title);
    const icon = create('img', ''); icon.src = 'ui/previews/ssh-ai/icons/x.svg'; icon.alt = ''; close.append(icon);
    close.addEventListener('click', hideDetails); header.append(close); aside.append(header);
    requestAnimationFrame(() => resize()); return aside;
  }
  function focus(id, center = true) {
    if (!cy || busy) return;
    const item = tables.get(id), node = cy.getElementById(id); if (!item || !node.visible()) return;
    selectedId = id;
    cy.elements().removeClass('focused dim');
    const neighborhood = node.closedNeighborhood(); cy.elements().difference(neighborhood).addClass('dim');
    neighborhood.edges().addClass('focused'); node.addClass('focused');
    el('dbGraphTable').value = id; window.AppSelects?.syncAll();
    const aside = showDetails(item.name); aside.append(create('p', 'muted', item.schema + ' / ' + item.type));
    const actions = create('div', 'db-graph-detail-actions');
    ['data', 'structure'].forEach((tab, index) => actions.append(detailButton(index ? '查看结构' : '查看数据', () => {
      closeModal(); openTable?.(item, tab);
    })));
    aside.append(actions);
    const list = create('dl', 'db-graph-field-list');
    item.columns.forEach(field => {
      const term = create('dt', '', (field.primary_key ? 'PK ' : field.foreign_key ? 'FK ' : '') + field.name);
      list.append(term, create('dd', '', field.type + (field.nullable ? ' / 可空' : ' / 非空')));
    });
    aside.append(list);
    const relations = model.relationships.filter(edge => edge.source === id || edge.target === id);
    aside.append(create('h4', '', `关联外键 (${relations.length})`));
    relations.forEach(edge => aside.append(detailButton(edge.name, () => edgeDetails(edge.id))));
    if (!relations.length) aside.append(create('p', 'muted', '没有已声明的外键关联'));
    if (center) requestAnimationFrame(() => { resize(); fit(neighborhood.filter(':visible')); });
    paint();
  }
  function edgeDetails(id) {
    const edge = edges.get(id); if (!edge || busy) return;
    const source = tables.get(edge.source), target = tables.get(edge.target);
    const aside = showDetails(edge.name);
    aside.append(create('p', 'muted', '已声明外键'));
    const list = create('dl', 'db-graph-relation-list');
    edge.columns.forEach((name, index) => list.append(create('dt', '', `${source.schema}.${source.name}.${name}`),
      create('dd', '', `→ ${target.schema}.${target.name}.${edge.target_columns[index]}`)));
    aside.append(list, create('p', 'muted', `更新：${edge.update_rule} / 删除：${edge.delete_rule}`));
    aside.append(detailButton(source.name, () => focus(source.id)), detailButton(target.name, () => focus(target.id)));
    cy.elements().removeClass('focused dim');
    const related = cy.getElementById(id).connectedNodes().union(cy.getElementById(id));
    related.addClass('focused'); cy.elements().difference(related).addClass('dim'); paint();
  }
  function filter() {
    if (!cy) return;
    cy.elements().removeClass('dim focused'); selectedId = ''; hideDetails();
    const connected = el('dbGraphConnected').checked;
    cy.nodes().forEach(node => node.style('display', !connected || node.degree() ? 'element' : 'none'));
    cy.edges().forEach(edge => edge.style('display', edge.source().visible() && edge.target().visible() ? 'element' : 'none'));
    const query = el('dbGraphSearch').value.trim().toLowerCase();
    const matches = cy.nodes(':visible').filter(node => {
      const table = tables.get(node.id()); return `${table.schema}.${table.name}`.toLowerCase().includes(query);
    });
    if (query) {
      const related = matches.closedNeighborhood(); cy.elements().difference(related).addClass('dim'); matches.addClass('focused');
      el('dbGraphCount').textContent = `匹配 ${matches.length} 张表`;
    } else el('dbGraphCount').textContent = `${cy.nodes(':visible').length} 张表 / ${cy.edges(':visible').length} 条外键`;
    el('dbGraphEmpty').hidden = !!cy.nodes(':visible').length;
    el('dbGraphEmpty').textContent = connected ? '当前范围没有已声明外键的表' : '当前账号在此范围内没有可查看的表';
    paint(); return matches;
  }
  async function render(data, onOpen) {
    await ensure(); reset(); model = data; openTable = onOpen;
    const elements = [];
    data.tables.forEach(item => { tables.set(item.id, item); card(item); elements.push({data: {id: item.id, height: height(item)}}); });
    data.relationships.forEach(edge => { edges.set(edge.id, edge); elements.push({classes: edge.source === edge.target ? 'loop' : '', data: {id: edge.id, source: edge.source, target: edge.target,
      label: edge.columns.join(', ') + ' → ' + edge.target_columns.join(', ')}}); });
    cy = window.cytoscape({container: el('dbGraphCanvas'), elements, minZoom: .06, maxZoom: 2.5, wheelSensitivity: .22,
      boxSelectionEnabled: false, selectionType: 'single', pixelRatio: Math.min(devicePixelRatio || 1, 2),
      style: [
        {selector: 'node', style: {width: WIDTH, height: 'data(height)', shape: 'round-rectangle', 'background-opacity': 0, 'border-width': 0}},
        {selector: 'edge', style: {width: 1.5, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': .8, 'font-family': getComputedStyle(pane).fontFamily,
          'line-opacity': .65, 'target-arrow-fill': 'filled', label: '', 'font-size': 11, 'text-rotation': 'autorotate',
          'text-background-opacity': 1, 'text-background-padding': 4, 'text-wrap': 'ellipsis', 'text-max-width': 220}},
        {selector: 'edge.focused', style: {width: 2.5, 'line-opacity': 1, label: 'data(label)'}},
        {selector: 'edge.loop', style: {'control-point-step-size': 220, 'loop-direction': '-45deg', 'loop-sweep': '75deg'}},
        {selector: '.dim', style: {opacity: .14}},
      ],
      layout: {name: 'dagre', rankDir: stage.clientWidth < 600 ? 'TB' : 'LR', rankSep: 150, nodeSep: 50, edgeSep: 35, padding: 44, animate: false, fit: false},
    });
    cy.on('pan zoom position', paint);
    cy.on('tap', 'node', event => focus(event.target.id(), false));
    cy.on('tap', 'edge', event => edgeDetails(event.target.id()));
    cy.on('tap', event => { if (event.target === cy) { selectedId = ''; cy.elements().removeClass('focused dim'); hideDetails(); paint(); } });
    const select = el('dbGraphTable'); select.replaceChildren(new Option('定位表…', ''));
    data.tables.forEach(item => select.add(new Option(`${item.schema}.${item.name}`, item.id)));
    el('dbGraphNotice').textContent = data.notice || '';
    stage.setAttribute('aria-label', `${data.database}：${data.tables.length} 张表，${data.relationships.length} 条已声明外键`);
    theme(); filter(); resize(true); setBusy(false);
  }
  function reset() {
    if (cy) cy.destroy(); cy = null; model = null; selectedId = '';
    cards.clear(); tables.clear(); edges.clear(); el('dbGraphCards').replaceChildren(); hideDetails();
    el('dbGraphSearch').value = ''; el('dbGraphConnected').checked = false;
    el('dbGraphTable').replaceChildren(new Option('定位表…', ''));
    el('dbGraphCount').textContent = ''; el('dbGraphNotice').textContent = '';
    el('dbGraphEmpty').hidden = false; el('dbGraphEmpty').textContent = '选择连接与数据库后查看关系图';
    setBusy(false);
  }
  function setBusy(value, ready = true) {
    busy = value;
    ['dbGraphZoomIn', 'dbGraphZoomOut', 'dbGraphFit', 'dbGraphExport', 'dbGraphTable', 'dbGraphSearch', 'dbGraphConnected'].forEach(id => { el(id).disabled = busy || !cy; });
    el('dbGraphExport').disabled ||= exporting;
    ['dbGraphSchema', 'dbGraphReload'].forEach(id => { el(id).disabled = busy || !ready; });
    el('dbGraphExpand').disabled = busy;
    el('dbGraphDetails').querySelectorAll('button').forEach(button => { button.disabled = busy; });
    stage.setAttribute('aria-busy', String(busy)); window.AppSelects?.syncAll();
  }
  function closeModal() { if (modal.open) modal.close(); }
  el('dbGraphExpand').addEventListener('click', () => {
    if (modal.open) { closeModal(); return; }
    modal.append(pane); modal.showModal();
    el('dbGraphExpand').title = '还原关系图'; el('dbGraphExpand').setAttribute('aria-label', '还原关系图');
    requestAnimationFrame(() => resize(true));
  });
  el('dbGraphClose').addEventListener('click', closeModal);
  modal.addEventListener('close', () => {
    el('dbGraphMount').append(pane); el('dbGraphExpand').title = '展开关系图'; el('dbGraphExpand').setAttribute('aria-label', '展开关系图');
    requestAnimationFrame(() => resize(true));
  });
  el('dbGraphFit').addEventListener('click', () => fit());
  ['dbGraphZoomIn', 'dbGraphZoomOut'].forEach((id, index) => el(id).addEventListener('click', () => {
    if (cy) cy.zoom({level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * (index ? 1 / 1.25 : 1.25))),
      renderedPosition: {x: stage.clientWidth / 2, y: stage.clientHeight / 2}});
  }));
  el('dbGraphTable').addEventListener('change', () => focus(el('dbGraphTable').value));
  el('dbGraphConnected').addEventListener('change', () => { filter(); fit(); });
  el('dbGraphSearch').addEventListener('input', filter);
  el('dbGraphSearch').addEventListener('keydown', event => {
    if (event.key === 'Enter') { const matches = filter(); if (matches?.length) focus(matches.first().id()); }
  });
  el('dbGraphExport').addEventListener('click', async () => {
    if (!cy || !model || exporting) return;
    const graph = cy, exportTables = new Map(tables);
    exporting = true; el('dbGraphExport').disabled = true;
    // Export the HTML table overlays with the graph, using Cytoscape's text labels for the image.
    const color = colors();
    graph.style().selector('node').style({label: node => {
      const item = exportTables.get(node.id()); return `${item.schema}.${item.name}\n\n` + item.columns.slice(0, SHOWN).map(field =>
        `${field.primary_key ? 'PK' : field.foreign_key ? 'FK' : '  '} ${field.name}  ${field.type}`).join('\n');
    }, 'background-opacity': 1, 'background-color': color.bg, 'border-width': 1, 'border-color': color.line,
    color: color.fg, 'font-size': 11, 'text-wrap': 'wrap', 'text-max-width': WIDTH - 20, 'text-valign': 'center', 'line-height': 1.6}).update();
    try {
      // The renderer recalculates text metrics on its next frame before image export.
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (graph !== cy || graph.destroyed()) return;
      const blob = graph.png({output: 'blob', full: true, bg: color.bg, scale: 1.5, maxWidth: 3500, maxHeight: 3500});
      const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url;
      link.download = 'database-relations.png'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      if (graph === cy) el('dbGraphNotice').textContent = '图片导出失败，请重试。';
    } finally {
      if (!graph.destroyed()) graph.style().selector('node').style({label: '', 'background-opacity': 0, 'border-width': 0}).update();
      exporting = false; el('dbGraphExport').disabled = busy || !cy;
    }
  });
  new ResizeObserver(() => resize()).observe(stage);
  new MutationObserver(theme).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  window.DatabaseGraph = {ensure, render, reset, setBusy, resize, close: closeModal, selected: () => selectedId};
})();
