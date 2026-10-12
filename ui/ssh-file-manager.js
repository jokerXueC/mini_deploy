(() => {
  'use strict';
  const el = id => document.getElementById(id), h = escapeHtml;
  const icon = name => `<img class="sf-icon" src="ui/previews/ssh-ai/icons/${name}.svg" alt="">`;
  const button = (id, name, title) => `<button id="${id}" class="toolbar-icon" type="button" title="${title}" aria-label="${title}">${icon(name)}</button>`;
  const dialog = document.createElement('dialog'); dialog.id = 'sshFilesDialog'; dialog.className = 'sf-dialog'; dialog.dataset.preview = 'false';
  dialog.setAttribute('aria-labelledby', 'sfTitle');
  dialog.innerHTML = `<header class="sf-head"><div><h2 id="sfTitle">远程文件</h2><p id="sfTarget"></p></div>${button('sfClose', 'x', '关闭文件管理')}</header><div class="sf-toolbar">${button('sfHome', 'house', '登录目录')}${button('sfUp', 'arrow-up', '上级目录')}${button('sfRefresh', 'refresh-cw', '刷新目录')}<form id="sfPathForm" class="sf-path-form"><input id="sfPath" aria-label="远程目录路径" placeholder="例如 /var/log" spellcheck="false" required maxlength="4096"><button class="ghost-button compact-action" type="submit">打开</button></form>${button('sfNewFolder', 'folder-plus', '新建文件夹')}${button('sfNewFile', 'file-plus', '新建文本文件')}${button('sfUpload', 'upload', '上传文件')}<input id="sfUploadInput" type="file" hidden></div><p id="sfFeedback" class="sf-status" role="status"></p><div class="sf-body"><section class="sf-browser" aria-label="远程目录"><div class="sf-filters"><input id="sfSearch" type="search" aria-label="筛选已读取的文件" placeholder="筛选文件名"><label><input id="sfHidden" type="checkbox">隐藏文件</label></div><div id="sfList" class="sf-list"></div><div class="sf-more"><span id="sfCount" class="sf-status"></span><button id="sfMore" class="ghost-button compact-action" type="button" hidden>加载更多</button></div></section><div id="sfDivider" class="sf-divider" role="separator" aria-label="调整文件列表与预览宽度" aria-orientation="vertical" tabindex="0" aria-valuemin="25" aria-valuemax="65" aria-valuenow="44"></div><section class="sf-preview" aria-label="文件预览"><div class="sf-preview-head" id="sfFileHead" hidden><div><strong id="sfFileName"></strong><small id="sfFileMeta"></small></div><div class="sf-preview-actions"><button id="sfBack" class="ghost-button compact-action" type="button">返回列表</button>${button('sfDownload', 'download', '下载文件')}${button('sfSave', 'save', '预览并保存修改')}</div></div><p id="sfEmpty" class="sf-empty-preview">选择文件查看内容</p><textarea id="sfEditor" aria-label="文件内容" spellcheck="false" readonly hidden></textarea></section></div><section id="sfTransfer" class="sf-transfer" hidden><div class="sf-transfer-top"><strong id="sfTransferTitle"></strong><button id="sfCancelTransfer" type="button" class="ghost-button compact-action">取消传输</button></div><progress id="sfProgress" max="100" value="0" aria-label="文件传输进度"></progress><p id="sfTransferNotice" role="status"></p></section>`;
  document.body.append(dialog);
  const create = document.createElement('dialog'); create.className = 'sf-dialog sf-small'; create.id = 'sfCreateDialog';
  create.setAttribute('aria-labelledby', 'sfCreateTitle');
  create.innerHTML = `<header class="sf-head"><h2 id="sfCreateTitle">新建文件夹</h2>${button('sfCreateClose', 'x', '取消新建')}</header><form id="sfCreateForm"><label>名称<input id="sfCreateName" required maxlength="255" placeholder="例如 logs" spellcheck="false"></label><p id="sfCreateFeedback" role="status"></p><div class="sf-preview-actions"><button id="sfCreateConfirm" type="submit" class="ghost-button compact-action">创建</button></div></form>`;
  document.body.append(create);
  const review = document.createElement('dialog'); review.className = 'sf-dialog sf-small'; review.id = 'sfReviewDialog';
  review.setAttribute('aria-labelledby', 'sfReviewTitle');
  review.innerHTML = `<header class="sf-head"><h2 id="sfReviewTitle">确认保存</h2>${button('sfReviewClose', 'x', '取消保存')}</header><form id="sfReviewForm"><p id="sfReviewPath"></p><pre id="sfDiff" tabindex="0" aria-label="修改差异"></pre><p id="sfReviewNotice"></p><p id="sfReviewFeedback" role="status"></p><div class="sf-preview-actions"><button id="sfReviewConfirm" type="submit" class="ghost-button compact-action">确认保存</button></div></form>`;
  document.body.append(review);
  let session = '', path = '.', entries = [], nextOffset = null, file = null, original = '', generation = 0, listGeneration = 0;
  let controller, listController, transferController, job = '', transferRunning = false, pollTimer, operationBusy = false, listing = false, creation = 'mkdir', preview;
  const message = (value, error = false) => { el('sfFeedback').textContent = value; el('sfFeedback').dataset.error = String(error); };
  const size = value => value == null ? '-' : value >= 1024**2 ? `${(value / 1024**2).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`;
  const changed = () => file?.editable && (file.draft || el('sfEditor').value !== original);
  const discard = () => !changed() || confirm('文件有未保存的修改，确认放弃修改？');
  const remote = (name) => `${path === '/' ? '' : path}/${name}`;
  function resetFile() { file = null; original = ''; el('sfEditor').value = ''; el('sfEditor').hidden = true; el('sfFileHead').hidden = true; el('sfEmpty').hidden = false; el('sfEmpty').textContent = '选择文件查看内容'; dialog.dataset.preview = 'false'; }
  function controls() {
    const connected = !!window.SSHTerminal?.context().session_id;
    el('sshFilesOpen').disabled = !connected;
    ['sfNewFile', 'sfNewFolder', 'sfUpload'].forEach(id => el(id).disabled = listing || path === '.' || operationBusy || !connected || transferRunning);
    el('sfDownload').disabled = !file || listing || operationBusy || transferRunning;
    el('sfSave').disabled = !file?.editable || !changed() || listing || operationBusy || transferRunning;
  }
  async function api(data, signal) { return window.SSHTerminal.fileRequest(data, {signal}); }
  function renderList() {
    const query = el('sfSearch').value.trim().toLowerCase(), visible = entries.filter(item => item.name.toLowerCase().includes(query));
    el('sfList').innerHTML = visible.length ? visible.map(item => `<button class="sf-row" type="button" data-sf-name="${h(item.name)}" aria-pressed="${String(file?.path === remote(item.name))}">${icon(item.kind === 'directory' ? 'folder' : 'file')}<span><strong>${h(item.name)}</strong><small>${h(item.mode)} · ${item.modified ? h(new Date(item.modified * 1000).toLocaleString('zh-CN', {hour12:false})) : '-'}</small></span><span class="sf-size">${item.kind === 'directory' ? '文件夹' : item.kind === 'link' ? '符号链接' : item.kind === 'special' ? '特殊文件' : size(item.size)}</span></button>`).join('') : '<p class="sf-list-empty">目录为空，或没有匹配的已读取文件</p>';
    el('sfMore').hidden = nextOffset === null; el('sfCount').textContent = `${entries.length} 项已读取`;
  }
  async function list(target = path, append = false) {
    if (transferRunning || operationBusy) { message('请先完成当前传输或保存'); return; }
    if (!append && !discard()) return;
    if (!append) { generation++; controller?.abort(); }
    const run = ++listGeneration, view = generation; listController?.abort(); listController = new AbortController();
    listing = true; controls(); message('读取目录…');
    try {
      const result = await api({action:'list', path:target, hidden:el('sfHidden').checked, offset:append ? nextOffset : 0}, listController.signal);
      if (run !== listGeneration || view !== generation || !dialog.open) return;
      if (!append) { resetFile(); entries = []; }
      path = result.path; entries = append ? [...entries, ...result.entries] : result.entries; nextOffset = result.next_offset;
      el('sfPath').value = path; renderList(); controls(); message(result.notice || `${result.total} 项 · ${window.SSHTerminal.context().target}`);
    } catch (error) { if (run === listGeneration && view === generation) message(error.message, true); }
    finally { if (run === listGeneration) { listing = false; controls(); } }
  }
  async function openFile(item) {
    if (listing || transferRunning || operationBusy) { message('请先完成当前读取、传输或保存'); return; }
    if (!discard()) return;
    if (item.kind !== 'file') { message('符号链接和特殊文件仅展示，不打开或修改。'); return; }
    const run = ++generation; controller?.abort(); controller = new AbortController();
    resetFile(); file = {path:remote(item.name), ...item}; el('sfFileName').textContent = item.name;
    el('sfFileMeta').textContent = `${size(item.size)} · ${item.mode} · UID ${item.uid} / GID ${item.gid}`;
    el('sfFileHead').hidden = false; dialog.dataset.preview = 'true'; controls(); message('读取文件…');
    try {
      const result = await api({action:'read', path:file.path}, controller.signal);
      if (run !== generation || !dialog.open) return;
      file = {...file, ...result}; original = result.content;
      el('sfEditor').value = original; el('sfEditor').readOnly = !result.editable; el('sfEditor').hidden = false; el('sfEmpty').hidden = true;
      message(result.notice || 'UTF-8 文本 · 保存前预览差异并备份，保存不会自动重载业务');
      controls(); renderList();
    } catch (error) { if (run === generation) { el('sfEmpty').textContent = '无法预览；普通文件可尝试下载'; message(error.message, true); controls(); } }
  }
  el('sshFilesOpen').addEventListener('click', () => {
    const context = window.SSHTerminal.context(); if (!context.session_id) return;
    if (session !== context.session_id) { session = context.session_id; path = '.'; entries = []; resetFile(); }
    el('sfTarget').textContent = context.target; dialog.showModal(); controls(); list(path);
  });
  window.addEventListener('ssh-session-change', () => {
    const context = window.SSHTerminal?.context();
    if (session && session !== context?.session_id) {
      generation++; listGeneration++; listing = false; controller?.abort(); listController?.abort(); cancelTransfer();
      review.close(); create.close(); dialog.close(); resetFile(); entries = []; path = '.'; session = '';
    }
    controls();
  });
  dialog.addEventListener('cancel', event => { if (!discard()) event.preventDefault(); });
  el('sfClose').addEventListener('click', () => { if (discard()) dialog.close(); });
  dialog.addEventListener('close', () => { generation++; listGeneration++; listing = false; controller?.abort(); listController?.abort(); cancelTransfer(); resetFile(); });
  el('sfPathForm').addEventListener('submit', event => { event.preventDefault(); list(el('sfPath').value.trim()); });
  el('sfHome').addEventListener('click', () => list('.'));
  el('sfUp').addEventListener('click', () => list(path.substring(0, path.lastIndexOf('/')) || '/'));
  el('sfRefresh').addEventListener('click', () => list());
  el('sfMore').addEventListener('click', () => list(path, true));
  el('sfHidden').addEventListener('change', () => list());
  el('sfSearch').addEventListener('input', renderList);
  el('sfEditor').addEventListener('input', controls);
  el('sfBack').addEventListener('click', () => { dialog.dataset.preview = 'false'; });
  el('sfList').addEventListener('click', event => {
    const row = event.target.closest('[data-sf-name]'); if (!row) return;
    const item = entries.find(i => i.name === row.dataset.sfName);
    if (item?.kind === 'directory') list(remote(item.name)); else if (item) openFile(item);
  });
  function newItem(kind) {
    if (!discard()) return; creation = kind;
    el('sfCreateTitle').textContent = kind === 'mkdir' ? '新建文件夹' : '新建文本文件';
    el('sfCreateName').value = ''; el('sfCreateName').placeholder = kind === 'mkdir' ? '例如 logs' : '例如 app.conf';
    el('sfCreateFeedback').textContent = `位置：${path}`; create.showModal(); el('sfCreateName').focus();
  }
  el('sfNewFolder').addEventListener('click', () => newItem('mkdir'));
  el('sfNewFile').addEventListener('click', () => newItem('file'));
  el('sfCreateClose').addEventListener('click', () => create.close());
  el('sfCreateForm').addEventListener('submit', async event => {
    event.preventDefault(); const name = el('sfCreateName').value;
    if (!name || name === '.' || name === '..' || /[\/\x00-\x1f\x7f]/.test(name)) { el('sfCreateFeedback').textContent = '名称不能包含斜杠或控制字符'; return; }
    if (creation === 'file') {
      generation++; controller?.abort();
      resetFile(); file = {path:remote(name), editable:true, sha256:'', draft:true}; original = '';
      el('sfFileName').textContent = name; el('sfFileMeta').textContent = '新文件，保存后创建';
      el('sfFileHead').hidden = false; el('sfEditor').hidden = false; el('sfEditor').readOnly = false; el('sfEmpty').hidden = true;
      dialog.dataset.preview = 'true'; create.close(); controls(); el('sfEditor').focus(); return;
    }
    el('sfCreateConfirm').disabled = true;
    try { await api({action:'mkdir', path:remote(name), confirm:true}); create.close(); await list(); message('文件夹已创建'); }
    catch (error) { el('sfCreateFeedback').textContent = error.message; }
    finally { el('sfCreateConfirm').disabled = false; }
  });
  el('sfSave').addEventListener('click', async () => {
    if (!file?.editable || operationBusy) return;
    operationBusy = true; controls(); const run = generation;
    try {
      const result = await api({action:'preview', path:file.path, content:el('sfEditor').value, expected_sha256:file.sha256 || ''});
      if (run !== generation || !dialog.open) return;
      preview = {...result.preview, content:el('sfEditor').value, path:file.path};
      el('sfReviewPath').textContent = file.path; el('sfDiff').textContent = result.preview.diff || '内容没有变化';
      el('sfReviewNotice').textContent = result.preview.notice + (result.preview.truncated ? ' 差异展示已截断。' : '');
      el('sfReviewFeedback').textContent = ''; review.showModal();
    } catch (error) { if (run === generation) message(error.message, true); }
    finally { operationBusy = false; controls(); }
  });
  el('sfReviewClose').addEventListener('click', () => review.close());
  el('sfReviewForm').addEventListener('submit', async event => {
    event.preventDefault(); if (!preview || operationBusy) return;
    const run = generation; operationBusy = true; controls(); el('sfReviewConfirm').disabled = true;
    try {
      const result = await api({action:'save', path:preview.path, content:preview.content, expected_sha256:preview.before_sha256, preview, confirm:true});
      if (run !== generation || !dialog.open) return;
      if (result.exit_code) throw new Error(result.verification?.detail || '最终内容校验未通过，请检查原文件及备份');
      original = preview.content; file.sha256 = result.sha256; file.draft = false; el('sfEditor').value = original;
      file.size = new TextEncoder().encode(original).length;
      el('sfFileMeta').textContent = `${size(file.size)} · 已保存并校验`;
      entries = entries.map(item => remote(item.name) === file.path ? {...item, size:file.size} : item); renderList();
      review.close(); message(result.backup ? `已保存；原文件备份：${result.backup}。业务配置尚未重载。` : '新文件已保存并回读校验');
    } catch (error) { if (run === generation) el('sfReviewFeedback').textContent = error.message; }
    finally { operationBusy = false; el('sfReviewConfirm').disabled = false; controls(); }
  });
  function progress(title, done, total, notice = '') {
    el('sfTransfer').hidden = false; el('sfTransferTitle').textContent = title;
    el('sfProgress').value = total ? Math.min(100, done / total * 100) : 0;
    el('sfTransferNotice').textContent = `${size(done)} / ${size(total)}${notice ? ' · ' + notice : ''}`;
  }
  async function cancelTransfer() {
    clearTimeout(pollTimer); transferController?.abort(); const previous = job; job = '';
    el('sfCancelTransfer').disabled = true;
    if (previous) { try { await api({action:'cancel', job:previous}); } catch { /* Session close also cancels its transfers. */ } }
    controls();
  }
  el('sfCancelTransfer').addEventListener('click', async () => { await cancelTransfer(); el('sfTransferNotice').textContent = '已请求取消；已提交的写入不会撤销，远程备份与候选文件保留。'; });
  const encode = array => { let text = ''; for (let i = 0; i < array.length; i += 8192) text += String.fromCharCode(...array.subarray(i, i + 8192)); return btoa(text); };
  async function waitTransfer(key, run) {
    while (job === key && run === generation && !transferController.signal.aborted) {
      const status = await api({action:'transfer', job:key}, transferController.signal);
      progress(status.phase, status.progress || 0, status.size, status.result?.backup ? `备份：${status.result.backup}` : '');
      if (status.state === 'failed' || status.state === 'cancelled') throw new Error(`${status.error || status.phase}${status.result?.backup ? `；备份 ${status.result.backup}` : ''}${status.result?.candidate ? `；候选文件 ${status.result.candidate}` : ''}`);
      if (status.state === 'succeeded') return status;
      await new Promise(resolve => {
        const signal = transferController.signal;
        const done = () => { clearTimeout(pollTimer); signal.removeEventListener('abort', done); resolve(); };
        pollTimer = setTimeout(done, 600); signal.addEventListener('abort', done, {once:true});
      });
    }
    throw new Error('传输已停止');
  }
  async function transfer(direction, selected) {
    if (listing || operationBusy || transferRunning || path === '.') return;
    if (direction === 'upload' && (!selected || selected.size > 32 * 1024**2 || /[\/\x00-\x1f\x7f]/.test(selected.name))) { message('请选择文件；每个文件最多 32 MiB，名称不能包含控制字符', true); return; }
    transferRunning = true; transferController = new AbortController(); el('sfCancelTransfer').disabled = false; controls();
    const run = generation; let key = '';
    try {
      if (direction === 'upload') {
        const target = remote(selected.name), existing = entries.find(i => i.name === selected.name);
        let expected = '';
        if (existing) {
          if (existing.kind !== 'file') throw new Error('同名路径不是普通文件，无法上传');
          const inspected = await api({action:'inspect', path:target}, transferController.signal); expected = inspected.sha256;
          if (!confirm(`上传将覆盖 ${target}，原文件会先备份。确认继续？`)) return;
        } else if (!confirm(`将文件上传到 ${target}？`)) return;
        if (run !== generation || transferController.signal.aborted) return;
        const started = await api({action:'upload_begin', path:target, size:selected.size, expected_sha256:expected}, transferController.signal);
        key = job = started.id;
        for (let offset = 0; offset < selected.size; offset += 256 * 1024) {
          if (run !== generation || transferController.signal.aborted) throw new Error('上传已停止');
          const chunk = new Uint8Array(await selected.slice(offset, offset + 256 * 1024).arrayBuffer());
          await api({action:'upload_chunk', job:key, offset, data:encode(chunk)}, transferController.signal);
          progress('上传至面板', offset + chunk.length, selected.size);
        }
        await api({action:'upload_commit', job:key, confirm:true}, transferController.signal);
        const done = await waitTransfer(key, run);
        message(`已上传 ${target}${done.result?.backup ? `；原文件备份：${done.result.backup}` : ''}`);
      } else {
        const target = file.path, name = target.split('/').at(-1);
        const started = await api({action:'download_begin', path:target}, transferController.signal); key = job = started.id;
        const done = await waitTransfer(key, run), chunks = []; let offset = 0;
        while (true) {
          const chunk = await api({action:'download_chunk', job:key, offset}, transferController.signal);
          if (run !== generation || job !== key) throw new Error('下载已停止');
          const binary = atob(chunk.data); chunks.push(Uint8Array.from(binary, c => c.charCodeAt(0)));
          if (!chunk.eof && chunk.next_offset <= offset) throw new Error('下载分块无进展，已停止');
          offset = chunk.next_offset; progress('下载至浏览器', offset, done.size);
          if (chunk.eof) break;
        }
        const blob = new Blob(chunks, {type:'application/octet-stream'}), url = URL.createObjectURL(blob), link = document.createElement('a');
        link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
        message(`下载完成：${name} · SHA-256 ${done.result?.sha256 || ''}`);
      }
    } catch (error) { if (run === generation) { message(error.message, true); el('sfTransferNotice').textContent = error.message; } }
    finally {
      if (key) { try { await api({action:'cancel', job:key}); } catch { /* Closing a session also clears temporary transfer data. */ } }
      if (job === key) job = '';
      transferRunning = false; controls(); el('sfCancelTransfer').disabled = true;
      if (direction === 'upload' && run === generation && dialog.open && !changed()) await list(path);
    }
  }
  el('sfUpload').addEventListener('click', () => { el('sfUploadInput').value = ''; el('sfUploadInput').click(); });
  el('sfUploadInput').addEventListener('change', () => transfer('upload', el('sfUploadInput').files[0]));
  el('sfDownload').addEventListener('click', () => transfer('download'));
  dialog.addEventListener('dragover', event => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
  dialog.addEventListener('drop', event => { event.preventDefault(); if (event.dataTransfer.files.length !== 1) message('每次拖入一个文件'); else transfer('upload', event.dataTransfer.files[0]); });
  let dragging = false;
  const split = value => { const width = Math.max(25, Math.min(65, value)); dialog.style.setProperty('--sf-list-width', `${width}%`); el('sfDivider').setAttribute('aria-valuenow', width.toFixed(0)); };
  el('sfDivider').addEventListener('pointerdown', event => { dragging = true; event.target.setPointerCapture(event.pointerId); });
  el('sfDivider').addEventListener('pointermove', event => { if (dragging) { const rect = dialog.getBoundingClientRect(); split((event.clientX - rect.x) / rect.width * 100); } });
  el('sfDivider').addEventListener('pointerup', () => { dragging = false; });
  el('sfDivider').addEventListener('pointercancel', () => { dragging = false; });
  el('sfDivider').addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) { event.preventDefault(); split(event.key === 'Home' ? 44 : Number(event.target.getAttribute('aria-valuenow')) + (event.key === 'ArrowRight' ? 5 : -5)); } });
  controls();
})();
