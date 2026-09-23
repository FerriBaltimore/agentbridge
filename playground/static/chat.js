import { api } from './api.js';
import { setupChatCatalog } from './chat-catalog.js';
import { renderChatHeader, renderConversations, renderEvents, renderMessages } from './chat-view.js';
import { byId, describeError, toast } from './ui.js';

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'incomplete']);

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
  const catalog = setupChatCatalog(updateControls);
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

  function stopPolling() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function updateControls() {
    sendButton.disabled = busy || !!activeTurn || !catalog.canSend();
    stopButton.hidden = !activeTurn;
    newButton.disabled = !!activeTurn || busy;
    byId('activity-refresh').disabled = !current;
  }

  function render() {
    renderChatHeader(current, activeTurn);
    renderConversations(instances, instanceId(current), activeTurn, selectConversation);
    renderMessages(messages, activeTurn);
    renderEvents(events, current, answerPermission);
    updateControls();
  }

  async function refreshConversation() {
    if (!current) { render(); return; }
    if (refreshPromise) return refreshPromise;
    const id = instanceId(current);
    refreshPromise = (async () => {
      const [messageRows, newEvents] = await Promise.all([
        api.messages(id), api.instanceEvents(id, eventCursor),
      ]);
      if (instanceId(current) !== id) return;
      messages = Array.isArray(messageRows) ? messageRows : [];
      if (Array.isArray(newEvents)) {
        for (const event of newEvents) {
          if (typeof event.seq === 'number' && event.seq > eventCursor) {
            events.push(event);
            eventCursor = event.seq;
          }
        }
        if (events.length > 2000) events = events.slice(-2000);
      }
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
        stopPolling();
        render();
        if (turn.state === 'completed') toast('Turn completed.');
        else toast(`Turn ended: ${turn.state}${turn.error ? ` (${turn.error})` : ''}.`, true);
        await onDataChanged();
      } else {
        timer = setTimeout(pollTurn, 1500);
      }
    } catch (error) {
      toast(describeError(error), true);
      timer = setTimeout(pollTurn, 4000);
    }
  }

  async function selectConversation(id) {
    if (activeTurn || busy || id === instanceId(current)) return;
    busy = true;
    updateControls();
    try {
      current = await api.instance(id);
      const lastTurn = current.last_turn;
      activeTurn = lastTurn && !TERMINAL.has(lastTurn.state)
        ? lastTurn.turn_id : null;
      catalog.lock(current);
      messages = [];
      events = [];
      eventCursor = 0;
      await refreshConversation();
      if (activeTurn) {
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

  function newConversation() {
    if (activeTurn || busy) return;
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

  async function send(event) {
    event.preventDefault();
    if (activeTurn || busy) return;
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    if (!catalog.canSend()) {
      toast('Choose a model observed from an available account.', true);
      return;
    }
    const choice = catalog.selected();
    if (!pendingSend || pendingSend.text !== text) {
      pendingSend = { text, createKey: key(), messageKey: key() };
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
        catalog.lock(current);
        instances = [current, ...instances.filter((item) => instanceId(item) !== instanceId(current))];
      }
      const values = { content: text, idempotency_key: pendingSend.messageKey };
      if (choice.effort) values.effort = choice.effort;
      if (choice.context) values.context_window = /^[0-9]+$/.test(choice.context)
        ? Number(choice.context) : choice.context;
      if (choice.permission) values.permission_mode = choice.permission;
      const result = await api.sendMessage(instanceId(current), values);
      activeTurn = result.turn_id;
      pendingSend = null;
      input.value = '';
      render();
      stopPolling();
      timer = setTimeout(pollTurn, 800);
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
      render();
    },
    newConversation,
    selectConversation,
    get currentInstance() { return current; },
  };
}
