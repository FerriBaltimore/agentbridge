import { byId, clear, emptyState, formatTime, node, statusPill } from './ui.js';
import { usageSignal } from './usage-view.js';

let capacityRows = [];
let capacityPage = 0;
let capacityPageSize = 3;
let capacityControlsReady = false;

function observedModelRefs(models) {
  const rows = Array.isArray(models?.items) ? models.items
    : Array.isArray(models?.models) ? models.models : [];
  const refs = new Set();
  let observedCount = 0;
  for (const model of rows) {
    if (model?.availability !== 'proxy_observed'
        || !Array.isArray(model.observed_account_refs) || !model.observed_account_refs.length) continue;
    observedCount += 1;
    for (const ref of model.observed_account_refs) refs.add(ref);
  }
  return { refs, observedCount };
}

function accountHealth(account, status, routeRefs) {
  const state = status?.status || status?.authentication?.status || 'unknown';
  const auth = status?.authentication?.status || state;
  const verified = status?.binding_verified === true;
  const healthy = ['active', 'usable'].includes(state) && ['active', 'usable'].includes(auth)
    && status?.disabled !== true && status?.unavailable !== true;
  const ready = healthy && verified && routeRefs.has(account.account_ref);
  if (ready) return { ready, label: 'Ready', detail: 'Verified model route', tone: 'good' };
  if (status?.disabled === true) return { ready, label: 'Disabled', detail: 'Route disabled', tone: 'muted' };
  if (status?.unavailable === true) return { ready, label: 'Unavailable', detail: 'Provider unavailable', tone: 'unknown' };
  if (status?.error || !status) {
    return { ready, label: 'Unknown', detail: 'Status observation unavailable', tone: 'unknown' };
  }
  if (healthy && !verified) {
    return { ready, label: 'Unverified', detail: 'Account binding not verified', tone: 'unknown' };
  }
  if (healthy) {
    return { ready, label: 'No route', detail: 'No observed model route', tone: 'unknown' };
  }
  return { ready, label: state.replaceAll('_', ' '), detail: 'Account is not ready', tone: 'muted' };
}

function quota(accountUsage) {
  const signal = usageSignal(accountUsage);
  if (signal.fresh) {
    return { fresh: true, used: Math.round(signal.observed),
      remaining: Math.round(100 - signal.observed), newest: signal.newest };
  }
  return { fresh: false, stale: signal.observed !== null, newest: signal.newest,
    reason: accountUsage?.reason };
}

function capacityRow(entry) {
  const { account, health, quota: usage } = entry;
  const row = node('article', 'capacity-row');
  row.dataset.testid = 'overview-capacity-row';
  row.setAttribute('role', 'listitem');
  const identity = node('div', 'capacity-identity');
  const avatar = node('span', `capacity-avatar is-${account.provider || 'other'}`,
    (account.name || account.account_ref || '?').trim().charAt(0).toUpperCase());
  avatar.setAttribute('aria-hidden', 'true');
  const names = node('div', 'capacity-names');
  names.append(node('strong', '', account.name || account.account_ref),
    node('small', '', account.provider || 'Historical provider'));
  identity.append(avatar, names);
  const condition = node('div', 'capacity-condition');
  condition.append(statusPill(health.label, health.tone), node('small', '', health.detail));
  const quotaArea = node('div', 'capacity-quota');
  if (usage.fresh) {
    const values = node('div', 'capacity-values');
    values.append(node('strong', '', `${usage.used}% used`),
      node('span', '', `${usage.remaining}% remaining`));
    const track = node('div', 'capacity-track');
    track.setAttribute('aria-hidden', 'true');
    const fill = node('span', usage.used >= 80 ? 'capacity-fill is-high' : 'capacity-fill');
    fill.style.width = `${usage.used}%`;
    track.append(fill);
    quotaArea.append(values, track, node('small', '', usage.newest
      ? `Observed ${formatTime(usage.newest)}` : 'Fresh quota observation'));
  } else {
    quotaArea.append(node('strong', 'capacity-unknown', usage.stale ? 'Stale usage' : 'Unknown usage'));
    quotaArea.append(node('small', '', usage.newest
      ? `Last observed ${formatTime(usage.newest)}`
      : usage.reason ? `Reason: ${String(usage.reason).replaceAll('_', ' ')}`
        : 'No fresh quota observation'));
  }
  row.append(identity, condition, quotaArea);
  return row;
}

function measuredPageSize() {
  const list = byId('overview-capacity-list');
  const style = getComputedStyle(list);
  const rowHeight = Number.parseFloat(style.getPropertyValue('--capacity-row-height')) || 82;
  const gap = Number.parseFloat(style.rowGap) || 8;
  return Math.max(1, Math.floor((list.clientHeight + gap) / (rowHeight + gap)));
}

function renderCapacityPage() {
  const list = clear(byId('overview-capacity-list'));
  const controls = byId('overview-page-controls');
  if (!capacityRows.length) {
    list.append(emptyState('No account capacity yet',
      'Connect an account to observe its status and quota.'));
    controls.hidden = true;
    return;
  }
  const nextPageSize = measuredPageSize();
  if (nextPageSize !== capacityPageSize) {
    const firstVisibleIndex = capacityPage * capacityPageSize;
    capacityPageSize = nextPageSize;
    capacityPage = Math.floor(firstVisibleIndex / capacityPageSize);
  }
  const pages = Math.ceil(capacityRows.length / capacityPageSize);
  capacityPage = Math.min(capacityPage, pages - 1);
  const start = capacityPage * capacityPageSize;
  const end = Math.min(start + capacityPageSize, capacityRows.length);
  for (const entry of capacityRows.slice(start, end)) list.append(capacityRow(entry));
  byId('overview-page-status').textContent = `${start + 1}–${end} of ${capacityRows.length} accounts`;
  byId('overview-prev-page').disabled = capacityPage === 0;
  byId('overview-next-page').disabled = capacityPage >= pages - 1;
  controls.hidden = pages <= 1;
}

function ensureCapacityControls() {
  if (capacityControlsReady) return;
  capacityControlsReady = true;
  byId('overview-prev-page').addEventListener('click', () => {
    capacityPage -= 1;
    renderCapacityPage();
  });
  byId('overview-next-page').addEventListener('click', () => {
    capacityPage += 1;
    renderCapacityPage();
  });
  const list = byId('overview-capacity-list');
  if (globalThis.ResizeObserver) {
    new ResizeObserver(() => {
      if (capacityRows.length && measuredPageSize() !== capacityPageSize) renderCapacityPage();
    }).observe(list);
  } else {
    window.addEventListener('resize', () => renderCapacityPage());
  }
}

export function renderOverview({ accounts, models, instances, usage, statuses }) {
  ensureCapacityControls();
  const accountRows = Array.isArray(accounts) ? accounts : [];
  const modelRoutes = observedModelRefs(models);
  const conversations = Array.isArray(instances) ? instances : [];
  capacityRows = accountRows.map((account) => {
    const health = accountHealth(account, statuses?.get(account.account_ref), modelRoutes.refs);
    return { account, health, quota: quota(usage?.get(account.account_ref)) };
  }).sort((left, right) => Number(right.health.ready) - Number(left.health.ready)
    || Number(right.quota.fresh) - Number(left.quota.fresh)
    || (right.quota.remaining ?? -1) - (left.quota.remaining ?? -1)
    || (left.account.name || left.account.account_ref).localeCompare(
      right.account.name || right.account.account_ref));
  const readyCount = capacityRows.filter((entry) => entry.health.ready).length;
  const freshCount = capacityRows.filter((entry) => entry.quota.fresh).length;
  byId('metric-accounts').textContent = String(accountRows.length);
  byId('metric-accounts-detail').textContent = 'Saved provider routes';
  byId('metric-ready').textContent = String(readyCount);
  byId('metric-ready-detail').textContent = `${modelRoutes.observedCount} observed model${modelRoutes.observedCount === 1 ? '' : 's'}`;
  byId('metric-fresh').textContent = String(freshCount);
  byId('metric-fresh-detail').textContent = `of ${accountRows.length} account${accountRows.length === 1 ? '' : 's'}`;
  byId('metric-instances').textContent = String(conversations.length);
  byId('metric-instances-detail').textContent = 'Saved AgentBridge instances';
  byId('overview-open-chat').hidden = !readyCount;
  byId('overview-account-action').textContent = accountRows.length
    ? 'Manage accounts' : 'Connect an account';
  renderCapacityPage();
}
