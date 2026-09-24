#!/usr/bin/env node

// Private stdio adapter for AgentBridge's local CLIProxyAPI OAuth flow.
// This entrypoint deliberately imports no npm packages or native providers.
import readline from 'node:readline';
import { GrantBridgeError } from '../src/agentbridge-proxy-errors.mjs';
import { proxyStart, proxyStatus, proxyCancel, proxyCallback } from '../src/agentbridge-proxy.mjs';

const cancelledSessions = new Set();

function safeError(error) {
  const code = error instanceof GrantBridgeError ? error.code : 'provider_error';
  const messages = {
    invalid_provider: 'The selected provider or login mode is not supported.',
    invalid_params: 'Invalid authentication parameters.',
    invalid_proxy_endpoint: 'The proxy endpoint must be a canonical local HTTP /v1 URL.',
    proxy_unavailable: 'The local proxy did not respond.',
    proxy_rejected: 'The local proxy rejected the authentication request.',
    proxy_invalid_response: 'The local proxy returned an invalid response.',
  };
  return { code, message: messages[code] || 'GrantBridge did not complete the operation.' };
}

async function invoke(method, params) {
  if (method === 'health') return { service: 'grantbridge', version: '0.1.0' };
  if (method === 'auth.proxy_start') return proxyStart(params);
  if (method === 'auth.proxy_status') return proxyStatus(params, cancelledSessions);
  if (method === 'auth.proxy_cancel') return proxyCancel(params, cancelledSessions);
  if (method === 'auth.proxy_callback') return proxyCallback(params);
  if (method === 'auth.close') return { closed: true };
  throw new GrantBridgeError('method_not_found', 'Unknown GrantBridge adapter method.');
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line.trim()) continue;
  let request;
  try { request = JSON.parse(line); }
  catch {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: null,
      error: { code: -32700, message: 'Invalid JSON.' } }) + '\n');
    continue;
  }
  const id = request?.id;
  const notification = !Object.prototype.hasOwnProperty.call(request || {}, 'id');
  let response;
  try {
    if (!request || request.jsonrpc !== '2.0' || typeof request.method !== 'string' ||
        (request.params !== undefined && (!request.params || typeof request.params !== 'object' || Array.isArray(request.params)))) {
      throw new GrantBridgeError('invalid_request', 'Expected a JSON-RPC 2.0 request.');
    }
    response = { jsonrpc: '2.0', id, result: await invoke(request.method, request.params || {}) };
  } catch (error) {
    const safe = safeError(error);
    response = { jsonrpc: '2.0', id,
      error: { code: -32000, message: safe.message, data: { code: safe.code } } };
  }
  if (!notification) process.stdout.write(JSON.stringify(response) + '\n');
}
