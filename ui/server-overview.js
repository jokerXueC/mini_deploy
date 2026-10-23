/* Persisted resource samples and historical evidence share one time axis. */
window.ServerOverview = (() => {
  const columns = 60;
  const rowColumns = columns;
  const metrics = [
    {key: 'cpu_percent', name: 'CPU', unit: '%', threshold: 80},
    {key: 'memory_percent', name: '内存', unit: '%', threshold: 90},
    {key: 'network_total_kbps', name: '网络', unit: ' KiB/s'},
  ];
  const $ = id => document.getElementById(id);
  const grid = $('loadMap');
  if (!grid) return;
  const cells = [];
  let descriptions = [], selected = -1, selectedMetric = 'cpu_percent';
  let latestPoints = [], latestOptions = {}, target = '', targetName = '';
  let requestKey = '', request = null, history = null, nextRead = 0, historyError = '';
  let buckets = [], windowStart = 0, bucketSpan = 1, pinned = null, detailRequest = null;
  let scenes = [], sceneRequest = null;
  let derived = null;
  let revision = 0;
  const redraw = () => document.dispatchEvent(new Event('server-observation-change'));
  const isVisible = () => $('serverView').classList.contains('active') && !document.hidden
    && !document.querySelector('#serverPerformanceDialog[open]');
  function pause() {
    request?.abort();
    request = null;
    nextRead = 0;
    if (detailRequest) $('observationEvents').textContent = '读取已暂停，重新选择时段可重试。';
    if (sceneRequest) $('observationProcesses').textContent = '读取已暂停，重新选择现场可重试。';
    detailRequest?.abort(); sceneRequest?.abort();
    detailRequest = null; sceneRequest = null;
  }
  function reset() {
    pause(); history = null; derived = null; historyError = ''; requestKey = ''; selected = -1;
    closeEvidence();
  }
  $('observationTarget').addEventListener('change', event => {
    target = event.target.value;
    targetName = event.target.selectedOptions[0]?.textContent || target;
    reset(); redraw();
  });
  document.querySelectorAll('[data-observation-metric]').forEach(button => {
    button.addEventListener('click', () => {
      selectedMetric = button.dataset.observationMetric;
      update(latestPoints, latestOptions);
    });
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); else redraw(); });

  async function readHistory(key, period) {
    const controller = new AbortController();
    request = controller;
    // Bound the read even if the connection remains open without a response.
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(target
        ? `performance/containers?${new URLSearchParams({id: target, period})}`
        : `performance/server?${new URLSearchParams({period, live: '1'})}`, {
        credentials: 'same-origin', signal: controller.signal,
      });
      if (response.status === 401) { location.reload(); return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '历史暂时无法读取');
      if (!Array.isArray(data.points)) throw new Error('历史返回格式无效');
      if (request !== controller || requestKey !== key) return;
      history = data; derived = null; historyError = '';
    } catch (error) {
      if (request !== controller || requestKey !== key) return;
      historyError = error.name === 'AbortError' ? '读取历史超时' : error.message;
      derived = null;
    } finally {
      clearTimeout(timeout);
      if (request === controller && requestKey === key) {
        request = null; nextRead = Date.now() + 30000;
        if (isVisible()) redraw();
      }
    }
  }

  function source(system, range) {
    const select = $('observationTarget');
    const containers = system.docker?.containers || [];
    const options = [['', '服务器整体'], ...containers.filter(item => /^[a-f0-9]{64}$/.test(item.id))
      .map(item => [item.id, `Docker · ${item.name || item.id.slice(0, 12)}`])];
    if (target && !options.some(([id]) => id === target)) options.push([target, `${targetName}（已离开当前列表）`]);
    const signature = JSON.stringify(options);
    if (select.dataset.options !== signature) {
      select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
      select.value = target; select.dataset.options = signature;
    }
    document.querySelector('[data-trend-range="realtime"]').textContent = target ? '近 30 分钟' : '实时';
    document.querySelector('[data-trend-range="all"]').textContent = target ? '已保留' : '全部';
    if (!target && range === 'realtime') {
      if (requestKey) reset();
      $('observationStatus').dataset.error = 'false';
      $('observationStatus').textContent = system.docker?.available === false ? 'Docker 暂不可用 · 当前查看主机' : '主机 CPU · 内存 · 网络吞吐';
      return null;
    }
    const period = range === 'realtime' ? '1h' : range === '6h' ? '24h' : range === 'all' ? '7d' : range;
    const key = `${target}:${range}`;
    if (requestKey !== key) { reset(); requestKey = key; }
    if (!request && Date.now() >= nextRead && isVisible()) readHistory(key, period);
    $('observationStatus').textContent = historyError ? `${historyError} · ${history ? '保留上次结果，' : ''}稍后重试`
      : history?.error || (history?.stale ? '历史采样已中断，最近超过 65 秒没有新记录'
      : request && !history ? '正在读取已保存的采样…' : target ? '容器 CPU · 内存；网络吞吐请查看服务器整体'
      : '已保存的历史采样 · 点击时段查看现场');
    $('observationStatus').dataset.error = String(Boolean(historyError || history?.error || history?.stale));
    if (derived) return derived;
    const interval = Math.max(30, Number(history?.interval_seconds) || 30);
    const now = Number.isFinite(Date.parse(history?.until)) ? Date.parse(history.until) / 1000 : Date.now() / 1000;
    // Keep saved sampling intervals wholly within a tile, including its process scenes.
    let windowEnd = Math.ceil(now / interval) * interval;
    const windowSeconds = range === 'realtime' ? 1800 : range === '1h' ? 3600 : range === '6h' ? 21600 : period === '7d' ? 604800 : 86400;
    const points = (history?.points || []).filter(point => Number.isFinite(point.ts) && point.ts >= now - windowSeconds && point.ts <= now)
      .map(point => {
        if (target) return point;
        const rx = number(point.network_rx_kib_s), tx = number(point.network_tx_kib_s);
        return {...point, network_rx_kbps: rx, network_tx_kbps: tx,
          network_total_kbps: rx == null || tx == null ? null : rx + tx};
      })
      .slice().sort((a, b) => a.ts - b.ts);
    // Tile ends are exclusive; include a sample exactly on the current boundary.
    if (points.at(-1)?.ts === windowEnd) windowEnd += interval;
    // Keep gaps visible in the shared chart instead of connecting unrelated samples.
    const filled = [];
    points.forEach(point => {
      const previous = filled.at(-1);
      if (previous) {
        for (let ts = previous.ts + interval; ts < point.ts - interval / 2; ts += interval) filled.push({ts});
      }
      filled.push(point);
    });
    const rangeLabel = range === 'realtime' ? '最近 30 分钟' : range === '1h' ? '最近 1 小时'
      : range === '6h' ? '最近 6 小时' : range === '24h' ? '最近 24 小时' : '最近 7 天';
    derived = {revision: ++revision, points: filled, intervalSeconds: interval, container: Boolean(target), target,
      windowStart: windowEnd - windowSeconds, windowEnd, rangeLabel,
      emptyMessage: historyError ? '历史暂时无法读取，将自动重试' : history?.error ? '历史采样保存失败，请查看上方原因'
        : history?.stale || !points.length && history?.last_saved_at ? '所选时段没有新的已保存采样' : '',
      contexts: history?.contexts || [],
      hint: `${select.selectedOptions[0]?.textContent} · ${rangeLabel} · 每 30 秒采集 · ${points.length} 个时间点${target ? ' · 区间均值' : ''}${points.length ? ` · 最新 ${clock(points.at(-1).ts)}` : ''}`};
    return derived;
  }
  const number = value => value == null || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value))
    ? null : Math.max(0, Number(value));
  const format = (value, unit = '%') => value == null ? '—' : unit === ' KiB/s' && value >= 1024
    ? `${(value / 1024).toFixed(1)} MiB/s` : `${value.toFixed(1)}${unit}`;
  const clock = (timestamp, date = false) => new Date(timestamp * 1000).toLocaleString('zh-CN', {
    ...(date ? {month: '2-digit', day: '2-digit'} : {}),
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  function stats(points, metric, container = latestOptions.container) {
    const readings = points.filter(point => number(point[metric.key]) != null);
    const weight = point => {
      if (!point.weights) return number(point.samples) || 1;
      if (metric.key === 'network_total_kbps') return Math.min(
        number(point.weights.network_rx_kib_s) || 0, number(point.weights.network_tx_kib_s) || 0);
      return number(point.weights[metric.key]) || 0;
    };
    const total = readings.reduce((sum, point) => sum + weight(point), 0);
    const average = total ? readings.reduce((sum, point) => sum + point[metric.key] * weight(point), 0) / total : null;
    const peaks = readings.map(point => number(point[metric.key + '_peak']) ?? number(point[metric.key]));
    return {average, peak: peaks.length ? Math.max(...peaks) : null, count: readings.length,
      high: readings.filter(point => (number(point[metric.key + '_peak']) ?? point[metric.key]) >= metric.threshold).length,
      peakLabel: container || metric.key === 'network_total_kbps' ? '区间均值峰值' : '峰值'};
  }
  function closeEvidence() {
    pinned = null; scenes = [];
    detailRequest?.abort(); sceneRequest?.abort();
    detailRequest = null; sceneRequest = null;
    $('observationEvidence').hidden = true;
    cells.forEach(cell => { delete cell.dataset.pinned; cell.setAttribute('aria-pressed', 'false'); });
  }
  $('observationEvidenceClose').addEventListener('click', closeEvidence);
  async function getJSON(url, signal) {
    const response = await fetch(url, {credentials: 'same-origin', signal});
    if (response.status === 401) { location.reload(); throw new Error('登录已失效'); }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '读取失败');
    return data;
  }
  function highlight() {
    cells.forEach((cell, index) => {
      const start = windowStart + index % columns * bucketSpan;
      const active = Boolean(pinned && start < pinned.until && start + bucketSpan > pinned.since);
      cell.dataset.pinned = String(active); cell.setAttribute('aria-pressed', String(active));
    });
  }
  async function loadScene() {
    sceneRequest?.abort(); sceneRequest = null;
    const scene = scenes[Number($('observationScene').value)];
    if (!scene || !pinned) return;
    const controller = new AbortController(); sceneRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    $('observationProcesses').textContent = '正在读取历史现场…';
    try {
      const data = await getJSON(`performance/server/context?${new URLSearchParams({run: scene.run, ts: scene.ts})}`, controller.signal);
      if (sceneRequest !== controller) return;
      if (!(data.at >= pinned.since && data.at < pinned.until)) throw new Error('现场不在所选时段内');
      const rank = pinned.metric === 'memory_percent' ? 'rss_mib' : 'host_cpu_percent';
      const processes = (data.processes || []).filter(process => !target || process.container_id === target)
        .slice().sort((a, b) => (number(b[rank]) || 0) - (number(a[rank]) || 0));
      $('observationProcesses').innerHTML = `<p class="evidence-caption">${escape(clock(data.at, true))} · ${processes.length} 个已保存进程 · 按${rank === 'rss_mib' ? '驻留内存' : '主机 CPU'}排序${data.available === false ? ' · 采集不完整' : ''}</p>`
        + (processes.length ? `<div class="evidence-table-scroll"><table><thead><tr><th>进程 / PID</th><th>主机 CPU</th><th>驻留内存</th><th>写入</th></tr></thead><tbody>${processes.slice(0, 8).map(process =>
          `<tr><td>${escape(process.name || '未知')} <small>${escape(process.pid)}</small>${process.container || process.service ? `<small class="evidence-process-source">${escape(process.container || process.service)}</small>` : ''}</td><td>${format(number(process.host_cpu_percent))}</td><td>${format(number(process.rss_mib), ' MiB')}</td><td>${format(number(process.write_kib_s), ' KiB/s')}</td></tr>`).join('')}</tbody></table></div>${processes.length > 8 ? '<p class="evidence-caption">展示前 8 个进程。</p>' : ''}`
          : '<p class="evidence-empty">该现场没有可展示的进程记录。</p>')
        + (data.warnings?.length ? `<p class="evidence-caption">${escape(data.warnings.join('；'))}</p>` : '');
    } catch (error) {
      if (sceneRequest === controller) $('observationProcesses').textContent = `现场暂不可读：${error.name === 'AbortError' ? '读取超时' : error.message}。可重新选择时段重试。`;
    } finally {
      clearTimeout(timeout);
      if (sceneRequest === controller) sceneRequest = null;
    }
  }
  $('observationScene').addEventListener('change', loadScene);
  $('observationLocate').addEventListener('click', () => {
    const metric = metrics.find(item => item.key === selectedMetric);
    const peaks = buckets.map(bucket => stats(bucket, metric).peak);
    const peak = Math.max(...peaks.filter(value => value != null));
    const column = peaks.indexOf(peak);
    if (column < 0) return;
    const cell = cells[metrics.indexOf(metric) * columns + column];
    cells.forEach(item => { item.tabIndex = item === cell ? 0 : -1; });
    cell.focus(); show(cell); openEvidence(column);
  });
  async function openEvidence(column) {
    detailRequest?.abort(); sceneRequest?.abort();
    detailRequest = null; sceneRequest = null;
    pinned = {since: windowStart + column * bucketSpan, until: windowStart + (column + 1) * bucketSpan, metric: selectedMetric};
    highlight();
    $('observationEvidence').hidden = false;
    $('observationEvidenceTitle').textContent = target ? targetName : '服务器整体';
    $('observationEvidenceRange').textContent = `${clock(pinned.since, true)} — ${clock(pinned.until, true)}（不含结束时刻）`;
    $('observationEvidenceMetrics').innerHTML = metrics.filter(metric => !target || metric.key !== 'network_total_kbps').map(metric => {
      const result = stats(buckets[column], metric);
      return `<div><span>${metric.name}</span><strong>${format(result.average, metric.unit)}</strong><small>${result.count ? `平均 · ${result.peakLabel} ${format(result.peak, metric.unit)} · ${result.count} 个时间点` : '无有效采样'}</small></div>`;
    }).join('');
    scenes = (latestOptions.contexts || []).filter(scene => scene.at >= pinned.since && scene.at < pinned.until)
      .slice().sort((a, b) => b.at - a.at);
    const select = $('observationScene');
    select.replaceChildren(...scenes.map((scene, index) => new Option(clock(scene.at), String(index))));
    select.hidden = scenes.length < 2;
    $('observationProcesses').textContent = target ? '容器历史仅保存资源均值，没有关联的进程现场。' : latestOptions.realtime
      ? '实时资源未关联已保存的进程现场；请切换到 1h 查看历史。'
      : '该时段没有保存进程现场，无法还原当时的进程。';
    if (scenes.length) loadScene();
    $('observationEvents').textContent = '正在读取已保留事件…';
    const controller = new AbortController(); detailRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const data = await getJSON('monitoring', controller.signal);
      if (detailRequest !== controller) return;
      const events = (data.events || []).filter(event => event.at_ts >= pinned.since && event.at_ts < pinned.until
        && (!target || event.metadata?.container_id === target));
      $('observationEvents').innerHTML = events.length ? `<ul class="evidence-events">${events.slice(0, 20).map(event =>
        `<li><time>${escape(clock(event.at_ts))}</time><strong>${escape(event.title || event.kind)}</strong><p>${escape(event.detail || '')}</p></li>`).join('')}</ul>${events.length > 20 ? '<p class="evidence-caption">最多展示 20 条。</p>' : ''}`
        : `<p class="evidence-empty">已保留事件中没有${target ? '可通过容器 ID 关联到该容器的' : '该时段的'}记录。事件可能未被采集或已过期。</p>`;
    } catch (error) {
      if (detailRequest === controller) $('observationEvents').textContent = `事件暂不可读：${error.name === 'AbortError' ? '读取超时' : error.message}。不能据此判断没有异常。`;
    } finally {
      clearTimeout(timeout);
      if (detailRequest === controller) detailRequest = null;
    }
  }
  metrics.forEach((metric, rowIndex) => {
    const row = document.createElement('div');
    row.className = 'load-map-row';
    row.dataset.metric = metric.key;
    const label = document.createElement('span');
    label.className = 'load-map-label';
    label.textContent = metric.name;
    row.append(label);
    const tiles = document.createElement('div');
    tiles.className = 'load-map-tiles';
    for (let column = 0; column < columns; column++) {
      const cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'load-cell';
      cell.tabIndex = rowIndex === 0 && column === 0 ? 0 : -1;
      cell.dataset.index = cells.length;
      cell.dataset.level = 'empty';
      cell.setAttribute('aria-label', `${metric.name}，等待采样`);
      tiles.append(cell);
      cells.push(cell);
    }
    row.append(tiles);
    grid.append(row);
  });
  const show = cell => {
    if (!cell) return;
    selected = Number(cell.dataset.index);
    $('loadMapDetail').textContent = descriptions[selected] || '等待资源采样';
  };
  grid.addEventListener('pointerover', event => show(event.target.closest('.load-cell')));
  grid.addEventListener('focusin', event => show(event.target.closest('.load-cell')));
  grid.addEventListener('click', event => {
    const cell = event.target.closest('.load-cell');
    if (!cell) return;
    cells.forEach(item => { item.tabIndex = item === cell ? 0 : -1; });
    selectedMetric = metrics[Math.floor(Number(cell.dataset.index) / columns)].key;
    update(latestPoints, latestOptions);
    show(cell);
    openEvidence(Number(cell.dataset.index) % columns);
  });
  grid.addEventListener('keydown', event => {
    const cell = event.target.closest('.load-cell');
    if (!cell) return;
    const index = Number(cell.dataset.index);
    const step = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -rowColumns, ArrowDown: rowColumns}[event.key];
    let next = step == null ? null : index + step;
    if (event.key === 'Home') next = Math.floor(index / rowColumns) * rowColumns;
    if (event.key === 'End') next = Math.floor(index / rowColumns) * rowColumns + rowColumns - 1;
    if (next == null) return;
    event.preventDefault();
    next = Math.max(0, Math.min((latestOptions.container ? columns * 2 : cells.length) - 1, next));
    cells.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
    cells[next].focus();
  });
  $('loadHistoryOpen').addEventListener('click', () => {
    if (!target) $('serverPerformanceOpen').click();
    else window.ContainerPerformance?.open(target, targetName);
  });

  function update(input, options = {}) {
    latestPoints = input; latestOptions = options;
    const {realtime = false, intervalSeconds = 1800, container = false, rangeLabel} = options;
    if (container && selectedMetric === 'network_total_kbps') selectedMetric = 'cpu_percent';
    document.querySelector('.load-summary').dataset.metric = selectedMetric;
    document.querySelectorAll('[data-observation-metric]').forEach(button => {
      button.hidden = container && button.dataset.observationMetric === 'network_total_kbps';
      button.setAttribute('aria-pressed', String(button.dataset.observationMetric === selectedMetric));
    });
    $('loadHistoryOpen').firstChild.textContent = container ? '查看容器资源历史 ' : '查看历史与进程现场 ';
    const points = input.filter(point => number(point.ts) > 0).slice().sort((a, b) => a.ts - b.ts);
    const end = options.windowEnd ?? (points.length ? Number(points[points.length - 1].ts) : Date.now() / 1000);
    const interval = realtime ? 1 : Math.max(1, Number(intervalSeconds) || 1800);
    const start = options.windowStart ?? (realtime ? end - columns + 1 : Number(points[0]?.ts ?? end - interval * (columns - 1)));
    const span = realtime ? 1 : Math.max(1, (end - start + (options.windowEnd ? 0 : interval)) / columns);
    buckets = Array.from({length: columns}, () => []);
    windowStart = start; bucketSpan = span;
    points.forEach(point => {
      const index = Math.floor((Number(point.ts) - start) / span);
      if (index >= 0 && index < columns) buckets[index].push(point);
    });
    const hasDate = end - start >= 86400;
    $('loadMapRange').textContent = rangeLabel || (realtime ? '最近 60 秒' : '所选时间范围');
    $('loadMapStart').textContent = points.length ? clock(start, hasDate) : '—';
    $('loadMapEnd').textContent = points.length ? clock(end, hasDate) : '—';
    descriptions = [];
    metrics.forEach((metric, rowIndex) => {
      const row = grid.children[rowIndex];
      row.hidden = container && rowIndex === 2;
      row.dataset.selected = String(metric.key === selectedMetric);
      const threshold = container && rowIndex === 0 ? null : metric.threshold;
      const values = points.map(point => number(point[metric.key])).filter(value => value != null);
      const maximum = threshold ? 100 : Math.max(container && rowIndex === 0 ? 100 : 1, ...values);
      buckets.forEach((bucket, column) => {
        const {average, peak, count, peakLabel} = stats(bucket, metric, container);
        const cell = cells[rowIndex * columns + column];
        cell.dataset.level = average == null ? 'empty' : threshold && peak >= threshold ? 'high' : 'normal';
        cell.style.setProperty('--load-intensity', average == null ? '0%' : `${12 + Math.min(1, average / maximum) * 88}%`);
        const from = clock(start + column * span, hasDate);
        const until = span > interval ? ` 至 ${clock(start + (column + 1) * span, hasDate)}` : '';
        const detail = `${metric.name} · ${from}${until} · ${count
          ? `${format(average, metric.unit)} 平均 · ${peakLabel} ${format(peak, metric.unit)} · ${count} 个样本`
          : '无有效采样'}`;
        descriptions.push(detail);
        cell.setAttribute('aria-label', detail);
      });
      if (metric.key === selectedMetric) {
        const result = stats(points, metric, container);
        $('loadAverageLabel').textContent = `${metric.name} 平均${rowIndex === 2 ? '吞吐' : '占用'}`;
        $('loadPeakLabel').textContent = `${metric.name} ${result.peakLabel}`;
        $('loadHighLabel').textContent = threshold ? `≥ ${threshold}% 时间点` : '有效时间点';
        $('loadAverage').textContent = format(result.average, metric.unit);
        $('loadPeak').textContent = format(result.peak, metric.unit);
        $('loadLatest').textContent = format(number(points.at(-1)?.[metric.key]), metric.unit);
        $('loadHighCount').textContent = values.length ? threshold ? `${result.high} / ${values.length}` : String(values.length) : '—';
        $('loadSampleCount').textContent = (values.length ? `${values.length} 个有效${container ? '时间点' : ` ${metric.name} 样本`}` : `尚无有效 ${metric.name} 样本`)
          + (container && rowIndex === 0 ? ' · 100% = 1 个逻辑核' : '');
        $('observationLocate').disabled = !result.count;
      }
    });
    // The network row can disappear when switching to a container.
    if (container && selected >= columns * 2) selected = -1;
    if (container && cells.slice(columns * 2).some(cell => cell.tabIndex === 0)) {
      cells.forEach((cell, index) => { cell.tabIndex = index === 0 ? 0 : -1; });
    }
    $('loadMapDetail').textContent = selected >= 0 ? descriptions[selected]
      : points.length ? container ? '内存 ≥ 90% 标为高占用 · CPU 按逻辑核计量' : 'CPU ≥ 80% / 内存 ≥ 90% 标黄 · 网络亮度为相对吞吐'
      : historyError ? '历史暂时无法读取' : history?.error ? '历史采样保存失败'
      : history?.stale || history?.last_saved_at ? '所选时段没有已保存的采样'
      : request ? '正在读取已保存的采样…' : requestKey ? '所选时段暂无采样，首次保存通常需要 30 秒' : '等待首次采样';
    highlight();
    DashboardMotion.followLatest(grid.closest('.load-map-scroll'), options.viewportKey);
  }
  return {update, source, pause};
})();
