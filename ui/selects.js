/* Styled form selects retain the native control for validation and submission. */
window.AppSelects = (() => {
  const controls = new Map();
  let opened = null;
  const selector = '#certificatesView select, #projectForm select';

  function close(focus = false) {
    if (!opened) return;
    const state = opened;
    opened = null;
    state.menu.hidden = true;
    state.button.setAttribute('aria-expanded', 'false');
    state.button.removeAttribute('aria-activedescendant');
    if (focus) state.button.focus();
  }

  function position(state) {
    const rect = state.button.getBoundingClientRect();
    if (!rect.width || !rect.height) { close(); return; }
    const below = innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const up = below < 180 && above > below;
    const space = Math.max(60, Math.min(280, up ? above : below));
    state.menu.style.width = `${Math.min(rect.width, innerWidth - 24)}px`;
    state.menu.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - rect.width - 12))}px`;
    state.menu.style.maxHeight = `${space}px`;
    state.menu.style.top = up ? 'auto' : `${rect.bottom + 6}px`;
    state.menu.style.bottom = up ? `${innerHeight - rect.top + 6}px` : 'auto';
  }

  function highlight(state, index) {
    state.active = index;
    Array.from(state.menu.children).forEach((item, i) => item.classList.toggle('highlighted', i === index));
    const option = state.menu.children[index];
    if (option) {
      state.button.setAttribute('aria-activedescendant', option.id);
      option.scrollIntoView({block: 'nearest'});
    }
  }

  function optionDisabled(option) {
    return option.disabled || (option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled);
  }

  function sync(state) {
    const {select, button} = state;
    button.querySelector('.form-select-value').textContent = select.selectedOptions[0]?.textContent || '请选择';
    button.disabled = select.matches(':disabled');
    button.setAttribute('aria-required', String(select.required));
    if (select.validity.valid) {
      button.removeAttribute('aria-invalid');
      state.error.hidden = true;
    }
    if (opened === state && button.disabled) close();
  }

  function choose(state, index) {
    const option = state.select.options[index];
    if (!option || optionDisabled(option) || state.select.matches(':disabled')) return;
    const previous = state.select.selectedIndex;
    state.select.selectedIndex = index;
    close(true);
    sync(state);
    if (previous !== index) {
      state.select.dispatchEvent(new Event('input', {bubbles: true}));
      state.select.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  function open(state) {
    sync(state);
    if (state.button.disabled) return;
    close();
    state.menu.replaceChildren();
    Array.from(state.select.options).forEach((option, index) => {
      const item = document.createElement('div');
      item.id = `${state.menu.id}-${index}`;
      item.className = 'form-select-option';
      item.textContent = option.textContent;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(index === state.select.selectedIndex));
      item.setAttribute('aria-disabled', String(optionDisabled(option)));
      item.addEventListener('pointerdown', event => event.preventDefault());
      item.addEventListener('click', () => choose(state, index));
      state.menu.appendChild(item);
    });
    opened = state;
    state.menu.hidden = false;
    state.button.setAttribute('aria-expanded', 'true');
    position(state);
    highlight(state, state.select.selectedIndex);
  }

  function bind(select) {
    if (controls.has(select)) return;
    const root = document.createElement('div');
    root.className = 'form-select';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'form-select-button';
    button.id = `${select.id}-button`;
    button.setAttribute('role', 'combobox');
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const label = select.closest('label');
    const labelText = label?.querySelector('span')?.textContent || Array.from(label?.childNodes || [])
      .filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
    button.setAttribute('aria-label', labelText || select.getAttribute('aria-label') || '选择选项');
    button.innerHTML = '<span class="form-select-value"></span><span class="select-caret" aria-hidden="true"></span>';
    const menu = document.createElement('div');
    menu.className = 'form-select-menu';
    menu.id = `${select.id}-menu`;
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', labelText || '选项');
    menu.hidden = true;
    button.setAttribute('aria-controls', menu.id);
    const error = document.createElement('small');
    error.className = 'form-select-error';
    error.id = `${select.id}-error`;
    error.hidden = true;
    button.setAttribute('aria-describedby', error.id);
    root.append(button, error);
    select.insertAdjacentElement('afterend', root);
    document.body.appendChild(menu);
    select.classList.add('form-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    const state = {select, root, button, menu, error, active: 0, search: '', searchAt: 0};
    controls.set(select, state);
    button.addEventListener('click', event => { event.preventDefault(); opened === state ? close() : open(state); });
    button.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      if (event.key === 'Tab') { close(); return; }
      if (['Enter', ' '].includes(event.key)) {
        event.preventDefault();
        if (opened === state) choose(state, state.active); else open(state);
        return;
      }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const wasOpen = opened === state;
        if (!wasOpen) open(state);
        const allowed = Array.from(select.options).map((o, i) => optionDisabled(o) ? -1 : i).filter(i => i >= 0);
        let index = allowed.indexOf(state.active);
        if (event.key === 'Home') index = 0;
        else if (event.key === 'End') index = allowed.length - 1;
        else if (wasOpen) index += event.key === 'ArrowDown' ? 1 : -1;
        highlight(state, allowed[Math.max(0, Math.min(index, allowed.length - 1))] ?? -1);
      } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        if (opened !== state) open(state);
        const now = Date.now();
        state.search = (now - state.searchAt > 700 ? '' : state.search) + event.key.toLowerCase();
        state.searchAt = now;
        const index = Array.from(select.options).findIndex(option => !optionDisabled(option) && option.textContent.trim().toLowerCase().startsWith(state.search));
        if (index >= 0) highlight(state, index);
      }
    });
    button.addEventListener('blur', () => { if (opened === state) close(); });
    select.addEventListener('change', () => sync(state));
    select.addEventListener('focus', () => button.focus());
    select.addEventListener('invalid', event => {
      event.preventDefault();
      button.setAttribute('aria-invalid', 'true');
      error.textContent = select.validationMessage;
      error.hidden = false;
      button.focus();
    });
    select.form?.addEventListener('reset', () => queueMicrotask(() => sync(state)));
    sync(state);
  }

  function syncAll() {
    document.querySelectorAll(selector).forEach(bind);
    for (const [select, state] of controls) {
      if (!select.isConnected) {
        if (opened === state) close();
        state.root.remove();
        state.menu.remove();
        controls.delete(select);
      } else sync(state);
    }
  }

  new MutationObserver(records => {
    if (records.some(record => record.target.closest?.('select, fieldset') ||
      [...record.addedNodes, ...record.removedNodes].some(node => node.nodeType === 1 &&
        (node.matches('select') || node.querySelector('select'))))) {
      if (opened) close();
      syncAll();
    }
  }).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['disabled', 'required', 'selected', 'label']});
  document.addEventListener('pointerdown', event => {
    if (opened && !opened.root.contains(event.target) && !opened.menu.contains(event.target)) close();
  });
  window.addEventListener('resize', () => close());
  document.addEventListener('scroll', event => {
    if (opened && !opened.menu.contains(event.target)) close();
  }, true);
  syncAll();
  return {syncAll, close};
})();
