import { api } from './api.js';
import { byId, clear, describeError, node, toast } from './ui.js';

const REASONS = {
  user_pause: 'Messages will wait until you resume.',
  user_stop: 'Stopped. Resume when you want to continue.',
  unknown_outcome: 'A delivery could not be confirmed. Review the conversation before resuming.',
  context_required: 'This message needs its original private context to be reconnected.',
  replacement_cancelled: 'The replacement was removed. Resume to continue the remaining messages.',
};

export function setupChatQueue({ getState, refresh, changed }) {
  const panel = byId('chat-queue');
  const list = byId('chat-queue-list');
  const toggle = byId('chat-queue-toggle');
  const mode = byId('chat-delivery');
  let snapshot = null;
  let editing = false;
  let signature = '';

  async function edit(action, values = {}) {
    const { id, activeTurn } = getState();
    if (!id || editing || !snapshot) return;
    const version = snapshot.version;
    editing = true;
    changed();
    try {
      await api.editQueue(id, action, { ...values, expected_version: version,
        ...(action === 'dispatch' && activeTurn ? { expected_turn_id: activeTurn } : {}) });
    } catch (error) {
      toast(describeError(error), true);
    } finally {
      try { await refresh(); } catch (error) { toast(describeError(error), true); }
      editing = false;
      changed();
    }
  }

  function button(label, testId, disabled, action) {
    const value = node('button', 'queue-action', label);
    value.type = 'button';
    value.dataset.testid = testId;
    value.disabled = disabled;
    value.addEventListener('click', action);
    return value;
  }

  function render() {
    const { id, activeTurn, busy } = getState();
    const locked = busy || editing;
    panel.hidden = !id || !snapshot;
    mode.hidden = !activeTurn && mode.value === 'queue';
    mode.disabled = locked;
    byId('chat-send-label').textContent = mode.value === 'steer' ? 'Send now'
      : mode.value === 'interrupt' ? 'Stop & send' : activeTurn || snapshot?.paused ? 'Add to queue' : 'Send';
    byId('composer-hint').textContent = activeTurn && mode.value === 'queue'
      ? 'Messages wait their turn · Shift+Enter for a new line'
      : 'Enter to send · Shift+Enter for a new line';
    if (!snapshot) return;
    const count = snapshot.total;
    byId('chat-queue-summary').textContent = `Queue · ${count} pending${snapshot.paused ? ' · paused' : ''}`;
    toggle.textContent = snapshot.paused || !snapshot.dispatcher_running && count ? 'Resume queue' : 'Pause queue';
    toggle.disabled = locked;
    byId('chat-queue-status').textContent = snapshot.paused
      ? REASONS[snapshot.reason] || 'Queue paused. Review the last result before resuming.'
      : !snapshot.dispatcher_running && count ? 'Messages are saved. Resume to reconnect the queue.'
        : count ? 'Pending messages run in this order.' : 'New messages will run when this conversation is ready.';
    const next = JSON.stringify([snapshot.items, snapshot.in_flight, locked, activeTurn]);
    if (signature === next) return;
    signature = next;
    const focus = document.activeElement?.closest('[data-message-id]');
    const focusId = focus?.dataset.messageId;
    const focusAction = document.activeElement?.dataset.testid;
    clear(list);
    for (const [index, item] of snapshot.items.entries()) {
      const row = node('li', 'queue-item');
      row.dataset.testid = 'queue-item';
      row.dataset.messageId = item.message_id;
      row.append(node('span', 'queue-position', String(index + 1)), node('p', 'queue-content', item.content));
      const actions = node('div', 'queue-actions');
      const replacement = item.delivery === 'interrupt';
      actions.append(
        button('↑', 'queue-up', locked || replacement || index === 0,
          () => edit('move', { message_id: item.message_id, position: index - 1 })),
        button('↓', 'queue-down', locked || replacement || index === count - 1,
          () => edit('move', { message_id: item.message_id, position: index + 1 })),
        button('Send now', 'queue-steer', locked || replacement || !activeTurn,
          () => edit('dispatch', { message_id: item.message_id, mode: 'steer' })),
        button(activeTurn ? 'Stop & send' : 'Run next', 'queue-interrupt', locked || replacement,
          () => edit('dispatch', { message_id: item.message_id, mode: 'interrupt' })),
        button('Remove', 'queue-remove', locked, () => edit('delete', { message_id: item.message_id })),
      );
      actions.children[0].setAttribute('aria-label', `Move message ${index + 1} up`);
      actions.children[1].setAttribute('aria-label', `Move message ${index + 1} down`);
      actions.children[2].title = 'Send this message to the active turn.';
      actions.children[3].title = 'Run this message next, stopping the active turn if needed.';
      row.append(actions);
      if (item.error || replacement) row.append(node('small', 'queue-item-status', replacement
        ? 'Waiting for the current turn to stop' : `Needs attention: ${item.error}`));
      list.append(row);
    }
    for (const item of snapshot.in_flight) {
      const row = node('li', 'queue-item is-delivering');
      row.append(node('p', 'queue-content', item.content), node('small', '', 'Sending to the active turn…'));
      list.append(row);
    }
    if (focusId && focusAction) {
      [...list.children].find((row) => row.dataset.messageId === focusId)
        ?.querySelector(`[data-testid="${focusAction}"]`)?.focus({ preventScroll: true });
    }
  }

  toggle.addEventListener('click', () => edit(snapshot?.paused || !snapshot?.dispatcher_running
    && snapshot?.total ? 'resume' : 'pause'));
  mode.addEventListener('change', changed);
  return {
    render,
    set(value) {
      if ((value?.total || value?.in_flight?.length) && !(snapshot?.total || snapshot?.in_flight?.length)) {
        panel.open = true;
      }
      snapshot = value;
      if (!value) { signature = ''; panel.open = false; mode.value = 'queue'; }
    },
    get occupied() { return !!(snapshot?.total || snapshot?.in_flight?.length); },
    get waiting() { return this.occupied && !snapshot?.paused; },
    get editing() { return editing; },
    get mode() { return mode.value; },
    resetMode() { mode.value = 'queue'; },
  };
}
