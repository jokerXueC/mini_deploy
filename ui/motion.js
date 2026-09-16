/* Shared, interruptible transitions for real sampled values. */
const DashboardMotion = (() => {
  const states = new WeakMap();
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ease = t => t * t * (3 - 2 * t);

  function cancel(element) {
    const state = states.get(element);
    if (state) window.cancelAnimationFrame(state.frame);
    states.delete(element);
  }

  function value(element, target, draw, { initial = target, duration = 1250 } = {}) {
    const old = states.get(element);
    if (old?.target === target) return;
    const start = old?.current ?? initial;
    cancel(element);
    const state = { current: start, target, frame: 0 };
    states.set(element, state);
    if (reduced() || document.hidden || start === target) {
      state.current = target;
      draw(target);
      return;
    }
    const started = performance.now();
    draw(start);
    const step = now => {
      if (!element.isConnected || states.get(element) !== state) return;
      const progress = reduced() || document.hidden ? 1 : Math.min(1, (now - started) / duration);
      state.current = start + (target - start) * ease(progress);
      draw(state.current);
      if (progress < 1) state.frame = window.requestAnimationFrame(step);
    };
    state.frame = window.requestAnimationFrame(step);
  }

  // Cubic Hermite interpolation with monotone-limited tangents. Control points
  // stay between neighbouring values, so smoothing cannot invent a new peak.
  function curve(points) {
    if (!points.length) return '';
    const fmt = n => Number(n.toFixed(3));
    let path = `M${fmt(points[0][0])} ${fmt(points[0][1])}`;
    if (points.length === 1) return path;
    const slopes = points.slice(1).map((p, i) => (p[1] - points[i][1]) / (p[0] - points[i][0]));
    const tangents = points.map((_, i) => {
      if (!i) return slopes[0];
      if (i === points.length - 1) return slopes[i - 1];
      const left = slopes[i - 1], right = slopes[i];
      return left * right <= 0 ? 0 : 2 * left * right / (left + right);
    });
    for (let i = 0; i < slopes.length; i++) {
      if (!slopes[i]) { tangents[i] = 0; tangents[i + 1] = 0; continue; }
      const magnitude = Math.hypot(tangents[i] / slopes[i], tangents[i + 1] / slopes[i]);
      if (magnitude > 3) {
        tangents[i] *= 3 / magnitude;
        tangents[i + 1] *= 3 / magnitude;
      }
    }
    for (let i = 0; i < slopes.length; i++) {
      const [x, y] = points[i], [nextX, nextY] = points[i + 1];
      const dx = (nextX - x) / 3;
      path += ` C${fmt(x + dx)} ${fmt(y + dx * tangents[i])} ${fmt(nextX - dx)} ${fmt(nextY - dx * tangents[i + 1])} ${fmt(nextX)} ${fmt(nextY)}`;
    }
    return path;
  }

  function show(element, duration = 650) {
    if (!reduced() && !document.hidden && element.animate) {
      element.animate([{opacity: 0}, {opacity: 1}], {duration, easing: 'ease-out'});
    }
  }

  function morph(svg, next) {
    const before = Array.from(svg.querySelectorAll('path,circle,line,text'));
    const after = Array.from(next.querySelectorAll('path,circle,line,text'));
    const compatible = svg.getAttribute('viewBox') === next.getAttribute('viewBox') && before.length === after.length &&
      before.every((node, i) => node.tagName === after[i].tagName &&
        (node.getAttribute('d') || '').replace(/[-+]?\d*\.?\d+/g, '#') === (after[i].getAttribute('d') || '').replace(/[-+]?\d*\.?\d+/g, '#'));
    if (!compatible) {
      cancel(svg);
      const oldPaths = Array.from(svg.querySelectorAll('path.trend-line'));
      const newPaths = Array.from(next.querySelectorAll('path.trend-line'));
      const canTween = svg.getAttribute('viewBox') === next.getAttribute('viewBox') &&
        oldPaths.length === newPaths.length && !reduced() && !document.hidden;
      // Sample the displayed geometry, not the source metrics, to bridge growing paths.
      const transitions = canTween ? oldPaths.map((path, i) => {
        const target = newPaths[i];
        const beforeLength = path.getTotalLength(), afterLength = target.getTotalLength();
        if (!beforeLength || !afterLength) return null;
        const samples = Array.from({length: 61}, (_, index) => {
          const fraction = index / 60;
          const from = path.getPointAtLength(beforeLength * fraction);
          const to = target.getPointAtLength(afterLength * fraction);
          return [from.x, from.y, to.x, to.y];
        });
        return {target, samples, finalPath: target.getAttribute('d')};
      }).filter(Boolean) : [];
      svg.setAttribute('viewBox', next.getAttribute('viewBox'));
      if (next.hasAttribute('preserveAspectRatio')) svg.setAttribute('preserveAspectRatio', next.getAttribute('preserveAspectRatio'));
      else svg.removeAttribute('preserveAspectRatio');
      svg.replaceChildren(...Array.from(next.childNodes));
      if (transitions.length) value(svg, 1, progress => {
        transitions.forEach(({target, samples, finalPath}) => {
          target.setAttribute('d', progress === 1 ? finalPath : curve(samples.map(([x, y, nextX, nextY]) =>
            [x + (nextX - x) * progress, y + (nextY - y) * progress])));
        });
      }, {initial: 0, duration: 850});
      return;
    }
    const attrs = ['d', 'cx', 'cy', 'x1', 'x2', 'y1', 'y2', 'x', 'y'];
    const changes = [];
    before.forEach((node, i) => {
      const target = after[i];
      if (node.tagName === 'text') node.textContent = target.textContent;
      attrs.forEach(attr => {
        const from = node.getAttribute(attr), to = target.getAttribute(attr);
        if (from == null || to == null || from === to) return;
        const numbers = from.match(/[-+]?\d*\.?\d+/g)?.map(Number) || [];
        const targets = to.match(/[-+]?\d*\.?\d+/g)?.map(Number) || [];
        changes.push(t => {
          let j = 0;
          node.setAttribute(attr, t === 1 ? to : to.replace(/[-+]?\d*\.?\d+/g, () => {
            const n = numbers[j] + (targets[j] - numbers[j]) * t;
            j++;
            return n.toFixed(3);
          }));
        });
      });
    });
    svg.querySelectorAll('.trend-point').forEach((point, i) => {
      const target = next.querySelectorAll('.trend-point')[i];
      point.dataset.tooltip = target.dataset.tooltip;
      point.setAttribute('aria-label', target.getAttribute('aria-label'));
    });
    cancel(svg);
    if (changes.length) value(svg, 1, t => changes.forEach(draw => draw(t)), {initial: 0, duration: 850});
  }

  return {value, cancel, curve, show, morph, reduced};
})();
