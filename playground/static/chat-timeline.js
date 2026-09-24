import { formatClock, node } from './ui.js';

const ACTIVITY_KINDS = new Set([
  'tool.started', 'tool.completed',
  'context.compacting', 'context.compacted', 'subagent.status', 'run.retrying', 'model.changed',
  'permission.required', 'permission.responded', 'permission.denied',
  'run.error', 'recovery.gap', 'recovery.observed', 'run.finished',
]);

function timeValue(value) {
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  return Date.parse(value) || 0;
}

function messageNode(message) {
  const item = node('div', `message is-${message.role}${message.incomplete ? ' is-incomplete' : ''}`);
  item.dataset.testid = 'chat-message';
  item.dataset.role = message.role;
  const label = message.role === 'user' ? 'You' : 'AgentBridge';
  item.append(node('div', 'message-meta', `${label} · ${formatClock(message.created_at)}`),
    node('div', 'message-bubble', message.content || ''));
  return item;
}

function activityText(event, completedCalls) {
  const data = event.data || {};
  const name = String(data.name || data.tool || 'Tool').replaceAll('_', ' ');
  switch (event.kind) {
    case 'tool.started': return [completedCalls.has(`${event.turn_id}:${data.call_id}`)
      ? `${name} started` : `${name} running`, ''];
    case 'tool.completed': return [`${name} ${data.outcome === 'failed' ? 'failed'
      : data.outcome === 'completed' ? 'completed' : 'finished'}`,
    data.outcome === 'unknown' ? 'Outcome unknown.' : ''];
    case 'context.compacting': return ['Compacting context…', 'The provider is condensing the conversation.'];
    case 'context.compacted': return ['Context compacted', 'Conversation context was condensed.'];
    case 'subagent.status': return ['Agent activity', `Status: ${data.status || 'unknown'}`];
    case 'run.retrying': return ['Provider retrying', 'The current turn is still in progress.'];
    case 'model.changed': return ['Model changed', data.model || 'The provider changed the model.'];
    case 'permission.required': return ['Permission needed', 'One action needs your decision.'];
    case 'permission.responded': return ['Permission answered',
      data.decision ? `Decision: ${data.decision}` : 'Decision recorded.'];
    case 'permission.denied': return ['Permission denied', 'The action was not allowed.'];
    case 'run.error': return ['Run error', data.code ? `Code: ${data.code}` : 'Error recorded.'];
    case 'recovery.gap': return ['Observation gap', 'Some provider activity could not be observed.'];
    case 'recovery.observed': return ['Recovery observed', 'The provider reported recovery.'];
    case 'run.finished': return ['Turn finished', `State: ${data.state || 'unknown'}`];
    default: return ['', ''];
  }
}

function showActivity(event) {
  if (!ACTIVITY_KINDS.has(event.kind)) return false;
  if (event.kind === 'run.finished') return event.data?.state !== 'completed';
  if (event.kind === 'recovery.gap') {
    const data = event.data || {};
    // Keep these provider observations in Activity without treating them as chat failures.
    return data.reason !== 'unsupported_provider_observation'
      && !(data.reason === 'unsupported_item' && data.native_type === 'error');
  }
  return true;
}

function permissionActions(event, answered, finished, onPermission) {
  const permissionId = event.data?.permission_id;
  const expires = event.data?.expires_at;
  if (event.kind !== 'permission.required' || !permissionId || !onPermission
    || answered.has(permissionId) || finished.has(event.turn_id)
    || (typeof expires === 'number' && expires <= Date.now() / 1000)) return null;
  const actions = node('div', 'timeline-actions');
  for (const [decision, label] of [['deny', 'Deny'], ['allow', 'Allow once']]) {
    const button = node('button', decision === 'allow' ? 'button button-secondary' : 'button button-ghost', label);
    button.type = 'button';
    button.addEventListener('click', () => onPermission(event, decision));
    actions.append(button);
  }
  return actions;
}

function activityNode(event, completedCalls, compactedTurns, answered, finished, onPermission) {
  const [title, description] = activityText(event, completedCalls);
  const runningTool = event.kind === 'tool.started'
    && !completedCalls.has(`${event.turn_id}:${event.data?.call_id}`);
  const compacting = event.kind === 'context.compacting'
    && !compactedTurns.has(event.turn_id) && !finished.has(event.turn_id);
  const item = node('div', `timeline-event${runningTool || compacting ? ' is-running' : ''}`);
  item.dataset.testid = 'chat-timeline-event';
  item.dataset.kind = event.kind;
  item.dataset.seq = event.seq;
  const marker = node('span', 'timeline-marker');
  marker.setAttribute('aria-hidden', 'true');
  const body = node('div', 'timeline-event-body');
  body.append(node('strong', '', title));
  if (description) body.append(node('small', '', description));
  const actions = permissionActions(event, answered, finished, onPermission);
  if (actions) body.append(actions);
  item.append(marker, body, node('time', '', formatClock(event.at)));
  return item;
}

function groupsFor(messages, events) {
  const groups = new Map();
  const groupFor = (id, at) => {
    if (!groups.has(id)) groups.set(id, { id, messages: [], events: [], at: timeValue(at) });
    const group = groups.get(id);
    group.at = Math.min(group.at || Infinity, timeValue(at) || Infinity);
    return group;
  };
  for (const message of messages) {
    if (['user', 'assistant'].includes(message.role)) {
      groupFor(message.sequence || message.message_id, message.created_at).messages.push(message);
    }
  }
  for (const event of events) {
    if (event.turn_id) groupFor(event.turn_id, event.at).events.push(event);
  }
  return [...groups.values()].sort((a, b) => a.at - b.at);
}

export function appendTimeline(list, messages, events, activeTurn, onPermission) {
  const allEvents = Array.isArray(events) ? events : [];
  const rows = Array.isArray(messages) ? messages : [];
  const answered = new Set(allEvents.filter((event) => event.kind === 'permission.responded')
    .map((event) => event.data?.permission_id).filter(Boolean));
  const finished = new Set(allEvents.filter((event) => event.kind === 'run.finished')
    .map((event) => event.turn_id).filter(Boolean));
  const completedCalls = new Set(allEvents.filter((event) => event.kind === 'tool.completed')
    .map((event) => `${event.turn_id}:${event.data?.call_id}`));
  const compactedTurns = new Set(allEvents.filter((event) => event.kind === 'context.compacted')
    .map((event) => event.turn_id));
  let count = 0;
  for (const group of groupsFor(rows, allEvents)) {
    for (const message of group.messages.filter((item) => item.role === 'user')) {
      list.append(messageNode(message));
      count += 1;
    }
    const assistant = group.messages.filter((item) => item.role === 'assistant' && item.content);
    const ordered = [...group.events].sort((a, b) => a.seq - b.seq);
    const outputSeq = Math.max(0, ...ordered.filter((event) =>
      ['message.delta', 'message.completed'].includes(event.kind)).map((event) => event.seq));
    let responseShown = false;
    const showResponse = () => {
      if (responseShown) return;
      for (const message of assistant) { list.append(messageNode(message)); count += 1; }
      responseShown = true;
    };
    for (const event of ordered) {
      if (!responseShown && outputSeq && event.seq > outputSeq) showResponse();
      if (!showActivity(event)) continue;
      list.append(activityNode(event, completedCalls, compactedTurns, answered, finished, onPermission));
      count += 1;
    }
    showResponse();
    if (group.id === activeTurn && !assistant.length) {
      const waiting = node('div', 'message is-assistant is-incomplete');
      waiting.append(node('div', 'message-meta', 'AgentBridge · working'),
        node('div', 'message-bubble', 'Working…'));
      list.append(waiting);
      count += 1;
    }
  }
  if (activeTurn && !groupsFor(rows, allEvents).some((group) => group.id === activeTurn)) {
    const waiting = node('div', 'message is-assistant is-incomplete');
    waiting.append(node('div', 'message-meta', 'AgentBridge · working'),
      node('div', 'message-bubble', 'Working…'));
    list.append(waiting);
    count += 1;
  }
  return count;
}
