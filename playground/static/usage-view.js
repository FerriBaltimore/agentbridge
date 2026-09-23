import { node, percent, formatTime } from './ui.js';

export function usageSignal(snapshot) {
  const windows = Array.isArray(snapshot?.quota_windows) ? snapshot.quota_windows : [];
  const values = windows.map((window) => percent(window?.used_percent)).filter((value) => value !== null);
  const observed = values.length ? Math.max(...values) : null;
  const fresh = snapshot?.supported === true && snapshot?.stale === false && observed !== null;
  const newest = windows.map((window) => window?.observed_at).filter(Boolean).at(-1);
  return { observed, fresh, newest, modelCount: windows.length };
}

export function usageRow(label, snapshot) {
  const { observed, fresh, newest, modelCount } = usageSignal(snapshot);
  const wrapper = node('div', 'usage-item');
  const head = node('div', 'usage-head');
  head.append(node('strong', '', label));
  head.append(node('span', '', fresh ? `${Math.round(observed)}% used`
    : observed === null ? 'Unknown usage' : 'Stale usage'));
  const track = node('div', fresh ? 'usage-track' : 'usage-track is-unknown');
  track.setAttribute('aria-hidden', 'true');
  if (fresh) {
    const fill = node('span', observed >= 80 ? 'usage-fill is-high' : 'usage-fill');
    fill.style.width = `${observed}%`;
    track.append(fill);
  }
  const details = node('div', 'usage-subline');
  details.append(node('span', '', fresh ? 'Highest observed quota window'
    : snapshot?.reason ? `Reason: ${String(snapshot.reason).replaceAll('_', ' ')}` : 'No fresh quota observation'));
  details.append(node('span', '', newest ? formatTime(newest) : `${modelCount} model windows`));
  wrapper.append(head, track, details);
  return wrapper;
}
