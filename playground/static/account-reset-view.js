import { api } from './api.js';
import { clear, describeError, formatTime, node, toast } from './ui.js';

const OUTCOMES = new Set(['reset', 'already_redeemed', 'nothing_to_reset', 'no_credit']);
const OUTCOME_LABELS = {
  reset: 'Reset redeemed. Quota was checked again.',
  already_redeemed: 'This reset request was already redeemed.',
  nothing_to_reset: 'No eligible quota window needed a reset.',
  no_credit: 'No reset credit was available.',
};
const DEFINITIVE_PREFLIGHT = new Set([
  'invalid_request', 'reset_observation_changed', 'reset_observation_stale',
  'reset_credit_unavailable', 'reset_credit_changed', 'reset_observation_used',
  'idempotency_conflict', 'proxy_binding_changed', 'busy', 'account_retired',
  'reset_pending', 'authentication_in_progress', 'proxy_binding_unverified',
]);
const STORAGE_PREFIX = 'agentbridge.quota-reset:';

function storageKey(accountId) {
  return `${STORAGE_PREFIX}${accountId}`;
}

function savedOperation(accountId) {
  if (!accountId) return null;
  const raw = localStorage.getItem(storageKey(accountId));
  if (!raw) return null;
  const value = JSON.parse(raw);
  if (value?.account_id !== accountId || typeof value.idempotency_key !== 'string'
      || typeof value.observation_ref !== 'string') {
    throw new Error('The saved reset attempt is invalid.');
  }
  return value;
}

function providerPending(snapshot, accountId) {
  const value = snapshot?.pending_reset;
  if (!value || !accountId) return null;
  if (typeof value.idempotency_key !== 'string'
      || typeof value.observation_ref !== 'string') {
    throw new Error('The account reset receipt is invalid.');
  }
  return { account_id: accountId, idempotency_key: value.idempotency_key,
    observation_ref: value.observation_ref,
    ...(value.credit_id ? { credit_id: value.credit_id } : {}) };
}

function availableCredit(snapshot) {
  const credits = Array.isArray(snapshot?.credits) ? snapshot.credits : [];
  return credits.find((credit) => credit?.status === 'available' && typeof credit.id === 'string');
}

function statusText(snapshot) {
  if (!snapshot) return 'Checking earned resets…';
  if (snapshot.status === 'available' && Number.isInteger(snapshot.available_count)
      && snapshot.available_count > 0) {
    const count = snapshot.available_count;
    return `${count} earned reset${count === 1 ? '' : 's'} available`;
  }
  if (snapshot.status === 'none' && snapshot.available_count === 0) {
    return 'No earned resets available';
  }
  return 'Reset availability unknown';
}

function creditDetails(snapshot) {
  const credits = Array.isArray(snapshot?.credits) ? snapshot.credits : [];
  const available = credits.filter((credit) => credit?.status === 'available');
  if (!available.length) return null;
  const details = node('details', 'reset-credit-details');
  details.append(node('summary', '', `Credit details · ${available.length}`));
  const list = node('ul', '');
  for (const [index, credit] of available.entries()) {
    const expiry = credit.expires_at ? ` · Expires ${formatTime(credit.expires_at)}` : '';
    list.append(node('li', '', `Credit ${index + 1}${expiry}`));
  }
  details.append(list);
  return details;
}

export function createAccountResetView(account, observedStatus, onRedeemed) {
  const root = node('section', 'account-reset-section');
  root.dataset.testid = 'account-reset-section';
  const state = {
    accountId: observedStatus?.account_id || null,
    snapshot: null,
    pending: null,
    busy: false,
    confirming: false,
    error: '',
    disposed: false,
    identityChanged: false,
    storageBlocked: false,
  };

  function readPending() {
    try {
      const local = savedOperation(state.accountId);
      const provider = providerPending(state.snapshot, state.accountId);
      if (local && provider && (local.idempotency_key !== provider.idempotency_key
          || local.observation_ref !== provider.observation_ref
          || (local.credit_id || null) !== (provider.credit_id || null))) {
        throw new Error('The saved reset request differs from the account receipt.');
      }
      state.pending = provider || local;
      state.storageBlocked = false;
    } catch (error) {
      state.error = error.message || 'Saved reset attempt unavailable.';
      state.pending = null;
      state.storageBlocked = true;
    }
  }

  function render() {
    clear(root);
    const heading = node('div', 'account-reset-heading');
    heading.append(node('h3', '', 'Earned resets'));
    const refresh = node('button', 'button button-ghost account-reset-refresh', 'Check resets');
    refresh.type = 'button';
    refresh.dataset.testid = 'account-reset-refresh';
    refresh.disabled = state.busy;
    refresh.addEventListener('click', () => { void load(true); });
    heading.append(refresh);
    root.append(heading);

    const status = node('p', 'account-reset-status', statusText(state.snapshot));
    status.dataset.testid = 'account-reset-status';
    root.append(status);
    if (state.snapshot?.stale) {
      root.append(node('p', 'account-reset-note', 'This reset observation is stale. Check again before using a credit.'));
    } else if (state.snapshot?.observed_at) {
      root.append(node('p', 'account-reset-note', `Checked ${formatTime(state.snapshot.observed_at)}.`));
    }
    const details = creditDetails(state.snapshot);
    if (details) root.append(details);

    if (state.pending) {
      root.append(node('p', 'account-reset-pending',
        'A reset attempt has an unknown outcome. Retry the saved request to resolve it.'));
    }
    if (state.error) {
      const error = node('p', 'account-reset-error', state.error);
      error.setAttribute('role', 'alert');
      root.append(error);
    }

    const canStart = state.snapshot?.status === 'available'
      && state.snapshot.available_count > 0 && !state.snapshot.stale
      && typeof state.snapshot.observation_ref === 'string';
    if (state.confirming) {
      const confirmation = node('div', 'account-reset-confirm');
      confirmation.dataset.testid = 'account-reset-confirmation';
      confirmation.append(node('p', '', state.pending
        ? 'Retry the same saved request? This will not start a new reset attempt.'
        : `Use one earned reset for ${account.name || account.account_ref}? This consumes one credit.`));
      const actions = node('div', 'account-reset-actions');
      const cancel = node('button', 'button button-secondary', 'Cancel');
      cancel.type = 'button';
      cancel.addEventListener('click', () => { state.confirming = false; render(); });
      const confirm = node('button', 'button button-primary', state.pending ? 'Retry saved request' : 'Confirm use');
      confirm.type = 'button';
      confirm.dataset.testid = 'account-reset-confirm';
      confirm.addEventListener('click', () => { void redeem(); });
      actions.append(cancel, confirm);
      confirmation.append(actions);
      root.append(confirmation);
    } else if (!state.identityChanged && !state.storageBlocked && (state.pending || canStart)) {
      const use = node('button', 'button button-secondary account-reset-use',
        state.pending ? 'Resolve saved attempt' : 'Use one reset');
      use.type = 'button';
      use.dataset.testid = 'account-reset-use';
      use.disabled = state.busy;
      use.addEventListener('click', () => { void prepare(); });
      root.append(use);
    }
  }

  async function load(refresh) {
    if (state.busy) return false;
    state.busy = true;
    state.error = '';
    render();
    try {
      const snapshot = await api.accountResetCredits(account.account_ref, refresh);
      if (snapshot?.provider !== 'codex' || typeof snapshot.account_id !== 'string') {
        throw new Error('The Codex reset observation is invalid.');
      }
      if (state.accountId && snapshot.account_id !== state.accountId) {
        state.identityChanged = true;
        throw new Error('The account changed while checking resets.');
      }
      state.accountId = snapshot.account_id;
      state.snapshot = snapshot;
      readPending();
      return true;
    } catch (error) {
      state.error = describeError(error);
      return false;
    } finally {
      state.busy = false;
      if (!state.disposed) render();
    }
  }

  async function prepare() {
    if (state.busy || state.identityChanged || state.storageBlocked) return;
    if (!state.pending && !await load(true)) return;
    if (state.pending || (state.snapshot?.status === 'available'
        && state.snapshot.available_count > 0 && !state.snapshot.stale
        && typeof state.snapshot.observation_ref === 'string')) {
      state.confirming = true;
      render();
    }
  }

  async function redeem() {
    if (state.busy || !state.confirming || !state.accountId || state.identityChanged
        || state.storageBlocked) return;
    let operation = state.pending;
    const wasPending = Boolean(operation);
    if (!operation) {
      const credit = availableCredit(state.snapshot);
      operation = {
        account_id: state.accountId,
        idempotency_key: crypto.randomUUID(),
        observation_ref: state.snapshot.observation_ref,
        ...(credit ? { credit_id: credit.id } : {}),
      };
      try {
        localStorage.setItem(storageKey(state.accountId), JSON.stringify(operation));
      } catch {
        state.error = 'The browser could not save this attempt. No reset request was sent.';
        state.confirming = false;
        render();
        return;
      }
      state.pending = operation;
    }
    state.busy = true;
    state.confirming = false;
    state.error = '';
    render();
    try {
      const { account_id, ...request } = operation;
      const result = await api.redeemAccountReset(account.account_ref, request);
      if (!OUTCOMES.has(result?.outcome)) throw new Error('The provider reset outcome is unknown.');
      try { localStorage.removeItem(storageKey(account_id)); }
      catch { toast('The reset finished, but the browser could not clear its saved receipt.', true); }
      state.pending = null;
      state.snapshot = result.reset_credits;
      toast(OUTCOME_LABELS[result.outcome]);
      try { await onRedeemed(); }
      catch (error) { toast(`The reset finished. ${describeError(error)}`, true); }
    } catch (error) {
      state.error = describeError(error);
      if ((!wasPending && (DEFINITIVE_PREFLIGHT.has(error?.code)
          || error?.data?.outcome === 'not_started'))
          || error?.code === 'reset_attempt_not_started') {
        try {
          localStorage.removeItem(storageKey(state.accountId));
          state.pending = null;
        } catch {
          state.error += ' The saved request could not be cleared.';
        }
      }
    } finally {
      state.busy = false;
      if (!state.disposed) render();
    }
  }

  readPending();
  render();
  void load(true);
  return { element: root, accountRef: account.account_ref,
    dispose() { state.disposed = true; } };
}
