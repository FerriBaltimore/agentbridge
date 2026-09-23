import { api } from './api.js';
import { byId, clear, describeError, node, setFeedback, toast } from './ui.js';

const PENDING = new Set(['starting', 'awaiting_user', 'exchanging']);
const FINISHED = new Set(['failed', 'cancelled', 'expired', 'abandoned', 'revoked', 'replaced']);

export function setupLogin({ providers, onComplete }) {
  const dialog = byId('login-dialog');
  const form = byId('login-form');
  const progress = byId('login-progress');
  const feedback = byId('login-feedback');
  const startButton = byId('login-start');
  const cancelButton = byId('login-cancel');
  const retryButton = byId('login-retry-check');
  let attempt = null;
  let timer = null;
  let busy = false;

  function stopPolling() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function step(current) {
    for (const [name, index] of [['config', 1], ['authorize', 2], ['verify', 3]]) {
      const item = byId(`login-step-${name}`);
      item.className = index < current ? 'is-done' : index === current ? 'is-current' : '';
    }
  }

  function setProviders() {
    const selector = byId('login-provider');
    const previous = selector.value;
    clear(selector);
    const values = providers();
    for (const value of values) {
      const option = node('option', '', value);
      option.value = value;
      selector.append(option);
    }
    if (!values.length) {
      const option = node('option', '', 'No provider advertised');
      option.value = '';
      selector.append(option);
    }
    if (values.includes(previous)) selector.value = previous;
    startButton.disabled = !values.length;
  }

  function safeAuthorizationUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' ? url.href : null;
    } catch {
      return null;
    }
  }

  function showAttempt(value) {
    attempt = { ...attempt, ...value };
    form.hidden = true;
    progress.hidden = false;
    const status = attempt.status || 'starting';
    step(status === 'verified' || status === 'authorized' || status === 'bound' ? 3 : 2);
    const messages = {
      starting: 'Starting sign-in…',
      awaiting_user: 'Waiting for authorization in your browser…',
      exchanging: 'The provider is finishing authorization…',
      authorized: 'Authorization received. Verifying the account…',
      verified: 'Account verified. Finishing setup…',
      bound: 'Account ready.',
      failed: 'The provider did not complete authentication.',
      cancelled: 'Sign-in was cancelled.',
      interrupted: 'The sign-in outcome could not be confirmed.',
      abandoned: 'This sign-in attempt was closed.',
    };
    byId('login-status').textContent = messages[status] || `OAuth state: ${status}`;
    const url = safeAuthorizationUrl(attempt.authorization_url);
    const link = byId('login-open-url');
    link.hidden = !url || !PENDING.has(status);
    if (url) link.href = url;
    const code = attempt.user_code;
    byId('login-user-code').hidden = !code || !PENDING.has(status);
    byId('login-code-value').textContent = code || '';
    byId('login-progress-help').textContent = status === 'abandoned'
      ? 'The previous authorization may still be active. Check your provider account before trying again.'
      : status === 'interrupted'
        ? 'You can close this attempt, but AgentBridge cannot confirm that authorization stopped.'
        : 'AgentBridge verifies your account after authorization before adding it.';
    cancelButton.textContent = FINISHED.has(status) ? 'Close' : status === 'interrupted' ? 'Abandon attempt' : 'Cancel login';
    cancelButton.hidden = status === 'authorized' || status === 'verified' || status === 'bound';
    retryButton.hidden = status !== 'authorized' && status !== 'verified';
  }

  async function finishLogin() {
    if (!attempt || busy) return;
    busy = true;
    stopPolling();
    retryButton.disabled = true;
    setFeedback(feedback, '');
    try {
      if (attempt.status !== 'verified') {
        const checked = await api.loginCheck(attempt.attempt_id, attempt.owner_ref);
        showAttempt(checked);
      }
      const completed = await api.loginComplete(attempt.attempt_id, attempt.owner_ref);
      showAttempt(completed.attempt || { status: 'bound' });
      attempt = null;
      dialog.close();
      toast('Account connected and verified.');
      try {
        await onComplete();
      } catch (error) {
        toast(`Account connected, but the local view could not refresh: ${describeError(error)}`, true);
      }
    } catch (error) {
      setFeedback(feedback, describeError(error));
      retryButton.hidden = false;
    } finally {
      busy = false;
      retryButton.disabled = false;
    }
  }

  async function poll() {
    if (!attempt || busy) return;
    try {
      const current = await api.loginStatus(attempt.attempt_id, attempt.owner_ref);
      showAttempt(current);
      if (current.status === 'authorized' || current.status === 'verified') {
        await finishLogin();
        return;
      }
      if (PENDING.has(current.status)) timer = setTimeout(poll, 1500);
    } catch (error) {
      setFeedback(feedback, describeError(error));
      timer = setTimeout(poll, 4000);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    startButton.disabled = true;
    setFeedback(feedback, '');
    const values = {
      provider: byId('login-provider').value,
      name: byId('login-name').value.trim(),
    };
    let completeImmediately = false;
    try {
      const started = await api.loginStart(values);
      showAttempt(started);
      if (PENDING.has(started.status)) timer = setTimeout(poll, 1000);
      else completeImmediately = started.status === 'authorized' || started.status === 'verified';
    } catch (error) {
      setFeedback(feedback, describeError(error));
    } finally {
      busy = false;
      startButton.disabled = !providers().length;
    }
    if (completeImmediately) await finishLogin();
  });

  cancelButton.addEventListener('click', async () => {
    if (!attempt || busy) return;
    if (FINISHED.has(attempt.status)) { dialog.close(); attempt = null; return; }
    busy = true;
    cancelButton.disabled = true;
    stopPolling();
    try {
      const result = await api.loginCancel(attempt.attempt_id, attempt.owner_ref);
      showAttempt(result);
      if (result.status === 'cancelled') toast('Sign-in was cancelled.');
      if (result.status === 'abandoned') toast('This sign-in attempt was closed.');
    } catch (error) {
      setFeedback(feedback, describeError(error));
    } finally {
      busy = false;
      cancelButton.disabled = false;
    }
  });

  retryButton.addEventListener('click', finishLogin);
  dialog.addEventListener('close', () => {
    if (attempt && PENDING.has(attempt.status)) toast('OAuth continues in this tab. Open Add account to return to it.');
  });

  return () => {
    setProviders();
    setFeedback(feedback, '');
    if (!attempt) {
      form.hidden = false;
      progress.hidden = true;
      step(1);
    } else showAttempt(attempt);
    dialog.showModal();
    (attempt ? cancelButton : byId('login-name')).focus();
  };
}
