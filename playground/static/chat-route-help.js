// Keep routing help next to its button and within the visible viewport.
import { byId } from './ui.js';

export function setupRoutingHelp() {
  const button = byId('routing-mode-help');
  const panel = byId('routing-mode-info');

  function position() {
    if (!panel.matches(':popover-open')) return;
    const anchor = button.getBoundingClientRect();
    const bounds = panel.getBoundingClientRect();
    const left = Math.max(12, Math.min(anchor.left, innerWidth - bounds.width - 12));
    const top = Math.max(12, Math.min(anchor.bottom + 8, innerHeight - bounds.height - 12));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  panel.addEventListener('toggle', () => {
    button.setAttribute('aria-expanded', String(panel.matches(':popover-open')));
    position();
  });
  window.addEventListener('resize', position);
  button.closest('.route-primary-fields').addEventListener('scroll', position);
}
