import { byId, clear, formatTime, node } from './ui.js';
import { observedPercent, usageValue } from './usage-view.js';

function metadataFor(status, modelId) {
  const metadata = status?.model_metadata;
  if (Array.isArray(metadata)) return metadata.find((item) => item?.id === modelId) || {};
  return metadata && typeof metadata === 'object' ? metadata[modelId] || {} : {};
}

export function accountModels(account, status) {
  const observed = Array.isArray(status?.models) ? status.models.filter((item) => item?.id) : [];
  const ids = new Set(Array.isArray(account.supported_models) ? account.supported_models : []);
  for (const model of observed) ids.add(model.id);
  return [...ids].sort((left, right) => left.localeCompare(right)).map((id) => ({
    id, observed: observed.find((model) => model.id === id) || null,
  }));
}

function modelUsage(model) {
  const value = observedPercent(model?.used_percent);
  return value === null ? 'Usage unknown' : `${usageValue({ used_percent: value, stale: false })} · observed`;
}

function modelDetail(entry, status) {
  const row = node('article', 'account-detail-model');
  const top = node('div', 'account-detail-model-head');
  top.append(node('strong', '', entry.id), node('span', '', modelUsage(entry.observed)));
  row.append(top);
  const facts = node('div', 'account-detail-facts');
  if (!entry.observed) {
    facts.append(node('span', '', 'Saved model; current provider details are unavailable.'));
  } else {
    const cooldown = entry.observed.cooldown_until ? new Date(entry.observed.cooldown_until) : null;
    facts.append(node('span', cooldown && cooldown.getTime() > Date.now() ? 'detail-warning' : '',
      cooldown && cooldown.getTime() > Date.now()
        ? `Cooldown until ${formatTime(entry.observed.cooldown_until)}`
        : status?.cooldown_known === true ? 'No current cooldown observed' : 'Cooldown unknown'));
    facts.append(node('span', '', entry.observed.quota_observed_at
      ? `Quota observed ${formatTime(entry.observed.quota_observed_at)}` : 'Quota time unknown'));
    if (entry.observed.quota_scope) facts.append(node('span', '', `Quota scope: ${entry.observed.quota_scope}`));
  }
  const metadata = metadataFor(status, entry.id);
  const efforts = entry.observed?.reasoning_efforts || metadata.reasoning_efforts;
  if (Array.isArray(efforts) && efforts.length) facts.append(node('span', '', `Effort: ${efforts.join(', ')}`));
  if (metadata.default_reasoning_effort) {
    facts.append(node('span', '', `Default effort: ${metadata.default_reasoning_effort}`));
  }
  const context = entry.observed?.context_windows || metadata.context_windows;
  if (Array.isArray(context) && context.length) facts.append(node('span', '', `Context: ${context.map(String).join(', ')}`));
  if (Array.isArray(metadata.input_modalities) && metadata.input_modalities.length) {
    facts.append(node('span', '', `Input: ${metadata.input_modalities.join(', ')}`));
  }
  row.append(facts);
  return row;
}

function observationSummary(status) {
  const info = node('div', 'account-detail-overview');
  if (!status || status.error) {
    info.append(node('span', '', 'Provider observation unavailable'));
    return info;
  }
  const auth = status.authentication || {};
  const health = status.disabled === true ? 'Disabled'
    : status.unavailable === true ? 'Unavailable'
      : status.status === 'active' && status.binding_verified === true ? 'Active, binding verified'
        : status.status || auth.status || 'Unknown';
  info.append(node('span', '', `Health: ${health}`));
  info.append(node('span', '', `Source: ${auth.source || status.source || 'Unknown'}`));
  info.append(node('span', '', `Observed: ${formatTime(auth.observed_at || status.observed_at)}`));
  if (status.reason) info.append(node('span', '', `Reason: ${String(status.reason).replaceAll('_', ' ')}`));
  return info;
}

export function renderAccountModelsDialog(account, status) {
  byId('account-models-heading').textContent = `Models for ${account.name || account.account_ref}`;
  byId('account-models-subtitle').textContent = `${account.provider || 'Historical'} account · ${account.email || 'Email not observed'}`;
  const body = clear(byId('account-models-content'));
  body.append(observationSummary(status));
  const models = accountModels(account, status);
  if (!models.length) {
    body.append(node('p', 'account-detail-note', 'No models are available for this account.'));
    return;
  }
  if (!Array.isArray(status?.models) || !status.models.length) {
    body.append(node('p', 'account-detail-note', 'Saved model IDs are shown below. Current provider model details are unavailable.'));
  }
  const list = node('div', 'account-detail-models');
  for (const model of models) list.append(modelDetail(model, status));
  body.append(list);
}
