export function byId(id) {
  return document.getElementById(id);
}

export function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined && text !== null) value.textContent = String(text);
  return value;
}

export function clear(element) {
  element.replaceChildren();
  return element;
}

export function emptyState(title, detail, action) {
  const wrapper = node('div', 'empty-state');
  wrapper.append(node('strong', '', title), node('p', '', detail));
  if (action) {
    const button = node('button', 'button button-secondary', action.label);
    button.type = 'button';
    button.addEventListener('click', action.onClick);
    wrapper.append(button);
  }
  return wrapper;
}

export function statusPill(label, tone = 'unknown') {
  const className = tone === 'good' ? 'status-pill'
    : tone === 'danger' ? 'status-pill is-danger'
      : tone === 'muted' ? 'status-pill is-muted' : 'status-pill is-unknown';
  return node('span', className, label);
}

export function pill(label) {
  const value = node('span', 'pill', label);
  value.title = label;
  return value;
}

export function formatTime(value) {
  if (!value) return 'Time unknown';
  const date = typeof value === 'number'
    ? new Date(value < 1e12 ? value * 1000 : value)
    : new Date(value);
  return Number.isNaN(date.getTime())
    ? 'Time unknown'
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

export function formatClock(value) {
  if (!value) return '—';
  const date = typeof value === 'number'
    ? new Date(value < 1e12 ? value * 1000 : value)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? '—'
    : new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(date);
}

export function percent(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
    ? value : null;
}

export function setFeedback(element, message) {
  element.textContent = message || '';
  element.hidden = !message;
}

export function describeError(error) {
  const message = error?.message || 'The local API could not complete this request.';
  return error?.code ? `${message} (${error.code})` : message;
}

export function toast(message, error = false) {
  const region = byId('toast-region');
  const item = node('div', error ? 'toast is-error' : 'toast', message);
  region.append(item);
  setTimeout(() => item.remove(), 6500);
}

export function setGlobalError(message) {
  setFeedback(byId('global-error'), message);
}
