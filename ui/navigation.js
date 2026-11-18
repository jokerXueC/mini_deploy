(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const sidebar = $('panelNavigation');
  if (!sidebar) return;
  const page = document.querySelector('main.page');
  const groups = [...sidebar.querySelectorAll('[data-navigation-group]')];
  const mobile = window.matchMedia('(max-width: 760px)');
  const storageKey = 'mini_deploy-navigation-collapsed';
  let collapsed = false, drawerOpen = false, incidentCount = 0;
  try { collapsed = localStorage.getItem(storageKey) === 'true'; } catch (_) {}

  function syncCount() {
    const group = groups.find(group => group.dataset.navigationGroup === 'observe');
    const badge = $('navigationIncidentCount');
    const show = incidentCount > 0 && (collapsed && !mobile.matches || !group.classList.contains('is-open'));
    badge.textContent = incidentCount > 99 ? '99+' : String(incidentCount);
    badge.hidden = !show;
    const button = group.querySelector('.navigation-group-toggle');
    button.setAttribute('aria-label', incidentCount ? `运行观测，${incidentCount} 项待知晓异常` : '运行观测');
    button.title = incidentCount ? `运行观测，${incidentCount} 项待知晓异常` : '运行观测';
  }

  function setGroup(target, open) {
    groups.forEach(group => {
      const expanded = group === target && open;
      group.classList.toggle('is-open', expanded);
      group.querySelector('.navigation-group-toggle').setAttribute('aria-expanded', String(expanded && !(collapsed && !mobile.matches)));
      group.querySelector('.navigation-children').inert = !expanded || collapsed && !mobile.matches;
    });
    syncCount();
  }

  function syncCollapsed() {
    const compact = collapsed && !mobile.matches;
    document.body.classList.toggle('navigation-collapsed', compact);
    $('navigationCollapse').setAttribute('aria-expanded', String(!compact));
    $('navigationCollapse').setAttribute('aria-label', mobile.matches ? '关闭导航' : compact ? '展开侧栏' : '收起侧栏');
    $('navigationCollapse').title = mobile.matches ? '关闭导航' : compact ? '展开侧栏' : '收起侧栏';
    groups.forEach(group => {
      const expanded = !compact && group.classList.contains('is-open');
      group.querySelector('.navigation-children').inert = !expanded;
      group.querySelector('.navigation-group-toggle').setAttribute('aria-expanded', String(expanded));
    });
    syncCount();
    window.dispatchEvent(new Event('resize'));
  }

  function setDrawer(open, restoreFocus = true) {
    drawerOpen = open && mobile.matches;
    document.body.classList.toggle('navigation-mobile-open', drawerOpen);
    $('navigationBackdrop').hidden = !drawerOpen;
    $('navigationOpen').setAttribute('aria-expanded', String(drawerOpen));
    sidebar.inert = mobile.matches && !drawerOpen;
    page.inert = drawerOpen;
    if (drawerOpen) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
      (sidebar.querySelector('.is-open [aria-current=page]') || $('navigationCollapse')).focus({preventScroll: true});
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
      if (restoreFocus && mobile.matches) $('navigationOpen').focus({preventScroll: true});
    }
  }

  groups.forEach(group => group.querySelector('.navigation-group-toggle').addEventListener('click', () => {
    const wasCollapsed = collapsed && !mobile.matches;
    if (wasCollapsed) {
      collapsed = false;
      try { localStorage.setItem(storageKey, 'false'); } catch (_) {}
      syncCollapsed();
    }
    setGroup(group, wasCollapsed || !group.classList.contains('is-open'));
  }));
  $('navigationCollapse').addEventListener('click', () => {
    if (mobile.matches) { setDrawer(false); return; }
    collapsed = !collapsed;
    try { localStorage.setItem(storageKey, String(collapsed)); } catch (_) {}
    syncCollapsed();
  });
  $('navigationOpen').addEventListener('click', () => setDrawer(true));
  $('navigationBackdrop').addEventListener('click', () => setDrawer(false));
  $('globalAIOpen').addEventListener('click', () => { if (drawerOpen) setDrawer(false); });
  sidebar.addEventListener('keydown', event => {
    if (!drawerOpen) return;
    if (event.key === 'Escape') { event.preventDefault(); setDrawer(false); return; }
    if (event.key !== 'Tab') return;
    const controls = [...sidebar.querySelectorAll('a, button')].filter(el => !el.disabled && !el.closest('[inert]') && el.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  mobile.addEventListener('change', () => { setDrawer(false, false); syncCollapsed(); });

  window.PanelNavigation = {
    viewChanged(view) {
      const selected = $(`${view}ViewTab`);
      const group = selected?.closest('[data-navigation-group]');
      if (!group) return;
      groups.forEach(item => item.classList.toggle('is-current', item === group));
      sidebar.querySelectorAll('[data-view]').forEach(button => {
        if (button === selected) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      $('navigationCategory').textContent = group.querySelector('.navigation-label').textContent;
      $('navigationCurrent').textContent = selected.firstChild.textContent.trim();
      setGroup(group, true);
      if (drawerOpen) {
        setDrawer(false, false);
        $('panelTitle').setAttribute('tabindex', '-1');
        $('panelTitle').focus({preventScroll: true});
      }
    },
    updateIncidentCount(count) { incidentCount = count; syncCount(); },
    updateConnection(online) {
      sidebar.dataset.connection = online ? 'online' : 'offline';
      $('navigationConnection').dataset.state = online ? 'online' : 'offline';
      $('navigationConnectionText').textContent = online ? '面板连接正常' : '面板连接失败';
    },
  };
  setDrawer(false, false);
  syncCollapsed();
})();
