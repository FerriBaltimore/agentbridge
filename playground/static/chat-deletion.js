import { api } from './api.js';
import { byId, describeError, setFeedback, toast } from './ui.js';

const PENDING_KEY = 'agentbridge.playground.pending_conversation_deletions';

function storedDeletions() {
  try {
    const rows = JSON.parse(localStorage.getItem(PENDING_KEY) || '[]');
    if (!Array.isArray(rows)) return new Map();
    return new Map(rows.filter((row) => typeof row?.id === 'string' && row.id
      && typeof row.label === 'string').slice(-100).map((row) =>
      [row.id, { id: row.id, label: row.label }]));
  } catch { return new Map(); }
}

export function setupChatDeletion({ getInstance, canDelete, setBusy, onPending, onDeleted }) {
  const dialog = byId('delete-conversation-dialog');
  const confirm = byId('delete-conversation-confirm');
  const cancel = byId('delete-conversation-cancel');
  const feedback = byId('delete-conversation-feedback');
  const pending = storedDeletions();
  let selected = null;

  function persist() {
    try {
      if (pending.size) localStorage.setItem(PENDING_KEY, JSON.stringify([...pending.values()]));
      else localStorage.removeItem(PENDING_KEY);
    } catch { /* Browser storage is optional. */ }
  }

  function open(id) {
    if (!canDelete() || dialog.open) return;
    const instance = getInstance(id);
    if (!instance) return;
    selected = { id, label: instance.model || 'Untitled conversation' };
    byId('delete-conversation-name').textContent = selected.label;
    setFeedback(feedback, '');
    confirm.disabled = false;
    cancel.disabled = false;
    dialog.showModal();
    confirm.focus();
  }

  async function attempt(row) {
    if (!row || !canDelete()) return;
    setBusy(true);
    confirm.disabled = true;
    cancel.disabled = true;
    setFeedback(feedback, '');
    pending.set(row.id, row);
    persist();
    try {
      const result = await api.deleteInstance(row.id);
      if (result?.deleted === true) {
        if (dialog.open) dialog.close();
        await onDeleted(row.id);
        pending.delete(row.id);
        persist();
      } else {
        if (dialog.open) dialog.close();
        onPending(row.id);
        toast(result?.pending
          ? 'Conversation cleanup is pending. Use Retry cleanup to finish it.'
          : 'Deletion was not confirmed. Use Retry cleanup to check again.', !result?.pending);
      }
    } catch (error) {
      if (error?.code === 'busy') {
        pending.delete(row.id);
        persist();
        if (dialog.open) setFeedback(feedback, describeError(error));
        else toast(describeError(error), true);
      } else {
        if (dialog.open) dialog.close();
        onPending(row.id);
        toast(`Deletion needs retry: ${describeError(error)}`, true);
      }
    } finally {
      confirm.disabled = false;
      cancel.disabled = false;
      setBusy(false);
    }
  }

  confirm.addEventListener('click', () => { void attempt(selected); });
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', (event) => { if (!canDelete()) event.preventDefault(); });
  dialog.addEventListener('close', () => { selected = null; });

  return {
    open,
    retry(id) { void attempt(pending.get(id)); },
    hasPending(id) { return pending.has(id); },
    pendingRows() { return [...pending.values()]; },
  };
}
