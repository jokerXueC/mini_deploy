window.DockerMirrors = (() => {
  let revision = '', editable = false, busy = false, loading = false, timer = null;
  let rowId = 0, generation = 0;
  const panel = $('dockerMirrorsPanel');
  const feedback = value => { $('dockerMirrorsFeedback').textContent = value; };
  const schedulePoll = delay => { window.clearTimeout(timer); timer = window.setTimeout(poll, delay); };

  function controls() {
    panel.querySelectorAll('input, [data-mirror-remove], #dockerMirrorsAdd, #dockerMirrorsSave').forEach(node => {
      node.disabled = !editable || busy || loading || !revision;
    });
    $('dockerMirrorsRefresh').disabled = busy || loading;
    $('dockerMirrorsTest').disabled = busy || loading || !editable;
  }

  function addRow(value = '') {
    const row = document.createElement('div');
    const id = `dockerMirrorAddress${++rowId}`;
    row.className = 'docker-mirror-row';
    row.innerHTML = `<label for="${id}">加速地址</label><input id="${id}" type="url" maxlength="2048" required placeholder="https://你的加速器地址.mirror.aliyuncs.com" autocomplete="off"><button class="ghost-button compact-action" type="button" data-mirror-remove>移除</button>`;
    row.querySelector('input').value = value;
    $('dockerMirrorsAddresses').append(row);
    controls();
    return row;
  }

  function jobView(job = {}) {
    busy = job.state === 'running';
    if (job.detail) feedback(job.detail);
    $('dockerMirrorsBackup').textContent = job.backup ? `本次备份：${job.backup}` : '';
    $('dockerMirrorsTest').hidden = job.state !== 'succeeded';
    controls();
  }

  async function poll() {
    window.clearTimeout(timer);
    try {
      const result = await fetchJson('docker/mirrors?job=1');
      jobView(result.job);
      if (busy) schedulePoll(1500);
      else await refresh();
    } catch (error) {
      feedback(`暂时无法读取操作状态：${error.message}。后台操作可能仍在进行，将继续查询。`);
      schedulePoll(5000);
    }
  }

  async function refresh() {
    if (loading) return;
    window.clearTimeout(timer);
    const request = ++generation;
    loading = true;
    controls();
    try {
      const result = await fetchJson('docker/mirrors');
      if (request !== generation) return;
      jobView(result.job);
      if (busy) {
        schedulePoll(1500);
        return;
      }
      editable = Boolean(result.editable);
      revision = result.revision || '';
      $('dockerMirrorsSource').textContent = result.path ? `配置文件：${result.path}` : '当前环境不可修改';
      if (!editable) {
        $('dockerMirrorsAddresses').replaceChildren();
        $('dockerMirrorsEffective').textContent = '';
        feedback(result.detail || '无法确认主机 Docker 配置');
        return;
      }
      $('dockerMirrorsAddresses').replaceChildren();
      (result.mirrors || []).forEach(addRow);
      const active = result.effective_mirrors || [];
      const mismatch = JSON.stringify(active) !== JSON.stringify(result.mirrors || []);
      $('dockerMirrorsEffective').textContent = `${mismatch ? '文件与运行配置不同。' : ''}当前生效：${active.length ? active.join('、') : 'Docker 默认拉取方式'}。仅用于 Docker Hub 镜像。`;
      if (!result.job?.detail) feedback((result.mirrors || []).length ? '已读取现有配置' : '尚未配置镜像加速，可添加服务商提供的地址');
    } catch (error) {
      editable = false;
      revision = '';
      feedback(`读取失败：${error.message}`);
    } finally {
      loading = false;
      controls();
    }
  }

  $('dockerMirrorsOpen').addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    $('dockerMirrorsOpen').setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden && (!revision || busy)) refresh();
  });
  $('dockerMirrorsRefresh').addEventListener('click', refresh);
  $('dockerMirrorsAdd').addEventListener('click', () => {
    if ($('dockerMirrorsAddresses').children.length >= 20) return feedback('最多添加 20 个地址');
    addRow().querySelector('input').focus();
  });
  $('dockerMirrorsAddresses').addEventListener('click', event => {
    const button = event.target.closest('[data-mirror-remove]');
    if (button && !button.disabled) button.closest('.docker-mirror-row').remove();
  });
  $('dockerMirrorsForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || loading || !editable || !revision) return;
    const mirrors = Array.from($('dockerMirrorsAddresses').querySelectorAll('input'), input => input.value.trim());
    busy = true;
    generation += 1;
    controls();
    feedback('正在提交配置，请勿重复操作');
    try {
      const result = await postJsonBody('docker/mirrors', {mirrors, revision});
      jobView(result.job);
      schedulePoll(1000);
    } catch (error) {
      busy = false;
      revision = '';
      feedback(`提交未确认：${error.message}。请重新读取，确认后台操作状态后重试。`);
      controls();
    }
  });
  $('dockerMirrorsTest').addEventListener('click', async () => {
    if (busy || loading || !editable) return;
    busy = true;
    controls();
    feedback('正在拉取 nginx:stable-alpine 验证，镜像将保留，不会启动容器');
    try {
      await postJsonBody('docker/images/action', {action: 'pull', reference: 'nginx:stable-alpine'});
      feedback('实际拉取成功；Docker 可能回退到官方源，此结果不保证流量经过加速器。镜像已保留，未启动容器。');
    } catch (error) {
      feedback(`配置仍保留，但实际拉取失败：${error.message}`);
    } finally {
      busy = false;
      controls();
      window.DockerImages?.refresh();
    }
  });
  return {refresh};
})();
