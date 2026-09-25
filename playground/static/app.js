import { api } from './api.js';
import { renderAccounts, setupRemoval } from './accounts-view.js';
import { setupChat } from './chat.js';
import { setupLogin } from './login.js';
import { renderOverview } from './overview.js';
import { byId, describeError, setGlobalError, toast } from './ui.js';
import { nextUsageExpiry } from './usage-view.js';

const state = {
  capabilities: null,
  accounts: [],
  models: { items: [] },
  instances: [],
  usage: new Map(),
  statuses: new Map(),
  workspacePath: '',
};

const titles = { overview: 'Overview', chat: 'Chat', accounts: 'Accounts', activity: 'Activity' };
let refreshPromise = null;
let usageExpiryTimer = null;
const usageRefreshes = new Map();
const usageAttemptAt = new Map();
const usageAttemptCount = new Map();
const usageEpochs = new Map();
const USAGE_RETRY_COOLDOWN_MS = 30_000;

function accountPresent(ref) {
  return state.accounts.some((account) => account.account_ref === ref);
}

function navigate(view) {
  if (!Object.hasOwn(titles, view)) return;
  if (window.location.hash !== `#${view}`) window.history.pushState(null, '', `#${view}`);
  for (const [name] of Object.entries(titles)) {
    const active = name === view;
    byId(`view-${name}`).hidden = !active;
    const link = document.querySelector(`.nav-link[data-view="${name}"]`);
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  document.title = `${titles[view]} · AgentBridge Playground`;
}

const chat = setupChat({ onDataChanged: refreshAll });
const openLogin = setupLogin({
  providers: () => Array.isArray(state.capabilities?.providers) ? state.capabilities.providers : [],
  onComplete: refreshAll,
});
const openRemove = setupRemoval(refreshAll);

function renderAccountSummaries() {
  const shared = {
    accounts: state.accounts,
    models: state.models,
    instances: state.instances,
    usage: state.usage,
    statuses: state.statuses,
    onAccounts: () => navigate('accounts'),
    onChat: () => navigate('chat'),
  };
  renderOverview(shared);
  renderAccounts(state.accounts, state.usage, {
    statuses: state.statuses,
    onRemove: openRemove,
    onChanged: refreshAll,
    onUsageRefresh: refreshAccountUsage,
  });
}

function scheduleUsageExpiry() {
  if (usageExpiryTimer !== null) clearTimeout(usageExpiryTimer);
  usageExpiryTimer = null;
  const deadlines = new Map();
  for (const [ref, snapshot] of state.usage) {
    if (!accountPresent(ref)) continue;
    const expiry = nextUsageExpiry([snapshot]);
    if (expiry !== null) deadlines.set(ref, expiry);
  }
  if (!deadlines.size) return;
  const expiry = Math.min(...deadlines.values());
  usageExpiryTimer = setTimeout(() => {
    usageExpiryTimer = null;
    renderAccountSummaries();
    const now = Date.now();
    for (const [ref, deadline] of deadlines) {
      if (deadline <= now && accountPresent(ref)) void refreshAccountUsage(ref, { force: true });
    }
    scheduleUsageExpiry();
  }, Math.min(2_147_483_647, Math.max(1, expiry - Date.now() + 25)));
}

function render() {
  renderAccountSummaries();
  chat.updateData({
    models: state.models,
    accounts: state.accounts,
    capabilities: state.capabilities,
    workspacePath: state.workspacePath,
    instanceRows: state.instances,
  });
  scheduleUsageExpiry();
}

function requestAccountUsage(ref) {
  const pending = usageRefreshes.get(ref);
  if (pending) return pending;
  const epoch = usageEpochs.get(ref) || 0;
  usageAttemptAt.set(ref, Date.now());
  usageAttemptCount.set(ref, (usageAttemptCount.get(ref) || 0) + 1);
  const request = Promise.resolve().then(() => api.accountUsage(ref, true)).catch((error) => ({
    supported: false, stale: true, reason: error?.code || 'observation_unavailable',
  })).then((snapshot) => {
    if (accountPresent(ref) && (usageEpochs.get(ref) || 0) === epoch) {
      state.usage.set(ref, snapshot);
      if (!refreshPromise) {
        renderAccountSummaries();
        scheduleUsageExpiry();
      }
    }
    return snapshot;
  }).finally(() => {
    if (usageRefreshes.get(ref) === request) usageRefreshes.delete(ref);
  });
  usageRefreshes.set(ref, request);
  return request;
}

function refreshAccountUsage(ref, { force = false } = {}) {
  if (!accountPresent(ref)) return Promise.resolve(null);
  const pending = usageRefreshes.get(ref);
  if (pending) return pending;
  if (refreshPromise) {
    const previousCount = usageAttemptCount.get(ref);
    return refreshPromise.catch(() => null).then(() => {
      if (!accountPresent(ref)) return null;
      if (usageAttemptCount.get(ref) !== previousCount) return state.usage.get(ref);
      const active = usageRefreshes.get(ref);
      if (active) return active;
      const lastAttempt = usageAttemptAt.get(ref);
      if (!force && lastAttempt !== undefined
          && Date.now() - lastAttempt < USAGE_RETRY_COOLDOWN_MS) {
        return state.usage.get(ref);
      }
      return requestAccountUsage(ref);
    });
  }
  const lastAttempt = usageAttemptAt.get(ref);
  if (!force && lastAttempt !== undefined && Date.now() - lastAttempt < USAGE_RETRY_COOLDOWN_MS) {
    return Promise.resolve(state.usage.get(ref));
  }
  return requestAccountUsage(ref);
}

async function loadAccountObservations(accounts) {
  const statuses = new Map();
  const refs = new Set(accounts.map((account) => account.account_ref));
  const knownRefs = new Set([...state.usage.keys(), ...usageRefreshes.keys(),
    ...usageAttemptAt.keys(), ...usageAttemptCount.keys()]);
  for (const ref of knownRefs) {
    if (refs.has(ref)) continue;
    state.usage.delete(ref);
    usageRefreshes.delete(ref);
    usageAttemptAt.delete(ref);
    usageAttemptCount.delete(ref);
    usageEpochs.set(ref, (usageEpochs.get(ref) || 0) + 1);
  }
  await Promise.all(accounts.map(async (account) => {
    const ref = account.account_ref;
    try {
      const status = await api.accountStatus(ref, true);
      statuses.set(ref, status);
    } catch (error) {
      statuses.set(ref, { error: describeError(error) });
    }
    await requestAccountUsage(ref);
  }));
  state.statuses = statuses;
}

async function refreshAll() {
  if (refreshPromise) return refreshPromise;
  const button = byId('refresh-button');
  button.disabled = true;
  setGlobalError('');
  refreshPromise = (async () => {
    const reads = await Promise.allSettled([
      api.meta(), api.capabilities(), api.accounts(), api.models(true), api.instances(),
    ]);
    const [meta, capabilities, accounts, models, instances] = reads;
    if (meta.status === 'fulfilled') state.workspacePath = meta.value.workspace_path || '';
    if (capabilities.status === 'fulfilled') state.capabilities = capabilities.value;
    if (accounts.status === 'fulfilled') state.accounts = Array.isArray(accounts.value) ? accounts.value : [];
    if (models.status === 'fulfilled') state.models = models.value || { items: [] };
    if (instances.status === 'fulfilled') state.instances = Array.isArray(instances.value) ? instances.value : [];
    if (accounts.status === 'fulfilled') await loadAccountObservations(state.accounts);
    const errors = reads.filter((result) => result.status === 'rejected');
    if (errors.length) setGlobalError(`Some local data is unavailable. ${describeError(errors[0].reason)}`);
    const refreshed = `Updated ${new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date())}`;
    byId('refresh-time').textContent = refreshed;
    button.title = `${refreshed} · Refresh data`;
    render();
  })();
  try { await refreshPromise; }
  finally { refreshPromise = null; button.disabled = false; }
}

for (const link of document.querySelectorAll('.nav-link')) {
  link.addEventListener('click', () => navigate(link.dataset.view));
}
for (const link of document.querySelectorAll('[data-go]')) {
  link.addEventListener('click', () => navigate(link.dataset.go));
}
byId('accounts-add').addEventListener('click', openLogin);
byId('refresh-button').addEventListener('click', () => refreshAll().catch((error) => toast(describeError(error), true)));

window.addEventListener('popstate', () => navigate(window.location.hash.slice(1) || 'overview'));
window.addEventListener('hashchange', () => navigate(window.location.hash.slice(1) || 'overview'));
navigate(Object.hasOwn(titles, window.location.hash.slice(1))
  ? window.location.hash.slice(1) : 'overview');
refreshAll().catch((error) => setGlobalError(describeError(error)));
