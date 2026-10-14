window.DockerImages = (() => {
  let images = [], busy = false, loading = false;
  const feedback = text => { $('dockerImageFeedback').textContent = text; };

  function controls() {
    $('dockerImagePull').disabled = busy || loading;
    $('dockerImageReference').disabled = busy;
    $('dockerImagesRefresh').disabled = busy || loading;
    $('dockerImagesCleanup').disabled = busy || loading || !images.some(item => item.usage === 'unused');
    document.querySelectorAll('[data-image-remove]').forEach(button => { button.disabled = busy || loading; });
  }

  function render() {
    const query = $('dockerImageSearch').value.trim().toLowerCase();
    const visible = images.filter(item => `${item.id} ${item.tags.join(' ')}`.toLowerCase().includes(query));
    const unused = images.filter(item => item.usage === 'unused').length;
    $('dockerImageCount').textContent = `${images.length} 个镜像 · ${unused} 个未使用`;
    $('dockerImageList').innerHTML = visible.length ? visible.map(item => `
      <div class="docker-image-row">
        <div class="docker-image-identity"><strong>${escapeHtml(item.tags.join(' · ') || '未标记镜像')}</strong>
          <code title="${escapeHtml(item.id)}">${escapeHtml(item.id.slice(7, 19))} · ${item.usage === 'in_use' ? '容器引用' : item.usage === 'unused' ? (item.tags.length ? '未使用' : '悬空镜像') : '引用待确认'}</code></div>
        <span>${escapeHtml(item.size)}</span><time>${escapeHtml(item.created)}</time>
        ${item.usage === 'in_use' ? '<span class="muted">保留</span>' : `<button class="ghost-button compact-action danger-text" type="button" data-image-remove="${escapeHtml(item.id)}">删除</button>`}
      </div>`).join('') : `<p class="muted">${images.length ? '没有匹配的镜像' : '暂无本地镜像'}</p>`;
    controls();
  }

  async function refresh() {
    if (loading || busy) return;
    loading = true;
    controls();
    try {
      const result = await fetchJson('docker/images');
      images = result.images || [];
      const cleanup = result.cleanup;
      $('dockerImageCleanupPolicy').textContent = cleanup ? `${cleanup.enabled ? '自动清理已开启：每小时清理创建超过 24 小时的悬空镜像。' : '自动清理已关闭。'}运行中、已停止容器引用的镜像都会保留。${cleanup.at ? ` 上次清理：${new Date(cleanup.at * 1000).toLocaleString()}${cleanup.ok ? '' : '（失败，将重试）'}` : ''}` : '';
      render();
    } catch (error) {
      feedback(`镜像列表读取失败：${error.message}`);
      $('dockerImageCount').textContent = '读取失败';
    } finally {
      loading = false;
      controls();
    }
  }

  async function operate(action, reference) {
    if (busy || loading) return;
    busy = true;
    controls();
    try {
      if (action === 'remove') {
        const image = images.find(item => item.id === reference);
        if (!image) return;
        const confirmed = await showConfirmDialog({title: '删除镜像',
          message: `确认删除 ${image.tags.join('、') || '未标记镜像'}（${reference.slice(7, 19)}）？再次使用时需要重新拉取；有容器引用或多个标签时不会强制删除。`,
          confirmText: '删除镜像', danger: true});
        if (!confirmed) return;
      }
      if (action === 'prune_unused') {
        const confirmed = await showConfirmDialog({title: '清理未使用镜像',
          message: '清理执行时所有未被容器引用的镜像，包括有标签、用于回滚或刚下载的镜像；再次使用需要重新拉取。运行中和已停止容器引用的镜像会保留，不清理容器、数据卷或服务器业务文件。',
          confirmText: '清理未使用镜像', danger: true});
        if (!confirmed) return;
      }
      feedback(action === 'pull' ? `正在拉取 ${reference}，请稍候…` : '正在清理镜像…');
      const result = await postJsonBody('docker/images/action', {action, reference, confirmed: action !== 'pull'});
      feedback(action === 'pull' ? `镜像 ${reference} 已拉取，不会自动启动或更新容器。` : action === 'prune_unused' ? `镜像清理完成。\n${result.output || ''}` : '镜像已删除。');
    } catch (error) {
      feedback(`操作失败：${error.message}`);
    } finally {
      busy = false;
      controls();
      await refresh();
    }
  }

  $('dockerImagesRefresh').addEventListener('click', () => { feedback(''); refresh(); });
  $('dockerImagesCleanup').addEventListener('click', () => operate('prune_unused', ''));
  $('dockerImageSearch').addEventListener('input', render);
  $('dockerImagePullForm').addEventListener('submit', event => {
    event.preventDefault();
    if ($('dockerImagePullForm').reportValidity()) operate('pull', $('dockerImageReference').value.trim());
  });
  $('dockerImageList').addEventListener('click', event => {
    const button = event.target.closest('[data-image-remove]');
    if (button && !button.disabled) operate('remove', button.dataset.imageRemove);
  });
  return {refresh};
})();
