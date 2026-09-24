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

function navigate(view) {
  if (!Object.hasOwn(titles, view)) return;
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
  });
}

function scheduleUsageExpiry() {
  if (usageExpiryTimer !== null) clearTimeout(usageExpiryTimer);
  usageExpiryTimer = null;
  const expiry = nextUsageExpiry(state.usage.values());
  if (expiry === null) return;
  usageExpiryTimer = setTimeout(() => {
    usageExpiryTimer = null;
    if (refreshPromise) return;
    renderAccountSummaries();
    scheduleUsageExpiry();
  }, Math.max(1, expiry - Date.now() + 25));
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

async function loadAccountObservations(accounts) {
  const statuses = new Map();
  const usage = new Map();
  await Promise.all(accounts.map(async (account) => {
    const ref = account.account_ref;
    try {
      const status = await api.accountStatus(ref, true);
      statuses.set(ref, status);
    } catch (error) {
      statuses.set(ref, { error: describeError(error) });
    }
    try {
      usage.set(ref, await api.accountUsage(ref, true));
    } catch (error) {
      usage.set(ref, { supported: false, stale: true, reason: error.code || 'observation_unavailable' });
    }
  }));
  state.statuses = statuses;
  state.usage = usage;
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

navigate('overview');
refreshAll().catch((error) => setGlobalError(describeError(error)));
