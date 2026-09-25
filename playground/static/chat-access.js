// Persisted access choices for one conversation.
import { byId, clear, node } from './ui.js';

const DEFAULTS = { permission: 'dontAsk', sandbox: 'read-only' };
const LABELS = {
  dontAsk: 'No approval prompts', default: 'Ask when required',
  'read-only': 'Read-only', 'workspace-write': 'Workspace write',
  'danger-full-access': 'Full access',
};
const DESCRIPTIONS = {
  'read-only': 'Allows reading workspace files; file changes are restricted.',
  'workspace-write': 'Allows file changes in this workspace.',
  'danger-full-access': 'Allows host file and network access.',
};

export function setupChatAccess(onChange) {
  const permission = byId('chat-permission');
  const sandbox = byId('chat-sandbox');
  const save = byId('save-access-settings');
  let instance = null;
  let busy = false;

  function selected() {
    return { permission: permission.value || DEFAULTS.permission,
      sandbox: sandbox.value || DEFAULTS.sandbox };
  }

  function payload() {
    if (!instance) return null;
    const choice = selected();
    const changes = {};
    if (choice.permission !== (instance.permission_mode || DEFAULTS.permission)) {
      changes.permission_mode = choice.permission;
    }
    if (choice.sandbox !== (instance.sandbox_mode || DEFAULTS.sandbox)) {
      changes.sandbox_mode = choice.sandbox;
    }
    return Object.keys(changes).length ? { expected_version: instance.version, ...changes } : null;
  }

  function render() {
    const choice = selected();
    permission.disabled = sandbox.disabled = busy;
    save.hidden = !instance;
    save.disabled = busy || !payload();
    byId('access-help').textContent = `${DESCRIPTIONS[choice.sandbox]} ${instance
      ? payload() ? 'Save these choices or send a message to apply them.'
        : 'Saved for this conversation.'
      : 'These choices will be saved with your conversation.'}`;
  }

  function populate(field, parameter, fallback) {
    const previous = field.value || fallback;
    clear(field);
    const values = Array.isArray(parameter?.values) ? parameter.values : [];
    for (const item of values) {
      const value = typeof item === 'string' ? item : item.value;
      if (!LABELS[value]) continue;
      const option = node('option', '', LABELS[value]);
      option.value = value;
      field.append(option);
    }
    field.value = [...field.options].some((option) => option.value === previous) ? previous : fallback;
  }

  for (const field of [permission, sandbox]) {
    field.addEventListener('change', () => { render(); onChange(); });
  }

  return {
    selected,
    payload,
    pending: () => !!payload(),
    summary: () => LABELS[selected().sandbox],
    update(capabilities) {
      populate(permission, capabilities?.parameters?.permission_mode, DEFAULTS.permission);
      populate(sandbox, capabilities?.parameters?.sandbox_mode, DEFAULTS.sandbox);
      byId('permission-field').hidden = !permission.options.length;
      byId('sandbox-field').hidden = !sandbox.options.length;
      render();
    },
    lock(value, preserveDraft = false) {
      instance = value;
      if (!preserveDraft) {
        permission.value = value?.permission_mode || DEFAULTS.permission;
        sandbox.value = value?.sandbox_mode || DEFAULTS.sandbox;
      }
      render();
    },
    setBusy(value) { busy = value; render(); },
  };
}
