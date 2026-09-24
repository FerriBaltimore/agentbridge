// Private CLIProxyAPI OAuth transport. Secrets enter only through the local
// stdio request and the loopback Authorization header; nothing is persisted.
import { isIP } from 'node:net';
import { GrantBridgeError, requireThat } from './agentbridge-proxy-errors.mjs';

const MANAGEMENT_PATH = Object.freeze({
  codex: '/codex-auth-url',
  claude: '/anthropic-auth-url',
  grok: '/xai-auth-url',
});
const STATE_PATTERN = /^[A-Za-z0-9_.-]{1,128}$/;
const MAX_RESPONSE_BYTES = 16 * 1024;
const MAX_AUTH_URL_LENGTH = 8192;

function providerOf(value) {
  requireThat(typeof value === 'string' && Object.hasOwn(MANAGEMENT_PATH, value),
    'invalid_provider', 'The proxy provider is not supported.');
  return value;
}

function stateOf(value, code = 'invalid_params') {
  requireThat(typeof value === 'string' && STATE_PATTERN.test(value) && !value.includes('..'),
    code, 'Invalid OAuth state.');
  return value;
}

function baseOf(value) {
  let url;
  try { url = new URL(value); } catch { /* Report only the safe validation code. */ }
  const host = url?.hostname;
  const ip = host?.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host;
  const loopback = ip === '::1' || (typeof ip === 'string' && isIP(ip) === 4 && ip.startsWith('127.'));
  requireThat(typeof value === 'string' && url?.protocol === 'http:' && loopback &&
    url.port && url.pathname === '/v1' && !url.username && !url.password &&
    !url.search && !url.hash && url.href === value,
  'invalid_proxy_endpoint', 'The proxy endpoint must be a canonical local HTTP /v1 URL.');
  return url.origin;
}

function keyOf(value) {
  requireThat(typeof value === 'string' && value.length > 0 && value.length <= 4096 &&
    !/[\r\n\x00-\x1f\x7f]/.test(value), 'invalid_params', 'Invalid proxy management key.');
  return value;
}

async function managementRequest(params, method, suffix, body = undefined) {
  const base = baseOf(params?.base_url);
  const key = keyOf(params?.management_key);
  const url = `${base}/v0/management${suffix}`;
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${key}`, Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      redirect: 'manual',
      signal: AbortSignal.timeout(5_000),
    });
  } catch {
    throw new GrantBridgeError('proxy_unavailable', 'The local proxy did not respond.');
  }
  if (response.status < 200 || response.status >= 300) {
    await response.body?.cancel().catch(() => {});
    throw new GrantBridgeError('proxy_rejected', 'The local proxy rejected the request.');
  }
  const size = Number(response.headers.get('content-length'));
  if (Number.isFinite(size) && size > MAX_RESPONSE_BYTES) {
    await response.body?.cancel().catch(() => {});
    throw new GrantBridgeError('proxy_invalid_response', 'The local proxy response is invalid.');
  }
  if (!response.body) throw new GrantBridgeError('proxy_invalid_response', 'The local proxy response is invalid.');
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > MAX_RESPONSE_BYTES) throw new Error('oversized response');
      chunks.push(value);
    }
  } catch {
    await reader.cancel().catch(() => {});
    throw new GrantBridgeError('proxy_invalid_response', 'The local proxy response is invalid.');
  }
  try {
    const bytes = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    requireThat(data && typeof data === 'object' && !Array.isArray(data),
      'proxy_invalid_response', 'The local proxy response is invalid.');
    return data;
  } catch {
    throw new GrantBridgeError('proxy_invalid_response', 'The local proxy response is invalid.');
  }
}

function authorizationUrl(value) {
  let url;
  try { url = new URL(value); } catch { /* Only return a safe validation code. */ }
  requireThat(typeof value === 'string' && value.length <= MAX_AUTH_URL_LENGTH &&
    url?.protocol === 'https:' && url.hostname && !url.username && !url.password,
  'proxy_invalid_response', 'The local proxy response is invalid.');
  return value;
}

function userCode(value) {
  requireThat(typeof value === 'string' && /^[\x21-\x7e]{1,128}$/.test(value),
    'proxy_invalid_response', 'The local proxy response is invalid.');
  return value;
}

export async function proxyStart(params) {
  const provider = providerOf(params?.provider);
  const key = keyOf(params?.management_key);
  const suffix = `${MANAGEMENT_PATH[provider]}?is_webui=true`;
  const data = await managementRequest(params, 'GET', suffix);
  requireThat(data.status === 'ok', 'proxy_rejected', 'The local proxy did not start authentication.');
  const id = stateOf(data.state, 'proxy_invalid_response');
  const result = { id, provider, status: 'awaiting_user', authorizationUrl: authorizationUrl(data.url) };
  if (provider === 'grok' && data.user_code !== undefined) result.userCode = userCode(data.user_code);
  requireThat(![id, result.authorizationUrl, result.userCode].some(value => value?.includes(key)) &&
    !result.authorizationUrl.includes(encodeURIComponent(key)),
  'proxy_invalid_response', 'The local proxy response is invalid.');
  return result;
}

export async function proxyStatus(params, cancelled = new Set()) {
  const provider = providerOf(params?.provider);
  const id = stateOf(params?.state);
  const base = baseOf(params?.base_url);
  keyOf(params?.management_key);
  if (cancelled.has(`${base}\0${provider}\0${id}`)) return { id, provider, status: 'cancelled' };
  const data = await managementRequest(params, 'GET', `/get-auth-status?state=${encodeURIComponent(id)}`);
  if (data.status === 'error' && data.error === 'unknown or expired state') {
    throw new GrantBridgeError('authentication_outcome_unknown', 'The local proxy no longer knows this OAuth session.');
  }
  const status = new Map([['wait', 'awaiting_user'], ['ok', 'authorized'], ['error', 'failed']]).get(data.status);
  requireThat(status, 'proxy_invalid_response', 'The local proxy response is invalid.');
  return { id, provider, status };
}

export async function proxyCancel(params, cancelled = new Set()) {
  const provider = providerOf(params?.provider);
  const id = stateOf(params?.state);
  const base = baseOf(params?.base_url);
  keyOf(params?.management_key);
  const marker = `${base}\0${provider}\0${id}`;
  if (cancelled.has(marker)) return { id, provider, status: 'cancelled' };
  const data = await managementRequest(params, 'DELETE', `/oauth-session?state=${encodeURIComponent(id)}`);
  requireThat(data.status === 'ok' && typeof data.cancelled === 'boolean',
    'proxy_invalid_response', 'The local proxy response is invalid.');
  if (data.cancelled) {
    cancelled.add(marker);
    return { id, provider, status: 'cancelled' };
  }
  const current = await managementRequest(params, 'GET', `/get-auth-status?state=${encodeURIComponent(id)}`);
  if (current.status === 'ok') return { id, provider, status: 'authorized' };
  if (current.status === 'error' && current.error === 'unknown or expired state') {
    // An expired completed session is indistinguishable from a cancelled one.
    throw new GrantBridgeError('authentication_outcome_unknown', 'The local proxy no longer knows this OAuth session.');
  }
  if (current.status === 'error') return { id, provider, status: 'failed' };
  throw new GrantBridgeError('proxy_invalid_response', 'The local proxy response is invalid.');
}

export async function proxyCallback(params) {
  const provider = providerOf(params?.provider);
  requireThat(provider !== 'grok', 'invalid_provider',
    'Grok uses its device authorization code.');
  const id = stateOf(params?.state);
  const raw = params?.redirect_url;
  let redirect;
  try { redirect = new URL(raw); } catch { /* Return a safe validation code. */ }
  const destination = provider === 'codex' ? ['1455', '/auth/callback'] : ['54545', '/callback'];
  const code = redirect?.searchParams.getAll('code') || [];
  const state = redirect?.searchParams.getAll('state') || [];
  requireThat(typeof raw === 'string' && raw.length <= 8192 &&
    redirect?.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(redirect.hostname) &&
    redirect.port === destination[0] && redirect.pathname === destination[1] &&
    !redirect.username && !redirect.password && !redirect.hash &&
    code.length === 1 && state.length === 1 && code[0].length > 0 &&
    code[0].length <= 4096 && state[0] === id,
  'invalid_params', 'Invalid OAuth callback URL.');
  let result;
  try {
    result = await managementRequest(params, 'POST', '/oauth-callback',
      { provider, redirect_url: raw });
  } catch {
    throw new GrantBridgeError('authentication_outcome_unknown',
      'The local proxy callback outcome is unknown. Check the same login attempt.');
  }
  requireThat(result.status === 'ok', 'authentication_outcome_unknown',
    'The local proxy callback outcome is unknown. Check the same login attempt.');
  return { id, provider, status: 'awaiting_user' };
}
