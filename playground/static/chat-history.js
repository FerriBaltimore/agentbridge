const LAST_INSTANCE_KEY = 'agentbridge.playground.last_instance_id';

export function lastViewedInstance() {
  try { return localStorage.getItem(LAST_INSTANCE_KEY); } catch { return null; }
}

export function rememberInstance(id) {
  try { localStorage.setItem(LAST_INSTANCE_KEY, id); } catch { /* Storage is optional. */ }
}

export function forgetInstance(id) {
  try {
    if (localStorage.getItem(LAST_INSTANCE_KEY) === id) localStorage.removeItem(LAST_INSTANCE_KEY);
  } catch { /* Storage is optional. */ }
}

export function instanceTime(instance) {
  const value = instance.updated_at || instance.updated || instance.created_at || instance.created;
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  return Date.parse(value) || 0;
}

export function retainTimelineEvents(rows) {
  const latestDelta = new Map();
  for (const event of rows) {
    if (event.kind === 'message.delta') latestDelta.set(event.turn_id, event.seq);
  }
  return rows.filter((event) => event.kind !== 'message.delta'
    || latestDelta.get(event.turn_id) === event.seq).slice(-2000);
}

export function instanceId(instance) {
  return instance?.instance_id || instance?.id || null;
}

export function key() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
