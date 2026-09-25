// Conversation route choices and update payloads for the playground.

export function routingMode(instance) {
  return instance?.routing_mode === 'pinned' ? 'pinned' : 'automatic';
}

export function selectedAccount(instance) {
  if (!instance) return null;
  return routingMode(instance) === 'automatic'
    ? instance.affinity_account_ref || instance.account_ref || null
    : instance.account_ref || null;
}

export function routePending(instance, choice) {
  if (!instance) return false;
  if (choice.model !== instance.model || choice.routingMode !== routingMode(instance)) {
    return true;
  }
  if (choice.routingMode === 'pinned') {
    return (choice.account || selectedAccount(instance)) !== selectedAccount(instance);
  }
  return choice.provider !== (instance.routing_provider || '')
    || !!choice.account && choice.account !== selectedAccount(instance);
}

export function createRoute(choice) {
  const values = { model: choice.model, routing_mode: choice.routingMode };
  if (choice.account) values.account_ref = choice.account;
  if (choice.routingMode === 'automatic' && choice.provider) values.provider = choice.provider;
  return values;
}

export function updateRoute(instance, choice) {
  if (!routePending(instance, choice)) return null;
  const values = { expected_version: instance.version, model: choice.model };
  const previousMode = routingMode(instance);
  if (choice.routingMode === 'pinned') {
    if (previousMode !== 'pinned') values.routing_mode = 'pinned';
    if (choice.account && choice.account !== selectedAccount(instance)) {
      values.account_ref = choice.account;
    }
    return values;
  }
  if (previousMode !== 'automatic'
      || choice.account && choice.account !== selectedAccount(instance)) {
    values.routing_mode = 'automatic';
  }
  if (choice.account && choice.account !== selectedAccount(instance)) {
    values.account_ref = choice.account;
  }
  if (previousMode !== 'automatic'
      || choice.provider !== (instance.routing_provider || '')) {
    values.provider = choice.provider || null;
  }
  return values;
}

export function appliedRoute(instance) {
  if (!instance) return 'No route selected';
  if (routingMode(instance) === 'pinned') {
    return `Pinned to ${instance.account_ref || 'an unavailable account'}`;
  }
  const provider = instance.routing_provider || 'All providers';
  const account = selectedAccount(instance) || 'no account selected yet';
  return `Automatic · ${provider} · preferred: ${account}`;
}

export function requestedRoute(choice, instance) {
  const provider = choice.provider || 'All providers';
  const account = choice.account || (instance && choice.routingMode === 'pinned'
    ? selectedAccount(instance) : null);
  const target = account || (instance ? 'keep preferred account' : 'least-used eligible account');
  return `${choice.routingMode === 'pinned' ? 'Pinned' : 'Automatic'} · ${provider} · ${target}`;
}
