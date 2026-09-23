import { api } from './api.js';
import { byId, clear, describeError, emptyState, formatTime, node, percent, pill, setFeedback, statusPill, toast } from './ui.js';
import { usageRow, usageSignal } from './usage-view.js';

function accountStatus(account, observed) {
  const status = observed?.authentication?.status || observed?.status
    || account.authentication?.status || 'unknown';
  const tone = ['active', 'usable'].includes(status) ? 'good'
    : status === 'legacy_read_only' ? 'muted' : 'unknown';
  return statusPill(status.replaceAll('_', ' '), tone);
}

function metadataFor(status, model) {
  const metadata = status?.model_metadata;
  if (Array.isArray(metadata)) return metadata.find((item) => item?.id === model) || {};
  return metadata && typeof metadata === 'object' ? metadata[model] || {} : {};
}

function modelDetail(model, status) {
  const row = node('div', 'account-detail-model');
  const top = node('div', 'account-detail-model-head');
  top.append(node('strong', '', model.id || 'Unknown model'));
  const used = percent(model.used_percent);
  const stamp = model.quota_observed_at ? new Date(model.quota_observed_at) : null;
  const fresh = used !== null && stamp && !Number.isNaN(stamp.getTime())
    && 0 <= Date.now() - stamp.getTime() && Date.now() - stamp.getTime() < 60000;
  top.append(node('span', '', fresh ? `${Math.round(used)}% used`
    : used === null ? 'Usage unknown' : 'Usage stale'));
  row.append(top);
  const facts = node('div', 'account-detail-facts');
  const cooldown = model.cooldown_until ? new Date(model.cooldown_until) : null;
  if (cooldown && !Number.isNaN(cooldown.getTime()) && cooldown.getTime() > Date.now()) {
    facts.append(node('span', 'detail-warning', `Cooldown until ${formatTime(model.cooldown_until)}`));
  } else {
    facts.append(node('span', '', status?.cooldown_known === true
      ? 'No current cooldown observed' : 'Cooldown unknown'));
  }
  facts.append(node('span', '', model.quota_observed_at
    ? `Quota observed ${formatTime(model.quota_observed_at)}` : 'Quota time unknown'));
  if (model.quota_scope) facts.append(node('span', '', `Quota scope: ${model.quota_scope}`));
  const metadata = metadataFor(status, model.id);
  const efforts = model.reasoning_efforts || metadata.reasoning_efforts;
  if (Array.isArray(efforts) && efforts.length) facts.append(node('span', '', `Effort: ${efforts.join(', ')}`));
  if (metadata.default_reasoning_effort) {
    facts.append(node('span', '', `Default effort: ${metadata.default_reasoning_effort}`));
  }
  const context = model.context_windows || metadata.context_windows;
  if (Array.isArray(context) && context.length) facts.append(node('span', '', `Context: ${context.map(String).join(', ')}`));
  if (Array.isArray(metadata.input_modalities) && metadata.input_modalities.length) {
    facts.append(node('span', '', `Input: ${metadata.input_modalities.join(', ')}`));
  }
  row.append(facts);
  return row;
}

function observationDetails(status) {
  const details = node('details', 'account-details');
  const summary = node('summary', '', 'Observed provider details');
  details.append(summary);
  const body = node('div', 'account-details-body');
  if (!status || status.error) {
    body.append(node('p', 'account-detail-note', status?.error || 'No account observation is available yet.'));
  } else {
    const info = node('div', 'account-detail-overview');
    const auth = status.authentication || {};
    const health = status.disabled === true ? 'Disabled'
      : status.unavailable === true ? 'Unavailable'
        : status.status === 'active' && status.binding_verified === true ? 'Active, binding verified'
          : status.status || auth.status || 'Unknown';
    info.append(node('span', '', `Health: ${health}`));
    info.append(node('span', '', `Source: ${auth.source || status.source || 'Unknown'}`));
    info.append(node('span', '', `Observed: ${formatTime(auth.observed_at || status.observed_at)}`));
    if (status.reason) info.append(node('span', '', `Reason: ${String(status.reason).replaceAll('_', ' ')}`));
    body.append(info);
    const models = Array.isArray(status.models) ? status.models : [];
    if (models.length) {
      const list = node('div', 'account-detail-models');
      for (const model of models) if (model && typeof model === 'object') {
        list.append(modelDetail(model, status));
      }
      body.append(list);
    } else {
      body.append(node('p', 'account-detail-note', 'No verified model details are available for this account.'));
    }
  }
  details.append(body);
  return details;
}

function accountCard(account, snapshot, status, onRemove) {
  const card = node('article', 'account-card');
  const head = node('div', 'account-heading');
  const identity = node('div', 'account-identity');
  const initial = (account.name || account.account_ref || '?').trim().charAt(0).toUpperCase();
  const avatar = node('span', `account-avatar is-${account.provider || 'other'}`, initial);
  const names = node('div');
  names.append(node('strong', 'account-name', account.name || account.account_ref),
    node('small', 'account-email', account.email || 'Email not observed'));
  identity.append(avatar, names);
  head.append(identity, accountStatus(account, status));
  const row = node('div', 'account-row');
  row.append(node('small', '', 'PROVIDER'), node('strong', 'provider-name', account.provider || 'Historical'));
  const modelList = node('div', 'account-models');
  const models = Array.isArray(account.supported_models) ? account.supported_models : [];
  for (const model of models.slice(0, 4)) modelList.append(pill(model));
  if (models.length > 4) modelList.append(pill(`+${models.length - 4} more`));
  if (!models.length) modelList.append(node('small', 'muted-label', 'No models observed'));
  const quota = usageRow('Highest observed usage', snapshot);
  const footer = node('div', 'account-footer');
  const signal = usageSignal(snapshot);
  footer.append(node('small', '', signal.fresh ? 'Fresh usage observation' : 'Quota is unknown or stale'));
  const remove = node('button', 'remove-button', 'Remove');
  remove.type = 'button';
  remove.dataset.testid = 'remove-account';
  remove.setAttribute('aria-label', `Remove ${account.name || account.account_ref}`);
  remove.addEventListener('click', () => onRemove(account));
  footer.append(remove);
  card.append(head, row, modelList, quota, observationDetails(status), footer);
  return card;
}

export function renderAccounts(accounts, usage, { statuses, onRemove }) {
  const rows = Array.isArray(accounts) ? accounts : [];
  const summary = clear(byId('accounts-summary'));
  summary.hidden = !rows.length;
  byId('accounts-removal-note').hidden = !rows.length;
  const connected = rows.filter((account) => {
    const observed = statuses?.get(account.account_ref);
    return ['usable', 'active'].includes(observed?.authentication?.status || observed?.status);
  }).length;
  summary.append(node('span', 'pulse-dot'));
  summary.append(node('span', '', `${rows.length} saved account${rows.length === 1 ? '' : 's'} · ${connected} with an active or usable observation. Usage is shown only when a fresh signal exists.`));
  const list = clear(byId('accounts-list'));
  if (!rows.length) {
    list.append(emptyState('No accounts connected',
      'Choose a provider and sign in to add your first account.'));
    return;
  }
  for (const account of rows) {
    list.append(accountCard(account, usage.get(account.account_ref),
      statuses?.get(account.account_ref), onRemove));
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
