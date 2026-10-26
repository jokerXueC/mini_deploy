(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'userGuideDialog';
  dialog.className = 'user-guide';
  dialog.setAttribute('aria-labelledby', 'userGuideTitle');
  dialog.innerHTML = `<header class="guide-head"><h2 id="userGuideTitle">安装与功能使用指南</h2><div class="guide-actions">
    <a class="toolbar-icon" href="ui/user-guide.md" download="mini_deploy-使用指南.md" title="下载指南文档" aria-label="下载指南文档"><img src="ui/previews/ssh-ai/icons/download.svg" alt=""></a>
    <button id="userGuideRetry" class="toolbar-icon" type="button" title="重新加载指南与本机命令" aria-label="重新加载指南与本机命令"><img src="ui/previews/ssh-ai/icons/refresh-cw.svg" alt=""></button>
    <button id="userGuideClose" class="toolbar-icon" type="button" title="关闭使用指南" aria-label="关闭使用指南"><img src="ui/previews/ssh-ai/icons/x.svg" alt=""></button></div></header>
    <p id="userGuideFeedback" class="guide-feedback" role="status" aria-live="polite"></p>
    <div class="guide-layout"><aside class="guide-sidebar"><input id="userGuideSearch" type="search" placeholder="搜索功能或命令" aria-label="搜索指南"><nav id="userGuideNav" aria-label="指南目录"></nav></aside>
    <article id="userGuideContent" class="guide-content" tabindex="0" aria-label="使用指南正文"></article></div>`;
  document.body.append(dialog);
  const el = id => document.getElementById(id);
  const content = el('userGuideContent'), nav = el('userGuideNav'), search = el('userGuideSearch');
  let controller = null, generation = 0, selected = 'start', opener = null;
  const icon = name => `<img src="ui/previews/ssh-ai/icons/${name}.svg" alt="">`;

  function codeBlock(text) {
    const wrapper = document.createElement('div'); wrapper.className = 'guide-code';
    const pre = document.createElement('pre'), code = document.createElement('code');
    code.textContent = text; pre.append(code); wrapper.append(pre);
    addCopy(wrapper); return wrapper;
  }
  function addCopy(wrapper) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'toolbar-icon guide-copy';
    button.title = '复制完整代码块'; button.setAttribute('aria-label', button.title);
    button.innerHTML = icon('copy'); wrapper.prepend(button);
  }
  function render(markdown, installation) {
    content.innerHTML = ''; nav.innerHTML = '';
    const local = document.createElement('section'); local.dataset.guideTopic = 'local';
    const heading = document.createElement('h2'); heading.id = 'user-guide-local'; heading.textContent = '本机排查命令'; local.append(heading);
    const note = document.createElement('p');
    note.textContent = installation?.available
      ? `以下命令按当前安装生成，在面板所在服务器执行。程序：${installation.app}；数据：${installation.data}；服务：${installation.service}。`
      : installation?.detail || '未取得当前安装信息。指南中的命令使用默认安装路径，自定义安装请核对后执行。';
    local.append(note);
    for (const item of installation?.commands || []) {
      if (typeof item.command !== 'string') continue;
      const title = document.createElement('h3'); title.textContent = item.title + (item.changes_server ? '（会修改服务器）' : '（只读）');
      local.append(title, codeBlock(item.command));
    }
    content.append(local);
    const parsed = document.createElement('div');
    parsed.innerHTML = DOMPurify.sanitize(marked.parse(markdown), {
      ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol', 'li', 'a', 'pre', 'code', 'strong', 'em', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'blockquote', 'hr', 'br'],
      ALLOWED_ATTR: ['href', 'id', 'title'],
    });
    let section = null;
    const topics = new Set(['local']);
    for (const node of [...parsed.childNodes]) {
      if (node.nodeName === 'H1') continue;
      if (node.nodeName === 'H2') {
        const topic = /^[a-z][a-z-]*$/.test(node.id) && !topics.has(node.id) ? node.id : `section-${topics.size}`;
        topics.add(topic); node.id = `user-guide-${topic}`;
        section = document.createElement('section'); section.dataset.guideTopic = topic; content.append(section);
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        node.removeAttribute('id');
      }
      section?.append(node);
    }
    content.querySelectorAll('pre').forEach(pre => {
      if (pre.parentElement.classList.contains('guide-code')) return;
      const wrapper = document.createElement('div'); wrapper.className = 'guide-code';
      pre.replaceWith(wrapper); wrapper.append(pre); addCopy(wrapper);
    });
    content.querySelectorAll('table').forEach(table => {
      const wrapper = document.createElement('div'); wrapper.className = 'guide-table';
      table.replaceWith(wrapper); wrapper.append(table);
    });
    content.querySelectorAll('a').forEach(link => {
      if (link.getAttribute('href')?.startsWith('#')) return;
      if (!/^https:\/\//i.test(link.getAttribute('href') || '')) { link.removeAttribute('href'); return; }
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    });
    for (const part of content.querySelectorAll('section')) {
      const link = document.createElement('a'); link.href = `#${part.dataset.guideTopic}`;
      link.textContent = part.querySelector('h2').textContent; link.dataset.guideLink = part.dataset.guideTopic; nav.append(link);
    }
    const empty = document.createElement('p'); empty.id = 'userGuideEmpty'; empty.className = 'guide-empty'; empty.hidden = true;
    empty.textContent = '没有匹配内容，请换一个关键词。'; content.append(empty);
  }
  function select(topic) {
    const section = [...content.querySelectorAll('section')].find(part => part.dataset.guideTopic === topic)
      || content.querySelector('[data-guide-topic=start]');
    if (!section) return;
    selected = section.dataset.guideTopic;
    search.value = ''; filter();
    section.querySelector('h2').scrollIntoView({block: 'start'});
    nav.querySelectorAll('a').forEach(link => {
      if (link.dataset.guideLink === selected) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }
  function filter() {
    const query = search.value.trim().toLocaleLowerCase();
    let found = 0;
    content.querySelectorAll('section').forEach(section => {
      section.hidden = !section.textContent.toLocaleLowerCase().includes(query);
      nav.querySelector(`[data-guide-link="${section.dataset.guideTopic}"]`).hidden = section.hidden;
      if (!section.hidden) found++;
    });
    if (el('userGuideEmpty')) el('userGuideEmpty').hidden = found > 0;
  }
  async function open(topic = 'start') {
    if (!dialog.open) { opener = document.activeElement; dialog.showModal(); }
    selected = topic; controller?.abort(); controller = new AbortController();
    const request = controller, run = ++generation;
    const timeout = setTimeout(() => request.abort(), 10000);
    el('userGuideFeedback').textContent = '正在读取本地指南与安装信息…';
    el('userGuideRetry').disabled = true; content.setAttribute('aria-busy', 'true');
    try {
      const results = await Promise.allSettled([
        fetch('ui/user-guide.md', {credentials: 'same-origin', signal: request.signal}).then(response => {
          if (!response.ok) throw new Error('指南文档暂不可用'); return response.text();
        }),
        fetch('setup/checks', {credentials: 'same-origin', signal: request.signal}).then(async response => {
          if (!response.ok) throw new Error('本机命令暂不可用');
          const data = await response.json(); return data.installation;
        }),
      ]);
      if (generation !== run || !dialog.open) return;
      if (results[0].status !== 'fulfilled') throw results[0].reason;
      if (!window.marked || !window.DOMPurify) throw new Error('指南组件未加载，请刷新页面');
      render(results[0].value, results[1].status === 'fulfilled' ? results[1].value : null);
      select(topic);
      el('userGuideFeedback').textContent = results[1].status === 'fulfilled' && results[1].value
        ? '' : '指南已加载；本机安装信息未取得，可重新加载。';
    } catch (error) {
      if (generation === run && dialog.open) el('userGuideFeedback').textContent = request.signal.aborted
        ? '加载超时，请重新加载。' : `加载未完成：${error.message}。可点击右上角下载文档。`;
    } finally {
      clearTimeout(timeout);
      if (generation === run) { el('userGuideRetry').disabled = false; content.setAttribute('aria-busy', 'false'); }
    }
  }
  async function copy(value) {
    try { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(value); return; } } catch { /* HTTP and browser permissions use the selection fallback. */ }
    const input = document.createElement('textarea'); input.value = value;
    input.style.cssText = 'position:fixed;opacity:0;pointer-events:none'; dialog.append(input);
    const focus = document.activeElement; input.select();
    try { if (!document.execCommand('copy')) throw new Error('请选中代码块手动复制'); }
    finally { input.remove(); focus?.focus({preventScroll: true}); }
  }
  dialog.addEventListener('click', async event => {
    const link = event.target.closest('a[href^="#"]');
    if (link) { event.preventDefault(); select(link.getAttribute('href').slice(1)); return; }
    const button = event.target.closest('.guide-copy');
    if (!button) return;
    try { await copy(button.parentElement.querySelector('code, pre').textContent); el('userGuideFeedback').textContent = '已复制完整代码块；按所在章节说明执行。'; }
    catch (error) { el('userGuideFeedback').textContent = `复制未完成：${error.message}`; }
  });
  dialog.addEventListener('close', () => { generation++; controller?.abort(); opener?.focus({preventScroll: true}); });
  el('userGuideClose').onclick = () => dialog.close();
  el('userGuideRetry').onclick = () => open(selected);
  el('userGuideOpen').onclick = () => open();
  search.addEventListener('input', filter);
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-user-guide]');
    if (button) open(button.dataset.userGuide);
  });
  const topic = new URLSearchParams(location.search).get('guide');
  if (topic && /^[a-z][a-z-]*$/.test(topic)) open(topic);
})();
