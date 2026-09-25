import { byId, clear, node } from './ui.js';
import { requestedRoute, routePending, routingMode, selectedAccount, updateRoute } from './chat-routing.js';

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

function providerName(value) {
  return { codex: 'Codex', claude: 'Claude', grok: 'Grok' }[value] || value;
}

function commonValues(rows, key) {
  if (!rows.length) return [];
  const lists = rows.map((row) => Array.isArray(row[key]) ? row[key].map(String) : []);
  return lists.reduce((shared, values) => shared.filter((value) => values.includes(value)));
}

function observedWindows(row) {
  return Array.isArray(row?.context_windows)
    ? row.context_windows.filter((value) => Number.isSafeInteger(value) && value > 0) : [];
}

function contextOptions(rows) {
  if (!rows.length) return [];
  const maxima = rows.map((row) => {
    const windows = observedWindows(row);
    const reported = row?.max_context_window;
    return Number.isSafeInteger(reported) && reported > 0
      ? reported : windows.length ? Math.max(...windows) : null;
  });
  if (maxima.includes(null)) return [];
  const ceiling = Math.min(...maxima);
  return [...new Set(rows.flatMap(observedWindows))]
    .filter((value) => value <= ceiling).sort((left, right) => left - right);
}

function commonDefaultWindow(rows) {
  const defaults = rows.map((row) => row.default_context_window);
  return defaults.length && Number.isSafeInteger(defaults[0]) && defaults[0] > 0
    && defaults.every((value) => value === defaults[0]) ? defaults[0] : null;
}

function selectedMetadata(model, provider, accountRef, accounts) {
  const eligible = eligibleAccounts(model, provider, accounts)
    .filter((item) => !accountRef || item.account_ref === accountRef);
  if (!eligible.length) return { efforts: [], windows: [], defaultWindow: null };
  const refs = new Set(eligible.map((item) => item.account_ref));
  if (!Array.isArray(model.account_capabilities)) {
    return !provider && !accountRef ? {
      efforts: Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts.map(String) : [],
      windows: contextOptions([model]), defaultWindow: model.default_context_window || null,
    } : { efforts: [], windows: [], defaultWindow: null };
  }
  const rows = model.account_capabilities.filter((item) => item.observed === true
    && refs.has(item.account_ref) && (!provider || item.provider === provider));
  if (rows.length !== eligible.length) return { efforts: [], windows: [], defaultWindow: null };
  return { efforts: commonValues(rows, 'reasoning_efforts'),
    windows: contextOptions(rows), defaultWindow: commonDefaultWindow(rows) };
}

function validContext(value, windows) {
  return value === '' || windows.some((window) => String(window) === value);
}

export function setupChatCatalog(onChange) {
  const provider = byId('chat-provider');
  const model = byId('chat-model');
  const account = byId('chat-account');
  const mode = byId('chat-routing-mode');
  const workspace = byId('chat-workspace');
  const effort = byId('chat-effort');
  const context = byId('chat-context');
  const permission = byId('chat-permission');
  let catalog = null;
  let accounts = [];
  let capabilities = null;
  let lockedInstance = null;
  let busy = false;
  let availableWindows = [];

  function hasObservedModel(reference) {
    return listModels(catalog).some((item) => observed(item)
      && item.observed_account_refs.includes(reference));
  }

  function currentEligible() {
    const reference = selectedAccount(lockedInstance);
    const selectedModel = listModels(catalog).find((item) => item.id === model.value);
    return !!reference && eligibleAccounts(selectedModel, provider.value, accounts)
      .some((item) => item.account_ref === reference);
  }

  function populateProviders() {
    const previous = provider.value;
    const providers = new Set(accounts.map((item) => item.provider).filter(Boolean));
    clear(provider).append(option('', 'All providers'));
    for (const value of [...providers].sort()) {
      const available = accounts.some((item) => item.provider === value
        && hasObservedModel(item.account_ref));
      provider.append(option(value, available
        ? providerName(value) : `${providerName(value)} · unavailable`));
    }
    if (previous && !providers.has(previous)) provider.append(option(previous, `${previous} · unavailable`));
    provider.value = previous;
  }

  function populateModels() {
    const previous = model.value;
    const available = listModels(catalog).filter((item) =>
      eligibleAccounts(item, provider.value, accounts).some((row) =>
        !account.value || row.account_ref === account.value));
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
    const matching = accounts.filter((item) => !provider.value || item.provider === provider.value);
    const currentAccount = selectedAccount(lockedInstance) || 'none yet';
    const emptyLabel = mode.value === 'pinned'
      ? lockedInstance && currentEligible() ? `Use current account · ${currentAccount}`
        : 'Select an account'
      : lockedInstance && currentEligible() ? `Keep preferred account · ${currentAccount}`
        : 'Start with least-used eligible account';
    clear(account).append(option('', emptyLabel));
    for (const item of matching) {
      const available = hasObservedModel(item.account_ref);
      const label = `${item.name || item.account_ref} · ${providerName(item.provider || 'unknown provider')}`;
      const choice = option(item.account_ref, available ? label : `${label} · unavailable`);
      choice.disabled = !available;
      account.append(choice);
    }
    if (previous && !matching.some((item) => item.account_ref === previous)) {
      const missing = option(previous, `${previous} · unavailable`);
      missing.disabled = true;
      account.append(missing);
    }
    account.value = [...account.options].some((item) => item.value === previous) ? previous : '';
  }

  function populateParameters() {
    const selectedModel = listModels(catalog).find((item) => item.id === model.value);
    const metadata = selectedMetadata(selectedModel, provider.value,
      mode.value === 'pinned' ? account.value || lockedInstance?.account_ref : null, accounts);
    const effortSupport = capabilities?.parameters?.effort?.support;
    const contextSupport = capabilities?.parameters?.context_window?.support;
    const effortAllowed = !!effortSupport && effortSupport !== 'unsupported';
    const contextAllowed = !!contextSupport && contextSupport !== 'unsupported';
    const previousEffort = effort.value;
    clear(effort).append(option('', 'Default'));
    for (const value of metadata.efforts) effort.append(option(value, value));
    effort.value = metadata.efforts.includes(previousEffort) ? previousEffort : '';
    byId('effort-field').hidden = !effortAllowed || !metadata.efforts.length;
    availableWindows = contextAllowed ? metadata.windows : [];
    const previousContext = context.value;
    const defaultLabel = metadata.defaultWindow
      ? `Provider default · ${metadata.defaultWindow.toLocaleString()} tokens`
      : 'Provider default';
    clear(context).append(option('', defaultLabel));
    for (const window of availableWindows) {
      context.append(option(String(window), `${window.toLocaleString()} tokens`));
    }
    context.value = validContext(previousContext, availableWindows) ? previousContext : '';
    byId('context-field').hidden = !availableWindows.length;
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
    const selectedRef = mode.value === 'pinned'
      ? account.value || selectedAccount(lockedInstance) : account.value;
    const route = mode.value === 'pinned'
      ? `Pinned routing uses ${selectedRef || 'the selected account'}.`
      : account.value ? `Automatic routing starts with ${account.value}.`
        : currentEligible() ? `Automatic routing keeps ${selectedAccount(lockedInstance)}.`
          : 'Automatic routing starts with the least-used eligible account.';
    byId('route-help').textContent = selectedRef
      && !eligible.some((item) => item.account_ref === selectedRef)
      ? 'This account has not observed the selected model. Choose another account or model.'
      : selectedModel && eligible.length
      ? `${route}${availableWindows.length ? ' Context sizes come from the observed model catalog; the provider may reject an override.' : ''}`
      : 'Choose a model observed from a connected account.';
  }

  function hasPendingRoute() {
    return routePending(lockedInstance, selected());
  }

  function updateSummary() {
    const choice = selected();
    const route = requestedRoute(choice, lockedInstance);
    byId('route-summary').textContent = `${lockedInstance && hasPendingRoute() ? 'Pending · ' : ''}${choice.model || 'Choose a model'} · ${route}`;
  }

  function applyDisabled() {
    for (const field of [provider, model, mode, account, effort, context, permission]) {
      field.disabled = busy;
    }
    const accountHelp = byId('chat-account-help');
    if (mode.value === 'automatic') {
      accountHelp.textContent = 'Stays on the preferred account until confirmed exhaustion; temporary limits wait for reset.';
    } else if (lockedInstance) {
      accountHelp.textContent = 'Uses the chosen account for each turn. Leave Account unchanged to pin the current account.';
    } else {
      accountHelp.textContent = 'Choose the account to use for this conversation.';
    }
    workspace.disabled = busy || !!lockedInstance;
    workspace.title = lockedInstance ? 'Workspace is fixed for this conversation.' : '';
  }

  function sync() {
    populateAccounts();
    populateModels();
    populateParameters();
    updateSummary();
    applyDisabled();
    onChange?.();
  }

  provider.addEventListener('change', () => {
    if (account.value && (provider.value && accounts.find((item) =>
      item.account_ref === account.value)?.provider !== provider.value)) {
      account.value = '';
    }
    sync();
  });
  model.addEventListener('change', sync);
  account.addEventListener('change', () => {
    if (account.value && !provider.value) {
      provider.value = accounts.find((item) => item.account_ref === account.value)?.provider || '';
    }
    sync();
  });
  mode.addEventListener('change', sync);
  context.addEventListener('change', () => onChange?.());

  function selected() {
    return { provider: provider.value, model: model.value, account: account.value,
      routingMode: mode.value, preferredEligible: currentEligible(),
      workspace: workspace.value.trim(),
      effort: byId('effort-field').hidden ? '' : effort.value,
      context: byId('context-field').hidden ? '' : context.value,
      permission: byId('permission-field').hidden ? '' : permission.value };
  }

  return {
    update({ models, accountRows, capabilityData, workspacePath }) {
      catalog = models;
      accounts = Array.isArray(accountRows) ? accountRows : [];
      capabilities = capabilityData;
      if (!workspace.value && workspacePath) workspace.value = workspacePath;
      populateProviders();
      sync();
    },
    selected,
    canSend() {
      const choice = selected();
      if (!validContext(choice.context, availableWindows)) return false;
      const item = listModels(catalog).find((row) => row.id === choice.model);
      const eligible = eligibleAccounts(item, choice.provider, accounts);
      const requiredAccount = choice.account || (choice.routingMode === 'pinned'
        ? selectedAccount(lockedInstance) : null);
      if (choice.routingMode === 'pinned' && !requiredAccount) return false;
      return eligible.length > 0 && (!choice.account
        || eligible.some((row) => row.account_ref === choice.account))
        && (!requiredAccount || eligible.some((row) => row.account_ref === requiredAccount));
    },
    hasPendingRoute,
    updatePayload() {
      return updateRoute(lockedInstance, selected());
    },
    lock(instance) {
      lockedInstance = instance || null;
      if (!instance) { sync(); return; }
      const routeProvider = routingMode(instance) === 'pinned'
        ? accounts.find((item) => item.account_ref === instance.account_ref)?.provider || ''
        : instance.routing_provider || '';
      populateProviders();
      if (routeProvider && ![...provider.options].some((item) => item.value === routeProvider)) {
        provider.append(option(routeProvider, `${routeProvider} · unavailable`));
      }
      provider.value = routeProvider;
      mode.value = routingMode(instance);
      populateAccounts();
      account.value = routingMode(instance) === 'pinned' ? instance.account_ref || '' : '';
      populateModels();
      if (instance.model && ![...model.options].some((item) => item.value === instance.model)) {
        model.append(option(instance.model, `${instance.model} · unavailable`));
      }
      model.value = instance.model || '';
      workspace.value = instance.workspace_path || instance.cwd || workspace.value;
      populateParameters();
      updateSummary();
      applyDisabled();
      onChange?.();
    },
    refreshInstance(instance) {
      if (!hasPendingRoute()) {
        this.lock(instance);
        return;
      }
      lockedInstance = instance;
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
      mode.value = 'automatic';
      account.value = '';
      model.value = '';
      sync();
    },
  };
}
