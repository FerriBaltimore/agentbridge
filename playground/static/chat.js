import { api } from './api.js';
import { setupChatCatalog } from './chat-catalog.js';
import { setupChatDeletion } from './chat-deletion.js';
import { createChatStream } from './chat-stream.js';
import { renderChatHeader, renderConversations, renderEvents, renderMessages } from './chat-view.js';
import { byId, describeError, toast } from './ui.js';

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'incomplete']);
const LAST_INSTANCE_KEY = 'agentbridge.playground.last_instance_id';
const POLL_INTERVAL_MS = 2000;

function lastViewedInstance() {
  try { return localStorage.getItem(LAST_INSTANCE_KEY); } catch { return null; }
}

function rememberInstance(id) {
  try { localStorage.setItem(LAST_INSTANCE_KEY, id); } catch { /* Storage is optional. */ }
}

function forgetInstance(id) {
  try {
    if (localStorage.getItem(LAST_INSTANCE_KEY) === id) localStorage.removeItem(LAST_INSTANCE_KEY);
  } catch { /* Storage is optional. */ }
}

function instanceTime(instance) {
  const value = instance.updated_at || instance.updated || instance.created_at || instance.created;
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  return Date.parse(value) || 0;
}

function retainTimelineEvents(rows) {
  const latestDelta = new Map();
  for (const event of rows) {
    if (event.kind === 'message.delta') latestDelta.set(event.turn_id, event.seq);
  }
  return rows.filter((event) => event.kind !== 'message.delta'
    || latestDelta.get(event.turn_id) === event.seq).slice(-2000);
}

function instanceId(instance) {
  return instance?.instance_id || instance?.id || null;
}

function key() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function setupChat({ onDataChanged }) {
  const form = byId('chat-form');
  const input = byId('chat-input');
  const sendButton = byId('chat-send');
  const stopButton = byId('stop-turn');
  const newButton = byId('new-conversation');
  const routeSettings = byId('route-settings');
  const catalog = setupChatCatalog(updateControls);
  document.addEventListener('pointerdown', (event) => {
    if (routeSettings.open && !routeSettings.contains(event.target)) routeSettings.open = false;
  });
  routeSettings.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && routeSettings.open) {
      routeSettings.open = false;
      routeSettings.querySelector('summary').focus();
      event.stopPropagation();
    }
  });
  let instances = [];
  let current = null;
  let messages = [];
  let events = [];
  let eventCursor = 0;
  let activeTurn = null;
  let pendingSend = null;
  let timer = null;
  let busy = false;
  let refreshPromise = null;
  let restorePending = true;
  const stream = createChatStream((event) => {
    if (event.turn_id !== activeTurn) return;
    if (!events.some((item) => item.seq === event.seq)) {
      events.push(event);
      events.sort((a, b) => a.seq - b.seq);
      events = retainTimelineEvents(events);
      render();
    }
    if (['message.created', 'message.delta', 'message.completed'].includes(event.kind)) {
      void refreshConversation().catch((error) => toast(describeError(error), true));
    }
    if (event.kind === 'run.finished') {
      stopPolling();
      timer = setTimeout(pollTurn, 0);
    }
  }, (turnId) => {
    if (turnId === activeTurn) {
      stopPolling();
      timer = setTimeout(pollTurn, 0);
    }
  });
  const deletion = setupChatDeletion({
    getInstance: (id) => instances.find((item) => instanceId(item) === id),
    canDelete: () => !activeTurn && !busy,
    setBusy(value) { busy = value; render(); },
    onPending(id) {
      forgetInstance(id);
      if (instanceId(current) === id) resetConversation();
      else render();
    },
    async onDeleted(id) {
      instances = instances.filter((item) => instanceId(item) !== id);
      forgetInstance(id);
      if (instanceId(current) === id) resetConversation();
      else render();
      toast('Conversation deleted.');
      try { await onDataChanged(); }
      catch (error) {
        toast(`Conversation deleted, but the view could not refresh: ${describeError(error)}`, true);
      }
    },
  });

  function stopPolling() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function updateControls() {
    catalog.setBusy(busy || !!activeTurn);
    sendButton.disabled = busy || !!activeTurn || !catalog.canSend();
    stopButton.hidden = !activeTurn;
    newButton.disabled = !!activeTurn || busy;
    byId('activity-refresh').disabled = !current;
    renderChatHeader(current, activeTurn, catalog.selected(), catalog.hasPendingRoute());
  }

  function render() {
    renderConversations(instances, instanceId(current), activeTurn,
      selectConversation, deletion.open, busy, deletion.pendingRows(), deletion.retry);
    renderMessages(messages, events, activeTurn, answerPermission);
    renderEvents(events, current, answerPermission);
    updateControls();
  }

  async function refreshConversation() {
    if (!current) { render(); return; }
    if (refreshPromise) return refreshPromise;
    const id = instanceId(current);
    refreshPromise = (async () => {
      const [messageRows, firstEvents] = await Promise.all([
        api.messages(id), api.instanceEvents(id, eventCursor),
      ]);
      if (instanceId(current) !== id) return;
      messages = Array.isArray(messageRows) ? messageRows : [];
      let newEvents = firstEvents;
      for (let page = 0; page < 10 && Array.isArray(newEvents); page += 1) {
        let advanced = false;
        for (const event of newEvents) {
          if (typeof event.seq !== 'number' || event.seq <= eventCursor) continue;
          if (!events.some((item) => item.seq === event.seq)) events.push(event);
          eventCursor = event.seq;
          advanced = true;
        }
        if (instanceId(current) !== id || !advanced || newEvents.length < 200) break;
        newEvents = await api.instanceEvents(id, eventCursor);
      }
      if (instanceId(current) !== id) return;
      events.sort((a, b) => a.seq - b.seq);
      events = retainTimelineEvents(events);
      render();
    })();
    try { await refreshPromise; } finally { refreshPromise = null; }
  }

  async function pollTurn() {
    if (!activeTurn) return;
    const turnId = activeTurn;
    try {
      const [turn] = await Promise.all([api.turn(turnId), refreshConversation()]);
      if (turnId !== activeTurn) return;
      if (TERMINAL.has(turn.state)) {
        activeTurn = null;
        stream.close();
        stopPolling();
        await refreshConversation().catch((error) => toast(describeError(error), true));
        render();
        if (turn.state === 'completed') toast('Turn completed.');
        else toast(`Turn ended: ${turn.state}${turn.error ? ` (${turn.error})` : ''}.`, true);
        await onDataChanged();
      } else {
        timer = setTimeout(pollTurn, POLL_INTERVAL_MS);
      }
    } catch (error) {
      toast(describeError(error), true);
      if (activeTurn) timer = setTimeout(pollTurn, 4000);
    }
  }

  async function selectConversation(id) {
    if (activeTurn || busy || deletion.hasPending(id) || id === instanceId(current)) return;
    busy = true;
    stream.close();
    updateControls();
    try {
      current = await api.instance(id);
      restorePending = false;
      rememberInstance(id);
      const lastTurn = current.last_turn;
      activeTurn = lastTurn && !TERMINAL.has(lastTurn.state)
        ? lastTurn.turn_id : null;
      catalog.lock(current);
      messages = [];
      events = [];
      eventCursor = 0;
      pendingSend = null;
      await refreshConversation();
      if (activeTurn) {
        stream.open(activeTurn, eventCursor);
        stopPolling();
        timer = setTimeout(pollTurn, 300);
      }
    } catch (error) {
      toast(describeError(error), true);
    } finally {
      busy = false;
      render();
    }
  }

  async function refreshCurrentState() {
    if (!current) return;
    const id = instanceId(current);
    const latest = await api.instance(id);
    if (instanceId(current) !== id) return;
    current = latest;
    instances = [latest, ...instances.filter((item) => instanceId(item) !== id)];
    catalog.refreshInstance(latest);
    const lastTurn = latest.last_turn;
    activeTurn = lastTurn && !TERMINAL.has(lastTurn.state) ? lastTurn.turn_id : null;
    if (activeTurn) {
      stream.open(activeTurn, eventCursor);
      stopPolling();
      timer = setTimeout(pollTurn, 300);
    }
  }

  function resetConversation() {
    restorePending = false;
    stream.close();
    stopPolling();
    current = null;
    messages = [];
    events = [];
    eventCursor = 0;
    pendingSend = null;
    catalog.reset();
    render();
    input.focus();
  }

  function newConversation() {
    if (activeTurn || busy) return;
    resetConversation();
  }

  async function send(event) {
    event.preventDefault();
    if (activeTurn || busy) return;
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    if (!catalog.canSend()) {
      toast('Choose an available route and a valid context window.', true);
      return;
    }
    routeSettings.open = false;
    const choice = catalog.selected();
    const route = choice.account
      ? { account: choice.account, model: choice.model }
      : { provider: choice.provider, model: choice.model };
    const fingerprint = JSON.stringify({ text, route, effort: choice.effort,
      context: choice.context, permission: choice.permission, workspace: choice.workspace });
    if (!pendingSend || pendingSend.fingerprint !== fingerprint) {
      pendingSend = { fingerprint, createKey: key(), messageKey: key() };
    }
    busy = true;
    updateControls();
    try {
      if (!current) {
        const values = {
          model: choice.model,
          idempotency_key: pendingSend.createKey,
        };
        if (choice.workspace) values.workspace_path = choice.workspace;
        if (choice.account) values.account_ref = choice.account;
        else if (choice.provider) values.provider = choice.provider;
        current = await api.createInstance(values);
        restorePending = false;
        rememberInstance(instanceId(current));
        catalog.lock(current);
        instances = [current, ...instances.filter((item) => instanceId(item) !== instanceId(current))];
      } else {
        const update = catalog.updatePayload();
        if (update) {
          try {
            current = await api.updateInstance(instanceId(current), update);
            catalog.lock(current);
            instances = [current, ...instances.filter((item) => instanceId(item) !== instanceId(current))];
          } catch (error) {
            await refreshCurrentState().catch(() => {});
            throw error;
          }
        }
      }
      const values = { content: text, idempotency_key: pendingSend.messageKey };
      if (choice.effort) values.effort = choice.effort;
      if (choice.context) values.context_window = Number(choice.context);
      if (choice.permission) values.permission_mode = choice.permission;
      const result = await api.sendMessage(instanceId(current), values);
      activeTurn = result.turn_id;
      pendingSend = null;
      input.value = '';
      render();
      stream.open(activeTurn, eventCursor);
      stopPolling();
      timer = setTimeout(pollTurn, 300);
      try {
        await refreshConversation();
        await onDataChanged();
      } catch (error) {
        toast(`Turn started, but the local view could not refresh: ${describeError(error)}`, true);
      }
    } catch (error) {
      toast(describeError(error), true);
      await refreshConversation().catch(() => {});
    } finally {
      busy = false;
      render();
    }
  }

  async function stopTurn() {
    if (!activeTurn) return;
    stopButton.disabled = true;
    try {
      await api.stopTurn(activeTurn);
      toast('Stop requested for this turn.');
      stopPolling();
      timer = setTimeout(pollTurn, 500);
    } catch (error) {
      toast(describeError(error), true);
    } finally {
      stopButton.disabled = false;
    }
  }

  async function answerPermission(event, decision) {
    try {
      await api.answerPermission(event.turn_id, event.data.permission_id, decision);
      toast(`One permission response queued: ${decision}.`);
      await refreshConversation();
    } catch (error) {
      toast(describeError(error), true);
    }
  }

  form.addEventListener('submit', send);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  stopButton.addEventListener('click', stopTurn);
  newButton.addEventListener('click', newConversation);
  byId('activity-refresh').addEventListener('click', async () => {
    try { await refreshConversation(); } catch (error) { toast(describeError(error), true); }
  });

  return {
    updateData({ models, accounts, capabilities, workspacePath, instanceRows }) {
      instances = Array.isArray(instanceRows) ? instanceRows : [];
      catalog.update({ models, accountRows: accounts, capabilityData: capabilities, workspacePath });
      const available = instances.filter((item) => !deletion.hasPending(instanceId(item)));
      if (restorePending && !current && !busy && available.length) {
        restorePending = false;
        const preferred = lastViewedInstance();
        const latest = [...available].sort((a, b) => instanceTime(b) - instanceTime(a))[0];
        const chosen = available.find((instance) => instanceId(instance) === preferred) || latest;
        void selectConversation(instanceId(chosen));
      }
      render();
    },
    newConversation,
    selectConversation,
    get currentInstance() { return current; },
  };
}
