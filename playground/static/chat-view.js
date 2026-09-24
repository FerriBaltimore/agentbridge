import { byId, clear, emptyState, formatClock, node } from './ui.js';

const emptyConversation = byId('chat-messages')?.firstElementChild?.cloneNode(true);

export function renderConversations(instances, selectedId, activeTurn, onSelect) {
  const rows = Array.isArray(instances) ? [...instances] : [];
  rows.sort((a, b) => (b.created_at || b.created || 0) - (a.created_at || a.created || 0));
  byId('conversation-count').textContent = String(rows.length);
  const list = clear(byId('conversation-list'));
  if (!rows.length) {
    list.append(node('p', 'conversation-empty', 'Your conversations will appear here.'));
    return;
  }
  for (const instance of rows) {
    const id = instance.instance_id || instance.id;
    const button = node('button', `conversation-item${id === selectedId ? ' is-selected' : ''}`);
    button.type = 'button';
    button.disabled = !!activeTurn && id !== selectedId;
    button.dataset.testid = 'conversation-item';
    button.setAttribute('aria-current', id === selectedId ? 'true' : 'false');
    const route = instance.routing_mode === 'pinned'
      ? instance.account_ref : instance.routing_provider || 'All providers';
    button.append(node('strong', '', instance.model || 'Untitled conversation'),
      node('small', '', `${route} · ${formatClock(instance.created_at || instance.created)}`));
    button.addEventListener('click', () => onSelect(id));
    list.append(button);
  }
}

export function renderMessages(messages, activeTurn) {
  const list = byId('chat-messages');
  const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
  clear(list);
  const rows = Array.isArray(messages) ? messages : [];
  if (!rows.length) {
    list.append(emptyConversation?.cloneNode(true) || emptyState('Start a conversation',
      'Choose an observed model and send a message.'));
    return;
  }
  for (const message of rows) {
    if (!['user', 'assistant'].includes(message.role)) continue;
    const item = node('div', `message is-${message.role}${message.incomplete ? ' is-incomplete' : ''}`);
    item.dataset.testid = 'chat-message';
    item.dataset.role = message.role;
    const label = message.role === 'user' ? 'You' : 'AgentBridge';
    item.append(node('div', 'message-meta', `${label} · ${formatClock(message.created_at)}`),
      node('div', 'message-bubble', message.content || ''));
    list.append(item);
  }
  if (activeTurn && rows.at(-1)?.role === 'user') {
    const waiting = node('div', 'message is-assistant is-incomplete');
    waiting.append(node('div', 'message-meta', 'AgentBridge · working'),
      node('div', 'message-bubble', 'Thinking'));
    list.append(waiting);
  }
  if (wasNearBottom || activeTurn) list.scrollTop = list.scrollHeight;
}

function detail(event) {
  const data = event?.data || {};
  switch (event.kind) {
    case 'route.selected':
      return `${data.model || 'Model'} → ${data.account_ref || data.account_id || 'selected account'}${data.account_changed ? ' · account changed' : ''}`;
    case 'run.finished': return `State: ${data.state || 'unknown'}`;
    case 'run.error': return data.code ? `Error code: ${data.code}` : 'Run error recorded';
    case 'tool.started': return data.name || data.tool || 'Tool started';
    case 'tool.completed': return data.name || data.tool || 'Tool completed';
    case 'permission.required': return data.operation || 'One action needs a decision';
    case 'permission.responded': return data.decision ? `Decision: ${data.decision}` : 'Decision recorded';
    case 'message.completed': return 'Assistant response completed';
    case 'message.created': return 'User message accepted';
    case 'usage.observed': return 'Usage observation recorded';
    case 'recovery.gap': return data.reason || 'An event gap was recorded';
    default: return data.reason || data.state || '';
  }
}

function eventItem(event, onPermission, answered, finished) {
  const item = node('div', 'event-item');
  const dotClass = event.kind === 'run.error' || event.kind === 'recovery.gap'
    ? 'event-dot is-error' : event.kind === 'run.finished' || event.kind === 'message.completed'
      ? 'event-dot is-success' : 'event-dot';
  const dot = node('span', dotClass);
  const body = node('div');
  body.append(node('strong', '', event.kind || 'provider.event'));
  const description = detail(event);
  if (description) body.append(node('p', '', description));
  const expires = event.data?.expires_at;
  const openPermission = event.kind === 'permission.required' && event.data?.permission_id
    && !answered.has(event.data.permission_id) && !finished.has(event.turn_id)
    && (typeof expires !== 'number' || expires > Date.now() / 1000);
  if (openPermission && onPermission) {
    const actions = node('div', 'event-actions');
    for (const decision of ['deny', 'allow']) {
      const button = node('button', decision === 'allow' ? 'button button-secondary' : 'button button-ghost',
        decision === 'allow' ? 'Allow once' : 'Deny');
      button.type = 'button';
      button.addEventListener('click', () => onPermission(event, decision));
      actions.append(button);
    }
    body.append(actions);
  }
  const at = node('time', '', formatClock(event.at));
  if (event.at) {
    const date = new Date(typeof event.at === 'number' && event.at < 1e12
      ? event.at * 1000 : event.at);
    if (!Number.isNaN(date.getTime())) at.dateTime = date.toISOString();
  }
  item.append(dot, body, at);
  return item;
}

export function renderEvents(events, instance, onPermission) {
  const all = Array.isArray(events) ? events : [];
  const visible = all.filter((event) => event.kind !== 'message.delta');
  const answered = new Set(all.filter((event) => event.kind === 'permission.responded')
    .map((event) => event.data?.permission_id).filter(Boolean));
  const finished = new Set(all.filter((event) => event.kind === 'run.finished')
    .map((event) => event.turn_id).filter(Boolean));
  byId('event-count').textContent = `${visible.length} event${visible.length === 1 ? '' : 's'}`;
  byId('activity-title').textContent = instance?.model || 'No conversation selected';
  const route = instance?.routing_mode === 'pinned'
    ? `Pinned to ${instance.account_ref}` : instance?.routing_provider || 'All providers';
  byId('activity-subtitle').textContent = instance
    ? `Instance ${instance.instance_id || instance.id} · ${route}`
    : 'Open a conversation in Chat to inspect its events.';
  for (const id of ['chat-event-list', 'activity-list']) {
    const list = clear(byId(id));
    if (!visible.length) {
      list.append(emptyState('No events recorded', instance
        ? 'Events appear here when this conversation runs.'
        : 'Open a conversation in Chat to inspect its events.'));
      continue;
    }
    for (const event of visible.slice(-200)) {
      list.append(eventItem(event, onPermission, answered, finished));
    }
  }
}

export function renderChatHeader(instance, activeTurn, selected, pending) {
  byId('chat-conversation-title').textContent = instance?.model || 'New conversation';
  const applied = instance?.routing_mode === 'pinned'
    ? `Pinned to ${instance.account_ref}`
    : `${instance?.routing_provider || 'All providers'} · automatic`;
  const next = `${selected?.provider || 'All providers'} · ${selected?.model || 'Choose a model'} · ${selected?.account || 'automatic'}`;
  byId('chat-conversation-subtitle').textContent = activeTurn
    ? `A turn is running · ${applied}` : pending
      ? `Pending: ${next} · Applied: ${instance.model} · ${applied}`
      : instance ? applied : 'Ready when you are';
  byId('stop-turn').hidden = !activeTurn;
}
