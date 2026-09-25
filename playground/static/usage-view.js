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
      stale: row.stale === true || (expiry !== null && expiry <= now)
        || (row.stale !== false && snapshot?.stale !== false),
      observed_at: timeValue(row.observed_at) === null ? null : row.observed_at,
    };
  });
  return windows.sort((left, right) => Number(left.stale) - Number(right.stale)
    || Number(right.scope === 'account') - Number(left.scope === 'account')
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
  if (row.used_percent === null) return 'Unknown usage';
  const shown = formatPercent(row.used_percent);
  const value = `${shown}% used`;
  return row.stale ? `${value} · stale` : value;
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
  const value = node('span', row.used_percent === null ? 'usage-unknown' : row.stale ? 'usage-stale' : 'usage-known', usageValue(row));
  head.append(name, value);
  wrapper.append(head);
  const track = node('div', row.used_percent === null ? 'usage-track is-unknown'
    : row.stale ? 'usage-track is-stale' : 'usage-track');
  track.setAttribute('aria-hidden', 'true');
  if (row.used_percent !== null) {
    const fill = node('span', row.used_percent >= 80 ? 'usage-fill is-high' : 'usage-fill');
    fill.style.width = `${Math.min(100, row.used_percent)}%`;
    track.append(fill);
  }
  wrapper.append(track);
  if (!compact) {
    const details = usageDetails(row);
    if (typeof row.window_seconds === 'number' && Number.isFinite(row.window_seconds)
        && row.window_seconds > 0) {
      details.unshift(`Window ${durationLabel(row.window_seconds)}`);
    }
    if (row.used_percent !== null && row.remaining_percent !== null) {
      details.unshift(`${formatPercent(row.remaining_percent)}% remaining`);
    }
    if (row.scope && row.scope !== 'unknown') details.unshift(`Scope ${String(row.scope).replaceAll('_', ' ')}`);
    if (row.model_id) details.unshift(`Model ${row.model_id}`);
    if (row.model_family) details.unshift(`Model family ${row.model_family}`);
    if (details.length) wrapper.append(node('small', 'account-usage-details', details.join(' · ')));
  }
  return wrapper;
}

export function usageUnavailable(snapshot) {
  const code = snapshot?.refresh_reason || snapshot?.reason;
  const reason = typeof code === 'string' && code
    ? ` (${code.replaceAll('_', ' ')})` : '';
  return `Usage has not been observed${reason}.`;
}
