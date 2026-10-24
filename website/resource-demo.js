(() => {
  'use strict';
  const panel = document.getElementById('demo-panel');
  const pause = document.getElementById('demo-pause');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let paused = motion.matches, visible = false;
  function notify() {
    panel.contentWindow?.postMessage({type:'mini-deploy-demo-state', paused, visible:visible && !document.hidden}, '*');
  }
  function setPaused(value) {
    paused = value;
    pause.setAttribute('aria-pressed', String(value));
    pause.title = pause.ariaLabel = value ? '播放演示' : '暂停演示';
    pause.querySelector('img').src = value ? 'assets/play.svg' : 'assets/pause.svg';
    notify();
  }
  addEventListener('message', event => {
    if (event.source === panel.contentWindow && event.data?.type === 'mini-deploy-demo-ready') notify();
  });
  pause.addEventListener('click', () => setPaused(!paused));
  new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting; notify();
  }).observe(panel);
  document.addEventListener('visibilitychange', notify);
  motion.addEventListener('change', () => { if (motion.matches) setPaused(true); });
  setPaused(paused);
})();
