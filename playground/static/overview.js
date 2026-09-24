import { byId, clear, emptyState, node, pill, statusPill } from './ui.js';
import { usageRow, usageSignal } from './usage-view.js';

function modelCard(model) {
  const observed = model.availability === 'proxy_observed'
    && Array.isArray(model.observed_account_refs) && model.observed_account_refs.length > 0;
  const card = node('article', observed ? 'model-card' : 'model-card is-unverified');
  const top = node('div', 'model-top');
  const title = node('strong', 'model-id', model.id || 'Unknown model');
  title.title = model.id || '';
  top.append(title, statusPill(observed ? 'Observed' : 'Unverified', observed ? 'good' : 'unknown'));
  const meta = node('div', 'model-meta');
  for (const provider of Array.isArray(model.providers) ? model.providers : []) meta.append(pill(provider));
  const count = observed ? model.observed_account_refs.length
    : Array.isArray(model.candidate_account_refs) ? model.candidate_account_refs.length : 0;
  card.append(top, meta, node('p', '', `${count} ${observed ? 'observed' : 'configured'} account route${count === 1 ? '' : 's'}`));
  return card;
}

export function renderOverview({ accounts, models, instances, usage, onChat }) {
  const accountRows = Array.isArray(accounts) ? accounts : [];
  const modelRows = Array.isArray(models?.items) ? models.items
    : Array.isArray(models?.models) ? models.models : [];
  const conversations = Array.isArray(instances) ? instances : [];
  const observed = modelRows.filter((model) => model.availability === 'proxy_observed'
    && model.observed_account_refs?.length);
  const freshUsage = accountRows.filter((account) => usageSignal(usage.get(account.account_ref)).fresh);
  const accountRefs = new Set(accountRows.map((account) => account.account_ref));
  const canChat = observed.some((model) => model.observed_account_refs
    .some((ref) => accountRefs.has(ref)));
  byId('overview-open-chat').hidden = !canChat;
  byId('overview-account-action').textContent = accountRows.length
    ? 'Manage accounts' : 'Connect an account';
  byId('metric-accounts').textContent = String(accountRows.length);
  byId('metric-accounts-detail').textContent = accountRows.length === 1 ? 'One local route' : 'Dedicated local routes';
  byId('metric-models').textContent = String(observed.length);
  byId('metric-models-detail').textContent = `${modelRows.length} configured in total`;
  byId('metric-usage').textContent = String(freshUsage.length);
  byId('metric-usage-detail').textContent = `of ${accountRows.length} accounts with fresh usage`;
  byId('metric-instances').textContent = String(conversations.length);
  byId('metric-instances-detail').textContent = 'Saved AgentBridge instances';

  const modelContainer = clear(byId('overview-models'));
  if (!modelRows.length) {
    modelContainer.append(emptyState('No models yet',
      accountRows.length ? 'No model IDs have been observed from connected accounts.'
        : 'Connect an account to observe its exact model IDs.'));
  } else {
    for (const model of modelRows.slice(0, 6)) modelContainer.append(modelCard(model));
    if (modelRows.length > 6) {
      const more = node('button', 'model-more', `Browse ${modelRows.length - 6} more models in Chat`);
      more.type = 'button';
      more.addEventListener('click', onChat);
      modelContainer.append(more);
    }
  }

  const usageContainer = clear(byId('overview-usage'));
  if (!accountRows.length) {
    usageContainer.append(emptyState('No account observations',
      'Connect an account to inspect its observed quota windows.'));
  } else {
    for (const account of accountRows.slice(0, 5)) {
      usageContainer.append(usageRow(account.name || account.account_ref,
        usage.get(account.account_ref)));
    }
  }
}
