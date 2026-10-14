(() => {
  'use strict';
  const $ = id => document.getElementById(id), panel = $('prometheusPanel');
  let busy = false, enabled = false;
  function render(state) {
    enabled = state.enabled;
    $('prometheusStatus').textContent = enabled ? `采集已开启 · ${new URL('metrics', location.href)} · 独立只读密钥` : '外部采集未开启；本机性能监测继续运行。';
    $('prometheusEnable').hidden = enabled; $('prometheusRotate').hidden = !enabled; $('prometheusDisable').hidden = !enabled;
    for (const id of ['prometheusEnable', 'prometheusRotate', 'prometheusDisable']) $(id).disabled = busy;
  }
  async function refresh() {
    if (busy || !panel.open) return;
    try { render(await fetchJson('metrics/access')); }
    catch (error) { $('prometheusStatus').textContent = error.message; }
  }
  async function change(action) {
    if (busy) return;
    if (action !== 'enable' && !await showConfirmDialog({title:action === 'rotate' ? '重新生成采集密钥' : '关闭外部采集', message:'原采集密钥会立即失效。服务器性能历史与告警仍会继续。', confirmText:'确认'})) return;
    busy = true; render({enabled});
    try {
      const state = await postJsonBody('metrics/access', {action}); render(state);
      $('prometheusCredentials').hidden = !state.token;
      $('prometheusConfig').value = state.token ? `scrape_configs:\n  - job_name: mini_deploy\n    scrape_interval: 15s\n    scheme: ${location.protocol.slice(0, -1)}\n    metrics_path: ${JSON.stringify(new URL('metrics', location.href).pathname)}\n    authorization:\n      type: Bearer\n      credentials: ${state.token}\n    static_configs:\n      - targets: [${JSON.stringify(location.host)}]\n` : '';
      $('prometheusFeedback').textContent = state.token ? '密钥仅允许读取指标，不允许控制服务器。请在可信网络中使用；配置中的访问地址须能从 Prometheus 服务器连通。' : '外部采集已关闭。';
    } catch (error) { $('prometheusFeedback').textContent = error.message; }
    finally { busy = false; render({enabled}); }
  }
  $('prometheusEnable').addEventListener('click', () => change('enable'));
  $('prometheusRotate').addEventListener('click', () => change('rotate'));
  $('prometheusDisable').addEventListener('click', () => change('disable'));
  $('prometheusCopy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('prometheusConfig').value); $('prometheusFeedback').textContent = '采集配置已复制。'; }
    catch { $('prometheusConfig').focus(); $('prometheusConfig').select(); $('prometheusFeedback').textContent = '浏览器不允许自动复制，配置已选中。'; }
  });
  $('prometheusDownload').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([$('prometheusConfig').value], {type:'text/yaml;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = 'mini-deploy-prometheus.yml'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
  });
  panel.addEventListener('toggle', () => { if (panel.open) refresh(); else { $('prometheusConfig').value = ''; $('prometheusCredentials').hidden = true; } });
})();
