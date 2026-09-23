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
    const observed = Array.isArray(row.context_windows)
      ? row.context_windows.map(Number).filter((value) => Number.isSafeInteger(value) && value > 0) : [];
    if (!observed.length) return [];
    maxima.push(Math.max(...observed));
  }
  return maxima.length ? [String(Math.min(...maxima))] : [];
}

function selectedMetadata(model, provider, accountRef, accounts) {
  const eligible = eligibleAccounts(model, provider, accounts)
    .filter((item) => !accountRef || item.account_ref === accountRef);
  if (!eligible.length) return { efforts: [], windows: [] };
  const refs = new Set(eligible.map((item) => item.account_ref));
  if (!Array.isArray(model.account_capabilities)) {
    return !provider && !accountRef ? {
      efforts: Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts.map(String) : [],
      windows: Array.isArray(model.context_windows) ? model.context_windows.map(String) : [],
    } : { efforts: [], windows: [] };
  }
  const rows = model.account_capabilities.filter((item) => item.observed === true
    && refs.has(item.account_ref) && (!provider || item.provider === provider));
  if (rows.length !== eligible.length) return { efforts: [], windows: [] };
  return { efforts: commonValues(rows, 'reasoning_efforts'),
    windows: commonContextLimit(rows) };
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
  let locked = false;
  let lockedInstance = null;

  function populateProviders() {
    const previous = provider.value;
    const providers = new Set();
    for (const item of accounts) {
      if (item.provider && listModels(catalog).some((model) =>
        eligibleAccounts(model, item.provider, accounts).some((account) => account.account_ref === item.account_ref))) {
        providers.add(item.provider);
      }
    }
    clear(provider).append(option('', 'All providers'));
    for (const value of [...providers].sort()) provider.append(option(value, value));
    provider.value = [...providers].includes(previous) ? previous : '';
  }

  function populateModels() {
    const previous = model.value;
    const available = listModels(catalog).filter((item) =>
      eligibleAccounts(item, provider.value, accounts).length > 0);
    clear(model).append(option('', available.length ? 'Select a model' : 'No observed models'));
    for (const item of available) model.append(option(item.id, item.id));
    model.value = available.some((item) => item.id === previous) ? previous : '';
    return available;
  }

  function populateAccounts() {
    const previous = account.value;
    const selectedModel = listModels(catalog).find((item) => item.id === model.value);
    const eligible = eligibleAccounts(selectedModel, provider.value, accounts);
    clear(account).append(option('', provider.value
      ? `Automatic · ${provider.value} accounts` : 'Automatic · all providers'));
    for (const item of eligible) account.append(option(item.account_ref,
      `${item.name || item.account_ref} · ${item.provider || 'unknown provider'}`));
    account.value = eligible.some((item) => item.account_ref === previous) ? previous : '';
    return eligible;
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
    const previousContext = context.value;
    clear(context).append(option('', 'Default'));
    for (const value of metadata.windows) {
      const display = /^[0-9]+$/.test(value) ? `${Number(value).toLocaleString()} tokens` : value;
      context.append(option(value, display));
    }
    context.value = metadata.windows.includes(previousContext) ? previousContext : '';
    byId('context-field').hidden = !contextAllowed || !metadata.windows.length;
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
    const eligible = eligibleAccounts(selectedModel, provider.value, accounts).length;
    const scope = provider.value ? ` within ${provider.value}` : ' across providers';
    byId('route-help').textContent = selectedModel && eligible
      ? `Automatic routing selects an observed account${scope}. Effort and context controls appear only when the SDK advertises concrete values.`
      : 'Choose a model observed from a connected account.';
  }

  function sync() {
    populateModels();
    populateAccounts();
    populateParameters();
    const summary = [provider.value || 'All providers', model.value || 'Choose a model',
      account.value || 'Automatic'].join(' · ');
    byId('route-summary').textContent = summary;
    onChange?.();
  }

  provider.addEventListener('change', sync);
  model.addEventListener('change', sync);
  account.addEventListener('change', sync);

  return {
    update({ models, accountRows, capabilityData, workspacePath }) {
      catalog = models;
      accounts = Array.isArray(accountRows) ? accountRows : [];
      capabilities = capabilityData;
      if (!workspace.value && workspacePath) workspace.value = workspacePath;
      populateProviders();
      if (lockedInstance) this.lock(lockedInstance);
      else sync();
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
      const item = listModels(catalog).find((row) => row.id === choice.model);
      const eligible = eligibleAccounts(item, choice.provider, accounts);
      if (lockedInstance?.routing_mode === 'pinned') {
        return eligible.some((account) => account.account_ref === lockedInstance.account_ref);
      }
      return eligible.length > 0 && (!choice.account
        || eligible.some((account) => account.account_ref === choice.account));
    },
    lock(instance) {
      locked = !!instance;
      lockedInstance = instance || null;
      if (instance) {
        if (instance.routing_provider && ![...provider.options].some((item) => item.value === instance.routing_provider)) {
          provider.append(option(instance.routing_provider, `${instance.routing_provider} · unavailable`));
        }
        provider.value = instance.routing_provider || '';
        sync();
        if (instance.model && ![...model.options].some((item) => item.value === instance.model)) {
          model.append(option(instance.model, `${instance.model} · unavailable`));
        }
        model.value = instance.model || '';
        populateAccounts();
        if (instance.routing_mode === 'pinned' && instance.account_ref
            && ![...account.options].some((item) => item.value === instance.account_ref)) {
          account.append(option(instance.account_ref, `${instance.account_ref} · unavailable`));
        }
        account.value = instance.routing_mode === 'pinned' ? instance.account_ref || '' : '';
        workspace.value = instance.workspace_path || instance.cwd || workspace.value;
        populateParameters();
      }
      for (const field of [provider, model, account, workspace]) field.disabled = locked;
      byId('route-summary').textContent = instance
        ? `${instance.routing_provider || 'All providers'} · ${instance.model || 'Unknown model'} · ${instance.routing_mode === 'pinned' ? instance.account_ref : 'Automatic'}`
        : [provider.value || 'All providers', model.value || 'Choose a model', account.value || 'Automatic'].join(' · ');
      onChange?.();
    },
    reset() {
      for (const field of [provider, model, account, workspace]) field.disabled = false;
      locked = false;
      lockedInstance = null;
      provider.value = '';
      sync();
    },
    get locked() { return locked; },
  };
}
