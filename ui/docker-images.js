window.DockerImages = (() => {
  let images = [], busy = false, loading = false;
  const feedback = text => { $('dockerImageFeedback').textContent = text; };

  function controls() {
    $('dockerImagePull').disabled = busy || loading;
    $('dockerImageReference').disabled = busy;
    $('dockerImagesRefresh').disabled = busy || loading;
    document.querySelectorAll('[data-image-remove]').forEach(button => { button.disabled = busy || loading; });
  }

  function render() {
    const query = $('dockerImageSearch').value.trim().toLowerCase();
    const visible = images.filter(item => `${item.id} ${item.tags.join(' ')}`.toLowerCase().includes(query));
    $('dockerImageCount').textContent = `${images.length} 个镜像`;
    $('dockerImageList').innerHTML = visible.length ? visible.map(item => `
      <div class="docker-image-row">
        <div class="docker-image-identity"><strong>${escapeHtml(item.tags.join(' · ') || '未标记镜像')}</strong>
          <code title="${escapeHtml(item.id)}">${escapeHtml(item.id.slice(7, 19))}</code></div>
        <span>${escapeHtml(item.size)}</span><time>${escapeHtml(item.created)}</time>
        <button class="ghost-button compact-action danger-text" type="button" data-image-remove="${escapeHtml(item.id)}">删除</button>
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
      feedback(action === 'pull' ? `正在拉取 ${reference}，请稍候…` : '正在删除镜像…');
      await postJsonBody('docker/images/action', {action, reference, confirmed: action === 'remove'});
      feedback(action === 'pull' ? `镜像 ${reference} 已拉取，不会自动启动或更新容器。` : '镜像已删除。');
    } catch (error) {
      feedback(`操作失败：${error.message}`);
    } finally {
      busy = false;
      controls();
      await refresh();
    }
  }

  $('dockerImagesRefresh').addEventListener('click', () => { feedback(''); refresh(); });
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
