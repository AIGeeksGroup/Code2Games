'use strict';

(() => {
  const section = document.querySelector('.results-section');
  if (!section) return;
  const tooltip = section.querySelector('.chart-tooltip');
  const methodLabel = tooltip.querySelector('.chart-tooltip-method');
  const valueLabel = tooltip.querySelector('.chart-tooltip-value');
  let activeBar = null;

  function hideTooltip() {
    tooltip.hidden = true;
    activeBar = null;
    section.querySelectorAll('.is-muted').forEach((element) => element.classList.remove('is-muted'));
  }

  function positionTooltip(bar, event) {
    const bounds = bar.getBoundingClientRect();
    const x = event && 'clientX' in event ? event.clientX : bounds.left + bounds.width / 2;
    const y = event && 'clientY' in event ? event.clientY : bounds.top;
    const width = tooltip.offsetWidth;
    const height = tooltip.offsetHeight;
    tooltip.style.left = Math.max(12, Math.min(x - width / 2, innerWidth - width - 12)) + 'px';
    const above = y - height - 14;
    tooltip.style.top = Math.max(12, Math.min(above >= 12 ? above : y + 18, innerHeight - height - 12)) + 'px';
  }

  function showTooltip(bar, event) {
    if (activeBar !== bar) {
      hideTooltip();
      activeBar = bar;
      methodLabel.textContent = bar.dataset.method;
      valueLabel.textContent = bar.dataset.metric + ' · ' + bar.dataset.value;
      const chart = bar.closest('.result-chart');
      const series = bar.closest('.chart-series').dataset.series;
      chart.querySelectorAll('[data-series]').forEach((element) => {
        element.classList.toggle('is-muted', element.dataset.series !== series);
      });
    }
    tooltip.hidden = false;
    positionTooltip(bar, event);
  }

  section.querySelectorAll('.chart-bar').forEach((bar) => {
    bar.addEventListener('pointerenter', (event) => showTooltip(bar, event));
    bar.addEventListener('pointermove', (event) => {
      if (activeBar === bar) positionTooltip(bar, event);
    });
    bar.addEventListener('pointerleave', hideTooltip);
    bar.addEventListener('focus', () => showTooltip(bar));
    bar.addEventListener('blur', hideTooltip);
    bar.addEventListener('click', (event) => showTooltip(bar, event));
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideTooltip();
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest('.chart-bar')) hideTooltip();
  });
  window.addEventListener('scroll', hideTooltip, { capture: true, passive: true });
  window.addEventListener('resize', hideTooltip);
})();
