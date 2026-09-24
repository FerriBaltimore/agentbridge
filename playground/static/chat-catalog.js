import { byId, clear, node } from './ui.js';

function listModels(catalog) {
  return Array.isArray(catalog?.items) ? catalog.items
    : Array.isArray(catalog?.models) ? catalog.models : [];
}

function observed(model) {
  return model?.availability === 'proxy_observed'
    && Array.isArray(model.observed_account_refs) && model.observed_account_refs.length > 0;
}

function eligibleAccounts(model, provider, accounts) {
  if (!observed(model)) return [];
  const refs = new Set(model.observed_account_refs);
  return accounts.filter((account) => refs.has(account.account_ref)
    && (!provider || account.provider === provider));
}

function option(value, label) {
  const item = node('option', '', label);
  item.value = value;
  return item;
}

function commonValues(rows, key) {
  if (!rows.length) return [];
  const lists = rows.map((row) => Array.isArray(row[key]) ? row[key].map(String) : []);
  return lists.reduce((shared, values) => shared.filter((value) => values.includes(value)));
}

function commonContextLimit(rows) {
  const maxima = [];
  for (const row of rows) {
    const values = Array.isArray(row.context_windows)
      ? row.context_windows.map(Number).filter((value) => Number.isSafeInteger(value) && value > 0) : [];
    if (!values.length) return null;
    maxima.push(Math.max(...values));
  }
  return maxima.length ? Math.min(10_000_000, ...maxima) : null;
}

function selectedMetadata(model, provider, accountRef, accounts) {
  const eligible = eligibleAccounts(model, provider, accounts)
    .filter((item) => !accountRef || item.account_ref === accountRef);
  if (!eligible.length) return { efforts: [], contextLimit: null };
  const refs = new Set(eligible.map((item) => item.account_ref));
  if (!Array.isArray(model.account_capabilities)) {
    return !provider && !accountRef ? {
      efforts: Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts.map(String) : [],
      contextLimit: commonContextLimit([model]),
    } : { efforts: [], contextLimit: null };
  }
  const rows = model.account_capabilities.filter((item) => item.observed === true
    && refs.has(item.account_ref) && (!provider || item.provider === provider));
  if (rows.length !== eligible.length) return { efforts: [], contextLimit: null };
  return { efforts: commonValues(rows, 'reasoning_efforts'),
    contextLimit: commonContextLimit(rows) };
}

function validContext(value, limit) {
  return value === '' || (limit !== null && /^[0-9]+$/.test(value)
    && Number.isSafeInteger(Number(value)) && Number(value) > 0 && Number(value) <= limit);
}

export function setupChatCatalog(onChange) {
  const provider = byId('chat-provider');
  const model = byId('chat-model');
  const account = byId('chat-account');
  const workspace = byId('chat-workspace');
  const effort = byId('chat-effort');
  const context = byId('chat-context');
  const permission = byId('chat-permission');
  let catalog = null;
  let accounts = [];
  let capabilities = null;
  let lockedInstance = null;
  let busy = false;
  let contextLimit = null;

  function pinnedProvider() {
    return accounts.find((item) => item.account_ref === lockedInstance?.account_ref)?.provider || '';
  }

  function populateProviders() {
    const previous = provider.value;
    const providers = new Set();
    for (const item of accounts) {
      if (item.provider && listModels(catalog).some((model) =>
        eligibleAccounts(model, item.provider, accounts).some((row) => row.account_ref === item.account_ref))) {
        providers.add(item.provider);
      }
    }
    clear(provider).append(option('', 'All providers'));
    for (const value of [...providers].sort()) provider.append(option(value, value));
    if (previous && !providers.has(previous)) provider.append(option(previous, `${previous} · unavailable`));
    provider.value = previous;
  }

  function populateModels() {
    const previous = model.value;
    const available = listModels(catalog).filter((item) =>
      eligibleAccounts(item, provider.value, accounts).length > 0);
    clear(model).append(option('', available.length ? 'Select a model' : 'No observed models'));
    for (const item of available) model.append(option(item.id, item.id));
    if (previous && !available.some((item) => item.id === previous)
        && lockedInstance?.model === previous) {
      model.append(option(previous, `${previous} · unavailable`));
    }
    model.value = [...model.options].some((item) => item.value === previous) ? previous : '';
  }

  function populateAccounts() {
    const previous = account.value;
    const selectedModel = listModels(catalog).find((item) => item.id === model.value);
    const eligible = eligibleAccounts(selectedModel, provider.value, accounts);
    clear(account).append(option('', provider.value
      ? `Automatic · ${provider.value} accounts` : 'Automatic · all providers'));
    if (lockedInstance?.routing_mode === 'automatic') {
      account.value = '';
      return;
    }
    if (lockedInstance?.routing_mode === 'pinned') {
      const original = lockedInstance.account_ref;
      const label = eligible.some((item) => item.account_ref === original)
        ? `Pinned · ${original}` : `Pinned · ${original} · unavailable`;
      account.append(option(original, label));
      account.value = previous === original ? original : '';
      return;
    }
    for (const item of eligible) account.append(option(item.account_ref,
      `${item.name || item.account_ref} · ${item.provider || 'unknown provider'}`));
    account.value = eligible.some((item) => item.account_ref === previous) ? previous : '';
  }

  function populateParameters() {
    const selectedModel = listModels(catalog).find((item) => item.id === model.value);
    const metadata = selectedMetadata(selectedModel, provider.value, account.value, accounts);
    const effortSupport = capabilities?.parameters?.effort?.support;
    const contextSupport = capabilities?.parameters?.context_window?.support;
    const effortAllowed = !!effortSupport && effortSupport !== 'unsupported';
    const contextAllowed = !!contextSupport && contextSupport !== 'unsupported';
    const previousEffort = effort.value;
    clear(effort).append(option('', 'Default'));
    for (const value of metadata.efforts) effort.append(option(value, value));
    effort.value = metadata.efforts.includes(previousEffort) ? previousEffort : '';
    byId('effort-field').hidden = !effortAllowed || !metadata.efforts.length;
    contextLimit = contextAllowed ? metadata.contextLimit : null;
    const previousContext = context.value;
    context.max = contextLimit === null ? '' : String(contextLimit);
    context.placeholder = contextLimit === null ? 'Default' : `Default · max ${contextLimit.toLocaleString()}`;
    context.value = validContext(previousContext, contextLimit) ? previousContext : '';
    byId('context-field').hidden = contextLimit === null;
    const permissionParameter = capabilities?.parameters?.permission_mode;
    const permissionValues = Array.isArray(permissionParameter?.values)
      ? permissionParameter.values.filter((item) => item && typeof item.value === 'string') : [];
    const previousPermission = permission.value;
    clear(permission);
    for (const item of permissionValues) {
      permission.append(option(item.value, item.display_name || item.value));
    }
    permission.value = permissionValues.some((item) => item.value === previousPermission)
      ? previousPermission : permissionValues[0]?.value || '';
    byId('permission-field').hidden = permissionParameter?.support !== 'adapter'
      || !permissionValues.length;
    const eligible = eligibleAccounts(selectedModel, provider.value, accounts);
    const route = account.value
      ? `This conversation uses ${account.value}.`
      : `Automatic routing uses an observed account${provider.value ? ` within ${provider.value}` : ''}.`;
    byId('route-help').textContent = account.value
      && !eligible.some((item) => item.account_ref === account.value)
      ? 'This account has not observed the selected model. Choose Automatic routing or another model.'
      : selectedModel && eligible.length
      ? `${route}${contextLimit === null ? '' : ` Context accepts a positive integer up to ${contextLimit.toLocaleString()} tokens; the provider may reject the override.`}`
      : 'Choose a model observed from a connected account.';
  }

  function hasPendingRoute() {
    if (!lockedInstance) return false;
    if (lockedInstance.routing_mode === 'pinned') {
      return account.value !== lockedInstance.account_ref || model.value !== lockedInstance.model;
    }
    return model.value !== lockedInstance.model
      || provider.value !== (lockedInstance.routing_provider || '');
  }

  function updateSummary() {
    const parts = [provider.value || 'All providers', model.value || 'Choose a model',
      account.value || 'Automatic'];
    byId('route-summary').textContent = `${lockedInstance && hasPendingRoute() ? 'Pending · ' : ''}${parts.join(' · ')}`;
  }

  function applyDisabled() {
    for (const field of [provider, model, effort, context, permission]) field.disabled = busy;
    account.disabled = busy || lockedInstance?.routing_mode === 'automatic';
    account.title = lockedInstance?.routing_mode === 'automatic'
      ? 'Create a new conversation to pin a specific account.' : '';
    workspace.disabled = busy || !!lockedInstance;
    workspace.title = lockedInstance ? 'Workspace is fixed for this conversation.' : '';
  }

  function sync() {
    populateModels();
    populateAccounts();
    populateParameters();
    updateSummary();
    applyDisabled();
    onChange?.();
  }

  provider.addEventListener('change', () => {
    if (lockedInstance?.routing_mode === 'pinned' && provider.value !== pinnedProvider()) {
      account.value = '';
    }
    sync();
  });
  model.addEventListener('change', sync);
  account.addEventListener('change', () => { populateParameters(); updateSummary(); onChange?.(); });
  context.addEventListener('input', () => onChange?.());

  return {
    update({ models, accountRows, capabilityData, workspacePath }) {
      catalog = models;
      accounts = Array.isArray(accountRows) ? accountRows : [];
      capabilities = capabilityData;
      if (!workspace.value && workspacePath) workspace.value = workspacePath;
      populateProviders();
      sync();
    },
    selected() {
      return { provider: provider.value, model: model.value, account: account.value,
        workspace: workspace.value.trim(),
        effort: byId('effort-field').hidden ? '' : effort.value,
        context: byId('context-field').hidden ? '' : context.value,
        permission: byId('permission-field').hidden ? '' : permission.value };
    },
    canSend() {
      const choice = this.selected();
      if (!validContext(choice.context, contextLimit) || !context.validity.valid) return false;
      const item = listModels(catalog).find((row) => row.id === choice.model);
      const eligible = eligibleAccounts(item, choice.provider, accounts);
      if (lockedInstance?.routing_mode === 'automatic' && choice.account) return false;
      if (lockedInstance?.routing_mode === 'pinned' && choice.account
          && choice.account !== lockedInstance.account_ref) return false;
      return eligible.length > 0 && (!choice.account
        || eligible.some((row) => row.account_ref === choice.account));
    },
    hasPendingRoute,
    updatePayload() {
      if (!hasPendingRoute()) return null;
      const choice = this.selected();
      const values = { expected_version: lockedInstance.version, model: choice.model };
      if (lockedInstance.routing_mode !== 'pinned' || choice.account !== lockedInstance.account_ref) {
        values.provider = choice.provider || null;
      }
      return values;
    },
    lock(instance) {
      lockedInstance = instance || null;
      if (!instance) { sync(); return; }
      const routeProvider = instance.routing_mode === 'pinned'
        ? pinnedProvider() : instance.routing_provider || '';
      populateProviders();
      if (routeProvider && ![...provider.options].some((item) => item.value === routeProvider)) {
        provider.append(option(routeProvider, `${routeProvider} · unavailable`));
      }
      provider.value = routeProvider;
      populateModels();
      if (instance.model && ![...model.options].some((item) => item.value === instance.model)) {
        model.append(option(instance.model, `${instance.model} · unavailable`));
      }
      model.value = instance.model || '';
      populateAccounts();
      account.value = instance.routing_mode === 'pinned' ? instance.account_ref || '' : '';
      workspace.value = instance.workspace_path || instance.cwd || workspace.value;
      populateParameters();
      updateSummary();
      applyDisabled();
      onChange?.();
    },
    refreshInstance(instance) {
      lockedInstance = instance;
      if (instance.routing_mode === 'automatic') account.value = '';
      populateAccounts();
      populateParameters();
      updateSummary();
      applyDisabled();
      onChange?.();
    },
    setBusy(value) { busy = value; applyDisabled(); },
    reset() {
      lockedInstance = null;
      provider.value = '';
      account.value = '';
      model.value = '';
      sync();
    },
  };
}
