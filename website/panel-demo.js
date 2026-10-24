(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let paused = motion.matches, visible = parent === window, frame = 0, previous = 0, elapsed = 0, lastReading = -1, lastMap = -1;
  let received = 20.9, sent = 8.22, metric = 'cpu', range = 'realtime', target = '', pinned = null;
  const periods = {realtime:60, '1h':3600, '24h':86400, '6h':21600, '7d':604800, all:604800};
  const labels = {cpu:'CPU', memory:'内存', network:'网络'};
  const metricKeys = {cpu_percent:'cpu', memory_percent:'memory', network_total_kbps:'network'};
  const bars = {};
  for (const key of ['cpu','memory','disk','network']) bars[key] = [...el(key + 'Bar').children];
  const rows = [...el('loadMap').querySelectorAll('.load-map-row')];
  const cells = rows.map(row => [...row.querySelectorAll('.load-cell')]);
  const targetLabel = document.querySelector('.observation-target');
  // Reset the captured select before the product's select helper enhances it.
  const select = document.createElement('select'); select.id = 'observationTarget'; select.ariaLabel = '观测对象';
  for (const [value, text] of [['','服务器整体'], ['demo-api','demo-api 容器']]) select.add(new Option(text, value));
  targetLabel.replaceChildren(document.createTextNode('观测对象 '), select);
  el('cpuCoreGrid').replaceChildren(...Array.from({length:8}, (_,i) => {
    const cell = document.createElement('div'); cell.className = 'cpu-core-cell'; cell.tabIndex = 0;
    cell.innerHTML = `<span class="cpu-core-dot" aria-hidden="true"></span><span class="cpu-core-id">L${i}</span><span class="cpu-core-percent">-</span><span class="cpu-core-fill" aria-hidden="true"></span>`;
    return cell;
  }));
  el('cpuModel').textContent = 'Intel Xeon · 演示服务器';

  function sample(t) {
    const burst = 60 * Math.exp(-Math.pow(((t + 9000) % 90 - 32) / 7, 2));
    const cpu = 25.6 + 9 * Math.sin(t / 4) + 3 * Math.sin(t / 1.7) + burst;
    return {
      cpu:Math.min(99, cpu), memory:61 + 3 * Math.sin(t / 14) + 1.2 * Math.sin(t / 4),
      disk:41 + .8 * (1 - Math.cos(t / 12)),
      rx:248.5 + 90 * Math.sin(t / 5) + 30 * Math.sin(t / 1.8) + burst * 5,
      tx:84.8 + 28 * Math.sin(t / 6) + 12 * Math.sin(t / 2.2) + burst * 2
    };
  }
  function point(t) {
    const s = sample(t);
    return {cpu:s.cpu * (target ? .65 : 1), memory:s.memory * (target ? .48 : 1), network:(s.rx + s.tx) * (target ? .7 : 1)};
  }
  function format(key, value) { return value.toFixed(1) + (key === 'network' ? ' KiB/s' : '%'); }
  function reading() {
    const s = sample(elapsed), network = Number(s.rx.toFixed(1)) + Number(s.tx.toFixed(1));
    for (const key of ['cpu','memory','disk']) el(key + 'Value').textContent = s[key].toFixed(1) + '%';
    el('networkValue').textContent = network.toFixed(1) + ' KiB/s';
    el('cpuSub').textContent = `Load ${(s.cpu * .08).toFixed(1)} / ${(sample(elapsed - 8).cpu * .08).toFixed(1)} / ${(sample(elapsed - 20).cpu * .08).toFixed(1)}`;
    el('memorySub').textContent = `已用 ${(s.memory * .08).toFixed(2)} GiB / 可用 ${(8 - s.memory * .08).toFixed(2)} GiB`;
    el('diskSub').textContent = `${(s.disk * .8).toFixed(2)} GiB / 80 GiB · /`;
    el('networkSub').textContent = `RX ${s.rx.toFixed(1)} KiB/s / TX ${s.tx.toFixed(1)} KiB/s`;
    el('networkRxTotal').textContent = received.toFixed(4) + ' GiB';
    el('networkTxTotal').textContent = sent.toFixed(4) + ' GiB';
    for (const key of ['cpu','memory','disk','network']) {
      const value = key === 'network' ? network / 10 : s[key];
      const track = el(key + 'Bar');
      track.setAttribute('role','meter'); track.setAttribute('aria-label', labels[key] || '磁盘');
      track.setAttribute('aria-valuemin','0'); track.setAttribute('aria-valuemax','100'); track.setAttribute('aria-valuenow', value.toFixed(1));
      track.dataset.level = key === 'network' ? '' : value >= 90 ? 'bad' : value >= 80 ? 'warn' : '';
      bars[key].forEach((bar, i) => bar.style.setProperty('--fill', Math.max(0, Math.min(1, value / 100 * 60 - i))));
    }
    document.querySelectorAll('.cpu-core-cell').forEach((cell,i) => {
      const value = Math.max(0, Math.min(100, s.cpu + 12 * Math.sin(elapsed / 3 + i)));
      cell.style.setProperty('--core-load', value / 100); cell.querySelector('.cpu-core-percent').textContent = value.toFixed(1) + '%';
      cell.title = `逻辑处理器 ${i}：${value.toFixed(1)}%`;
    });
    el('cpuCoreSummary').textContent = `8 个逻辑处理器 · 平均 ${s.cpu.toFixed(1)}%`;
  }
  function history() {
    return Array.from({length:60}, (_,i) => point(elapsed - (59 - i) / 59 * periods[range]));
  }
  function detail(index, key) {
    const values = history(), s = values[index];
    el('loadMapDetail').textContent = `${index === 59 ? '最近采样' : Math.round((59 - index) / 59 * periods[range]) + ' 秒前'} · ${labels[key]} ${format(key, s[key])} · 演示数据`;
  }
  function draw() {
    const values = history();
    for (let row = 0; row < 3; row++) {
      const key = ['cpu','memory','network'][row];
      rows[row].dataset.selected = String(key === metric);
      cells[row].forEach((cell,i) => {
        const value = values[i][key], ratio = value / (key === 'network' ? 1000 : 100);
        cell.dataset.level = key !== 'network' && value >= (key === 'cpu' ? 80 : 90) ? 'high' : 'normal';
        cell.style.setProperty('--load-intensity', `${Math.min(100, 20 + ratio * 80)}%`);
        cell.dataset.pinned = String(pinned?.index === i && pinned?.key === key);
        cell.setAttribute('aria-pressed', cell.dataset.pinned);
        cell.title = `${labels[key]} ${format(key,value)} · 演示数据`;
        cell.setAttribute('aria-label', cell.title);
      });
    }
    const items = values.map(s => s[metric]), peak = Math.max(...items);
    el('loadAverageLabel').textContent = `${labels[metric]} 平均${metric === 'network' ? '吞吐' : '占用'}`;
    el('loadAverage').textContent = format(metric, items.reduce((a,b) => a + b,0) / 60);
    el('loadPeakLabel').textContent = labels[metric] + ' 峰值'; el('loadPeak').textContent = format(metric, peak);
    el('loadLatest').textContent = format(metric, items[59]); el('loadSampleCount').textContent = '60 个有效采样 · 演示数据';
    el('loadHighLabel').textContent = metric === 'network' ? '高吞吐时间点' : `${labels[metric]} ≥ ${metric === 'cpu' ? 80 : 90}%`;
    el('loadHighCount').textContent = `${items.filter(v => v >= (metric === 'network' ? 800 : metric === 'cpu' ? 80 : 90)).length} / 60`;
    el('systemTrendHint').textContent = `最近 ${range === 'realtime' ? '60 秒' : range === 'all' ? '7 天' : range} · 60 个采样 · 演示数据`;
    el('loadMapRange').textContent = range === 'realtime' ? '最近 60 秒' : '历史回放 · ' + range;
    el('loadMapStart').textContent = range === 'realtime' ? '60 秒前' : range === 'all' ? '7 天前' : range + ' 前';
    el('loadMapEnd').textContent = '现在';
    if (pinned) detail(pinned.index, pinned.key);
    el('serverView').dataset.frame = String(Number(el('serverView').dataset.frame || 0) + 1);
  }
  function showHistory() {
    el('demo-history-detail').textContent = `当前为官网模拟现场，未连接服务器。${target ? 'demo-api 容器' : '服务器整体'}，${el('loadAverageLabel').textContent} ${el('loadAverage').textContent}，峰值 ${el('loadPeak').textContent}。正式面板可继续查看历史、进程与关联事件。`;
    el('demo-history').showModal();
  }
  cells.forEach((row, r) => row.forEach((cell,i) => {
    const key = ['cpu','memory','network'][r];
    cell.addEventListener('mouseenter', () => detail(i,key));
    cell.addEventListener('focus', () => detail(i,key));
    cell.addEventListener('click', () => { pinned = {index:i,key}; draw(); });
    cell.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? 59 : Math.max(0,Math.min(59,i + (event.key === 'ArrowRight' ? 1 : -1)));
      row.forEach((node,j) => { node.tabIndex = j === index ? 0 : -1; }); row[index].focus();
    });
  }));
  document.querySelectorAll('[data-observation-metric]').forEach(button => button.addEventListener('click', () => {
    metric = metricKeys[button.dataset.observationMetric];
    document.querySelectorAll('[data-observation-metric]').forEach(b => b.setAttribute('aria-pressed', String(b === button))); draw();
  }));
  document.querySelectorAll('[data-trend-range]').forEach(button => button.addEventListener('click', () => {
    range = button.dataset.trendRange;
    document.querySelectorAll('[data-trend-range]').forEach(b => b.classList.toggle('active', b === button)); draw();
  }));
  select.addEventListener('change', () => { target = select.value; draw(); });
  el('cpuCoreToggle').onclick = () => {
    const open = el('cpuCorePanel').hidden; el('cpuCorePanel').hidden = !open;
    el('cpuCoreToggle').setAttribute('aria-expanded', String(open));
  };
  document.querySelectorAll('[data-cpu-view]').forEach(button => button.addEventListener('click', () => {
    el('cpuCoreGrid').dataset.view = button.dataset.cpuView;
    document.querySelectorAll('[data-cpu-view]').forEach(b => { b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', String(b === button)); });
  }));
  el('observationLocate').onclick = () => {
    const values = history().map(s => s[metric]); pinned = {index:values.indexOf(Math.max(...values)),key:metric}; draw();
  };
  for (const id of ['serverPerformanceOpen','loadHistoryOpen']) el(id).onclick = showHistory;
  el('demo-history-close').onclick = () => el('demo-history').close();
  for (const button of el('performanceValues').querySelectorAll('button')) button.onclick = showHistory;
  function animate(now) {
    frame = 0;
    if (paused || !visible || document.hidden) return;
    const delta = previous ? Math.min((now - previous) / 1000, .1) : 0; previous = now;
    const s = sample(elapsed + delta / 2);
    received += s.rx * delta / 1048576; sent += s.tx * delta / 1048576; elapsed += delta;
    if (now - lastReading >= 120) { reading(); lastReading = now; }
    if (now - lastMap >= 1000) { draw(); lastMap = now; }
    frame = requestAnimationFrame(animate);
  }
  function schedule() {
    cancelAnimationFrame(frame); frame = 0; previous = 0;
    el('observationStatus').textContent = `${paused ? '已暂停' : '主机 CPU · 内存 · 网络吞吐'} · 演示数据`;
    if (!paused && visible && !document.hidden) frame = requestAnimationFrame(animate);
  }
  addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'mini-deploy-demo-state') return;
    paused = Boolean(event.data.paused); visible = Boolean(event.data.visible); schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  reading(); draw(); schedule();
  parent.postMessage({type:'mini-deploy-demo-ready'}, '*');
})();
