const MUTATION_HEADER = { 'X-AgentBridge-Playground': '1' };

export class PlaygroundError extends Error {
  constructor(code, message, status = 0, data = {}) {
    super(message || 'The local API could not complete the request.');
    this.name = 'PlaygroundError';
    this.code = code || 'request_failed';
    this.status = status;
    this.data = data;
  }
}

async function request(path, { method = 'GET', body, timeout = 30000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const headers = method === 'GET' ? {} : { ...MUTATION_HEADER };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      signal: controller.signal,
    });
  } catch (error) {
    const message = error?.name === 'AbortError'
      ? 'The local API timed out. Check the saved state before retrying.'
      : 'The local API is unavailable. Check that the playground server is running.';
    throw new PlaygroundError(error?.name === 'AbortError' ? 'timeout' : 'connection_failed', message);
  } finally {
    clearTimeout(timer);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new PlaygroundError('invalid_response', 'The local API returned an invalid response.', response.status);
  }
  if (!response.ok || payload?.error) {
    const error = payload?.error || {};
    throw new PlaygroundError(error.code, error.message, response.status, error.data);
  }
  if (!payload || !Object.hasOwn(payload, 'result')) {
    throw new PlaygroundError('invalid_response', 'The local API response has no result.', response.status);
  }
  return payload.result;
}

function ref(value) {
  return encodeURIComponent(value);
}

function query(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  return params.size ? `?${params}` : '';
}

export const api = {
  meta: () => request('/api/meta'),
  capabilities: () => request('/api/capabilities'),
  accounts: () => request('/api/accounts'),
  accountStatus: (account, refresh = false) => request(`/api/accounts/${ref(account)}/status${query({ refresh: refresh ? 1 : undefined })}`),
  accountUsage: (account, refresh = false) => request(`/api/accounts/${ref(account)}/usage${query({ refresh: refresh ? 1 : undefined })}`),
  accountResetCredits: (account, refresh = false) => request(
    `/api/accounts/${ref(account)}/reset-credits${query({ refresh: refresh ? 1 : undefined })}`),
  redeemAccountReset: (account, operation) => request(
    `/api/accounts/${ref(account)}/quota/reset`, { method: 'POST', body: operation }),
  models: (refresh = false) => request(`/api/models${query({ refresh: refresh ? 1 : undefined })}`),
  instances: () => request('/api/instances'),
  instance: (id) => request(`/api/instances/${ref(id)}`),
  messages: (id) => request(`/api/instances/${ref(id)}/messages`),
  instanceEvents: (id, afterSeq = 0) => request(`/api/instances/${ref(id)}/events${query({ after_seq: afterSeq })}`),
  turn: (id) => request(`/api/turns/${ref(id)}`),
  turnEvents: (id, afterSeq = 0) => request(`/api/turns/${ref(id)}/events${query({ after_seq: afterSeq })}`),
  streamTurnEvents: (id, afterSeq = 0) => new EventSource(
    `/api/turns/${ref(id)}/stream${query({ after_seq: afterSeq })}`),
  loginStart: (values) => request('/api/accounts/login/start', { method: 'POST', body: values }),
  loginAttempts: (limit = 3, cursor = 0) => request(
    `/api/accounts/login/attempts${query({ limit, cursor })}`),
  loginStatus: (id, ownerRef) => request(`/api/accounts/login/${ref(id)}${query({ owner_ref: ownerRef })}`),
  loginCheck: (id, ownerRef) => request(`/api/accounts/login/${ref(id)}/check`, { method: 'POST', body: { owner_ref: ownerRef } }),
  loginComplete: (id, ownerRef) => request(`/api/accounts/login/${ref(id)}/complete`, { method: 'POST', body: { owner_ref: ownerRef } }),
  loginCancel: (id, ownerRef) => request(`/api/accounts/login/${ref(id)}/cancel`, { method: 'POST', body: { owner_ref: ownerRef } }),
  deleteAccount: (account) => request(`/api/accounts/${ref(account)}`, { method: 'DELETE' }),
  pauseAccount: (account) => request(`/api/accounts/${ref(account)}/pause`, { method: 'POST', body: {} }),
  resumeAccount: (account) => request(`/api/accounts/${ref(account)}/resume`, { method: 'POST', body: {} }),
  createInstance: (values) => request('/api/instances', { method: 'POST', body: values }),
  deleteInstance: (id) => request(`/api/instances/${ref(id)}`, { method: 'DELETE' }),
  updateInstance: (id, values) => request(`/api/instances/${ref(id)}`,
    { method: 'POST', body: values }),
  sendMessage: (id, values) => request(`/api/instances/${ref(id)}/messages`, { method: 'POST', body: values }),
  stopTurn: (id) => request(`/api/turns/${ref(id)}/stop`, { method: 'POST', body: { wait: false } }),
  answerPermission: (turnId, permissionId, decision) => request(
    `/api/turns/${ref(turnId)}/permissions/${ref(permissionId)}`,
    { method: 'POST', body: { decision } },
  ),
};
