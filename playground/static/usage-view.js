import { formatTime, node, percent } from './ui.js';

export function observedPercent(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1000
    ? value : null;
}

function windowLabel(value, index) {
  let label = null;
  for (const candidate of [value?.label, value?.name, value?.model_id, value?.id]) {
    if (typeof candidate === 'string' && candidate.trim()) {
      label = candidate.trim().replaceAll('_', ' ');
      break;
    }
  }
  label ||= `Window ${index + 1}`;
  const owner = value?.model_id || value?.model_family;
  return typeof owner === 'string' && owner && !label.toLowerCase().includes(owner.toLowerCase())
    ? `${label} · ${owner}` : label;
}

function timeValue(value) {
  if (value === null || value === undefined || value === '') return null;
  const date = typeof value === 'number'
    ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.getTime();
}

export function usageWindows(snapshot) {
  const rows = Array.isArray(snapshot?.quota_windows) ? snapshot.quota_windows : [];
  const now = Date.now();
  const windows = rows.filter((row) => row && typeof row === 'object').map((row, index) => {
    const expiry = timeValue(row.stale_at);
    return {
      ...row,
      display_label: windowLabel(row, index),
      used_percent: observedPercent(row.used_percent),
      remaining_percent: percent(row.remaining_percent),
      stale: row.stale !== false || snapshot?.stale === true
        || (expiry !== null && expiry <= now),
      observed_at: timeValue(row.observed_at) === null ? null : row.observed_at,
    };
  });
  return windows.filter((row) => !row.stale).sort((left, right) =>
    Number(right.scope === 'account') - Number(left.scope === 'account')
    || (left.window_seconds ?? Infinity) - (right.window_seconds ?? Infinity)
    || left.display_label.localeCompare(right.display_label));
}

export function nextUsageExpiry(snapshots) {
  const now = Date.now();
  let next = null;
  for (const snapshot of snapshots) {
    for (const row of usageWindows(snapshot)) {
      const expiry = timeValue(row.stale_at);
      if (!row.stale && expiry !== null && expiry > now && (next === null || expiry < next)) {
        next = expiry;
      }
    }
  }
  return next;
}

export function usageValue(row) {
  if (row.stale || row.used_percent === null) return 'Unknown usage';
  const shown = formatPercent(row.used_percent);
  return `${shown}% used`;
}

function formatPercent(value) {
  return value > 0 && value < 0.01 ? '<0.01'
    : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
}

function durationLabel(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400} day${seconds === 86400 ? '' : 's'}`;
  if (seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}`;
  if (seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? '' : 's'}`;
  return `${seconds} seconds`;
}

export function usageDetails(row) {
  const details = [];
  if (row.observed_at) details.push(`${row.stale ? 'Observed' : 'Updated'} ${formatTime(row.observed_at)}`);
  if (row.resets_at) details.push(`Resets ${formatTime(row.resets_at)}`);
  return details;
}

export function usageWindowRow(row, { compact = false } = {}) {
  const wrapper = node('div', compact ? 'account-usage-window is-compact' : 'account-usage-window');
  wrapper.dataset.testid = 'account-usage-window';
  const head = node('div', 'account-usage-head');
  const compactDuration = compact && typeof row.window_seconds === 'number'
    && Number.isFinite(row.window_seconds) && row.window_seconds > 0
    ? ` · ${durationLabel(row.window_seconds)}` : '';
  const name = node('strong', 'account-usage-name', `${row.display_label}${compactDuration}`);
  name.title = `${row.display_label}${compactDuration}`;
  const showPercent = !row.stale && row.used_percent !== null;
  const value = node(compact ? 'span' : 'strong', showPercent
    ? 'account-usage-value usage-known' : 'account-usage-value usage-unknown', usageValue(row));
  if (compact) {
    head.append(name, value);
  } else {
    const label = node('div', 'account-usage-label');
    label.append(name);
    const redundantAccountScope = row.scope === 'account'
      && /^account quota\b/i.test(row.display_label);
    if (row.scope && row.scope !== 'unknown' && !redundantAccountScope) {
      label.append(node('span', 'account-usage-context', String(row.scope).replaceAll('_', ' ')));
    }
    head.append(label, value);
  }
  wrapper.append(head);
  const track = node('div', showPercent ? 'usage-track' : 'usage-track is-unknown');
  track.setAttribute('aria-hidden', 'true');
  if (showPercent) {
    const fill = node('span', row.used_percent >= 80 ? 'usage-fill is-high' : 'usage-fill');
    fill.style.width = `${Math.min(100, row.used_percent)}%`;
    track.append(fill);
  }
  wrapper.append(track);
  if (!compact) {
    const details = [];
    if (typeof row.window_seconds === 'number' && Number.isFinite(row.window_seconds)
        && row.window_seconds > 0) {
      details.push(`${durationLabel(row.window_seconds)} window`);
    }
    if (showPercent && row.remaining_percent !== null) {
      details.push(`${formatPercent(row.remaining_percent)}% remaining`);
    }
    details.push(...usageDetails(row));
    if (details.length) {
      const meta = node('div', 'account-usage-meta');
      for (const item of details) meta.append(node('span', '', item));
      wrapper.append(meta);
    }
  }
  return wrapper;
}

export function usageUnavailable(snapshot) {
  const saved = Array.isArray(snapshot?.quota_windows) && snapshot.quota_windows.length > 0;
  return saved
    ? 'The saved quota reading is not current. Refresh data to check again.'
    : 'No current quota reading is available. Refresh data to try again.';
}
