import { api } from './api.js';
import { accountModels, renderAccountModelsDialog } from './account-models-view.js';
import { createAccountResetView } from './account-reset-view.js';
import { byId, clear, describeError, emptyState, node, setFeedback, statusPill, toast } from './ui.js';
import { usageUnavailable, usageWindowRow, usageWindows } from './usage-view.js';

let selectedModelsAccountRef = null;
let selectedUsageAccountRef = null;
let resetView = null;
let modelsDialogReady = false;
let usageDialogReady = false;
let accountRows = [];
let accountUsageRows = new Map();
let accountStatuses = new Map();
let removeAccount = null;
let accountsChanged = null;
let refreshUsage = null;
let accountPage = 0;
let accountPageSize = 6;
let paginationReady = false;
let layoutQueued = false;

function visiblePageSize() {
  const list = byId('accounts-list');
  const height = list.clientHeight;
  if (!height) return accountPageSize;
  const style = getComputedStyle(list);
  const header = Number.parseFloat(style.getPropertyValue('--account-header-height')) || 0;
  const row = Number.parseFloat(style.getPropertyValue('--account-row-height')) || 92;
  return Math.max(1, Math.floor((height - header) / row));
}

function queuePageLayout() {
  if (layoutQueued) return;
  layoutQueued = true;
  requestAnimationFrame(() => {
    layoutQueued = false;
    const next = visiblePageSize();
    if (next !== accountPageSize) {
      const previousStart = accountPage * accountPageSize;
      const previousEnd = Math.min(previousStart + accountPageSize, accountRows.length);
      accountPageSize = next;
      const nextPage = Math.min(accountPage,
        Math.max(0, Math.ceil(accountRows.length / next) - 1));
      const nextStart = nextPage * next;
      const nextEnd = Math.min(nextStart + next, accountRows.length);
      if (nextPage !== accountPage || nextStart !== previousStart || nextEnd !== previousEnd) {
        renderAccountRows();
      }
    }
  });
}

function setupPagination() {
  if (paginationReady) return;
  paginationReady = true;
  byId('accounts-prev').addEventListener('click', () => {
    accountPage = Math.max(0, accountPage - 1);
    renderAccountRows();
  });
  byId('accounts-next').addEventListener('click', () => {
    accountPage += 1;
    renderAccountRows();
  });
  new ResizeObserver(queuePageLayout).observe(byId('accounts-list'));
  window.addEventListener('resize', queuePageLayout);
}

function accountStatus(account, observed) {
  if (observed?.routing?.paused || account.routing?.paused) {
    return statusPill('Paused', 'muted');
  }
  const status = observed?.authentication?.status || observed?.status
    || account.authentication?.status || 'unknown';
  const tone = ['active', 'usable'].includes(status) ? 'good'
    : status === 'legacy_read_only' ? 'muted' : 'unknown';
  return statusPill(status.replaceAll('_', ' '), tone);
}

function accountIdentity(account) {
  const identity = node('div', 'account-identity');
  const initial = (account.name || account.account_ref || '?').trim().charAt(0).toUpperCase();
  const avatar = node('span', `account-avatar is-${account.provider || 'other'}`, initial);
  avatar.setAttribute('aria-hidden', 'true');
  const names = node('div', 'account-identity-text');
  names.append(node('strong', 'account-name', account.name || account.account_ref),
    node('small', 'account-email', account.email || 'Email not observed'));
  identity.append(avatar, names);
  return identity;
}

function accountUsage(account, snapshot) {
  const windows = usageWindows(snapshot);
  const wrapper = node('div', 'account-table-usage');
  if (!windows.length) {
    wrapper.append(node('span', 'usage-unknown', 'Current usage unavailable'));
    wrapper.title = usageUnavailable(snapshot);
    if (account.provider !== 'codex') return wrapper;
  } else {
    for (const window of windows.slice(0, 2)) wrapper.append(usageWindowRow(window, { compact: true }));
  }
  const details = node('button', 'account-usage-open', windows.length > 2
    ? `View ${windows.length} windows` : windows.length ? 'Usage details' : 'Usage and resets');
  details.type = 'button';
  details.dataset.testid = 'account-usage-open';
  details.dataset.accountRef = account.account_ref;
  details.setAttribute('aria-label', `View usage for ${account.name || account.account_ref}`);
  details.addEventListener('click', () => openUsage(account, snapshot));
  wrapper.append(details);
  return wrapper;
}

function renderUsageDialog(account, snapshot) {
  const windows = usageWindows(snapshot);
  byId('account-usage-heading').textContent = `Usage for ${account.name || account.account_ref}`;
  byId('account-usage-subtitle').textContent = `${account.provider || 'Historical'} · Provider-reported quota windows`;
  const content = clear(byId('account-usage-content'));
  if (!windows.length) {
    const empty = node('div', 'usage-dialog-empty');
    empty.append(node('strong', '', 'Current usage unavailable'),
      node('p', '', usageUnavailable(snapshot)));
    content.append(empty);
    appendResetControl(content, account);
    return;
  }
  let help = 'Each bar represents a separate provider quota window. Percentages are not combined.';
  if (account.provider === 'codex') {
    help += ' Primary and secondary name window slots, not account rankings.';
    const namedPool = windows.map((window) =>
      /^(.+) · (?:primary|secondary)$/i.exec(window.display_label)?.[1]).find(Boolean);
    if (namedPool) help += ` ${namedPool} is a provider-reported quota group, not another account.`;
  }
  const section = node('section', 'usage-window-section');
  section.append(node('h3', '', `Current windows · ${windows.length}`));
  for (const window of windows) section.append(usageWindowRow(window));
  content.append(section);
  content.append(node('p', 'usage-dialog-note', help));
  if (snapshot?.refresh_reason) {
    content.append(node('p', 'account-detail-note',
      'The latest direct quota check did not complete. Showing only current readings.'));
  }
  appendResetControl(content, account);
}

function appendResetControl(content, account) {
  if (account.provider !== 'codex') {
    resetView?.dispose();
    resetView = null;
    return;
  }
  if (resetView?.accountRef !== account.account_ref) {
    resetView?.dispose();
    resetView = createAccountResetView(account, accountStatuses?.get(account.account_ref),
      () => accountsChanged());
  }
  content.append(resetView.element);
}

function openUsage(account, snapshot) {
  selectedUsageAccountRef = account.account_ref;
  renderUsageDialog(account, snapshot);
  byId('account-usage-dialog').showModal();
  byId('account-usage-close').focus();
  if (!usageWindows(snapshot).length && refreshUsage) refreshUsage(account.account_ref);
}

function ensureUsageDialog() {
  if (usageDialogReady) return;
  usageDialogReady = true;
  const dialog = byId('account-usage-dialog');
  byId('account-usage-close').addEventListener('click', () => dialog.close());
  byId('account-usage-done').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => {
    const ref = selectedUsageAccountRef;
    selectedUsageAccountRef = null;
    resetView?.dispose();
    resetView = null;
    const button = [...byId('accounts-list').querySelectorAll('[data-testid="account-usage-open"]')]
      .find((candidate) => candidate.dataset.accountRef === ref);
    if (button?.getClientRects().length) button.focus();
  });
}

function openModels(account, status) {
  selectedModelsAccountRef = account.account_ref;
  renderAccountModelsDialog(account, status);
  byId('account-models-dialog').showModal();
  byId('account-models-close').focus();
}

function ensureModelsDialog() {
  if (modelsDialogReady) return;
  modelsDialogReady = true;
  byId('account-models-dialog').addEventListener('close', () => {
    const ref = selectedModelsAccountRef;
    selectedModelsAccountRef = null;
    const button = [...byId('accounts-list').querySelectorAll('[data-testid="account-models-open"]')]
      .find((candidate) => candidate.dataset.accountRef === ref);
    if (button?.getClientRects().length) button.focus();
  });
}

function accountRow(account, snapshot, status, onRemove) {
  const row = node('tr', 'account-table-row');
  row.dataset.testid = 'account-row';
  const name = account.name || account.account_ref;
  const identity = node('th', 'account-table-identity');
  identity.scope = 'row';
  identity.append(accountIdentity(account));
  const provider = node('td', 'account-table-provider');
  provider.append(node('span', 'provider-name', account.provider || 'Historical'));
  const health = node('td', 'account-table-status');
  health.append(accountStatus(account, status));
  const usage = node('td', 'account-table-usage-cell');
  usage.append(accountUsage(account, snapshot));
  const models = node('td', 'account-table-models');
  const modelsButton = node('button', 'button button-secondary account-models-button', 'View models');
  modelsButton.type = 'button';
  modelsButton.dataset.testid = 'account-models-open';
  modelsButton.dataset.accountRef = account.account_ref;
  modelsButton.setAttribute('aria-label', `View models for ${name}`);
  modelsButton.append(node('span', 'model-count', accountModels(account, status).length));
  modelsButton.addEventListener('click', () => openModels(account, status));
  models.append(modelsButton);
  const actions = node('td', 'account-table-actions');
  const paused = status?.routing?.paused || account.routing?.paused;
  const toggle = node('button', 'account-pause-button', paused ? 'Resume' : 'Pause');
  toggle.type = 'button';
  toggle.dataset.testid = 'account-pause-toggle';
  toggle.setAttribute('aria-label', `${paused ? 'Resume' : 'Pause'} ${name}`);
  toggle.addEventListener('click', async () => {
    toggle.disabled = true;
    try {
      if (paused) await api.resumeAccount(account.account_ref);
      else await api.pauseAccount(account.account_ref);
      toast(`${name} ${paused ? 'resumed' : 'paused'} for new work.`);
      await accountsChanged();
    } catch (error) {
      toast(describeError(error), true);
      toggle.disabled = false;
    }
  });
  const remove = node('button', 'remove-button', 'Remove');
  remove.type = 'button';
  remove.dataset.testid = 'remove-account';
  remove.setAttribute('aria-label', `Remove ${name}`);
  remove.addEventListener('click', () => onRemove(account));
  actions.append(toggle, remove);
  row.append(identity, provider, health, usage, models, actions);
  return row;
}

function renderAccountRows() {
  const rows = accountRows;
  const list = clear(byId('accounts-list'));
  list.classList.toggle('is-empty', !rows.length);
  const pager = byId('accounts-pager');
  pager.hidden = !rows.length;
  const pages = Math.max(1, Math.ceil(rows.length / accountPageSize));
  accountPage = Math.min(accountPage, pages - 1);
  const start = accountPage * accountPageSize;
  const visible = rows.slice(start, start + accountPageSize);
  if (rows.length) {
    byId('accounts-page-label').textContent = `${start + 1}–${start + visible.length} of ${rows.length}`;
    byId('accounts-prev').disabled = accountPage === 0;
    byId('accounts-next').disabled = accountPage >= pages - 1;
  }
  if (!rows.length) {
    list.append(emptyState('No accounts connected',
      'Choose a provider and sign in to add your first account.'));
  } else {
    const table = node('table', 'account-table');
    const caption = node('caption', 'visually-hidden', 'Connected accounts and observed usage');
    const head = node('thead');
    const headers = node('tr');
    for (const label of ['Account', 'Provider', 'Status', 'Usage', 'Models', 'Actions']) {
      const header = node('th', '', label);
      header.scope = 'col';
      headers.append(header);
    }
    head.append(headers);
    const body = node('tbody');
    for (const account of visible) {
      body.append(accountRow(account, accountUsageRows.get(account.account_ref),
        accountStatuses?.get(account.account_ref), removeAccount));
    }
    table.append(caption, head, body);
    list.append(table);
  }
}

export function renderAccounts(accounts, usage, { statuses, onRemove, onChanged, onUsageRefresh }) {
  ensureModelsDialog();
  ensureUsageDialog();
  setupPagination();
  accountRows = Array.isArray(accounts) ? accounts : [];
  accountUsageRows = usage;
  accountStatuses = statuses;
  removeAccount = onRemove;
  accountsChanged = onChanged;
  refreshUsage = onUsageRefresh;
  const summary = clear(byId('accounts-summary'));
  summary.hidden = !accountRows.length;
  byId('accounts-removal-note').hidden = !accountRows.length;
  const connected = accountRows.filter((account) => {
    const observed = statuses?.get(account.account_ref);
    return ['usable', 'active'].includes(observed?.authentication?.status || observed?.status);
  }).length;
  summary.append(node('span', 'pulse-dot'));
  summary.append(node('span', '', `${accountRows.length} saved account${accountRows.length === 1 ? '' : 's'} · ${connected} with an active or usable observation. Quota windows show provider observations and their freshness.`));
  renderAccountRows();
  queuePageLayout();
  const dialog = byId('account-models-dialog');
  if (dialog.open && selectedModelsAccountRef) {
    const selected = accountRows.find((account) => account.account_ref === selectedModelsAccountRef);
    if (selected) renderAccountModelsDialog(selected, statuses?.get(selected.account_ref));
    else dialog.close();
  }
  const usageDialog = byId('account-usage-dialog');
  if (usageDialog.open && selectedUsageAccountRef) {
    const selected = accountRows.find((account) => account.account_ref === selectedUsageAccountRef);
    if (selected) renderUsageDialog(selected, usage?.get(selected.account_ref));
    else usageDialog.close();
  }
}

export function setupRemoval(onRemoved) {
  const dialog = byId('remove-dialog');
  const feedback = byId('remove-feedback');
  const confirm = byId('remove-confirm');
  let selected = null;
  byId('remove-cancel').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { selected = null; setFeedback(feedback, ''); });
  confirm.addEventListener('click', async () => {
    if (!selected) return;
    confirm.disabled = true;
    setFeedback(feedback, '');
    try {
      const result = await api.deleteAccount(selected.account_ref);
      dialog.close();
      toast(result?.upstream_credential_removed === false
        ? 'Account removed from AgentBridge. Its provider authorization was not revoked.'
        : 'Account removed from AgentBridge. Check the provider authorization separately.');
      await onRemoved();
    } catch (error) {
      setFeedback(feedback, describeError(error));
    } finally {
      confirm.disabled = false;
    }
  });
  return (account) => {
    selected = account;
    byId('remove-account-name').textContent = account.name || account.account_ref;
    setFeedback(feedback, '');
    dialog.showModal();
    confirm.focus();
  };
}
