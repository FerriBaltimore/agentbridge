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
  const recovery = byId('login-recovery');
  const recoveryList = byId('login-recovery-list');
  const recoveryPages = byId('login-recovery-pages');
  const recoveryPrev = byId('login-recovery-prev');
  const recoveryNext = byId('login-recovery-next');
  let attempt = null;
  let timer = null;
  let busy = false;
  let recoveryCursor = 0;
  let recoveryGeneration = 0;

  async function refreshRecovery() {
    const generation = ++recoveryGeneration;
    try {
      const rows = await api.loginAttempts(3, recoveryCursor);
      if (generation !== recoveryGeneration || !dialog.open || attempt) return;
      if (!rows.length && recoveryCursor > 0) {
        recoveryCursor = Math.max(0, recoveryCursor - 2);
        await refreshRecovery();
        return;
      }
      recovery.hidden = !rows.length;
      clear(recoveryList);
      for (const row of rows.slice(0, 2)) {
        const item = node('div', 'event-item');
        const label = node('div');
        label.append(node('strong', '', `${row.account_ref} · ${row.provider}`));
        const abandon = node('button', 'button button-ghost', 'Abandon');
        abandon.type = 'button';
        abandon.setAttribute('aria-label', `Abandon ${row.account_ref} ${row.provider} sign-in`);
        abandon.addEventListener('click', async () => {
          if (busy) return;
          busy = true;
          abandon.disabled = true;
          setFeedback(feedback, '');
          try {
            const result = await api.loginCancel(row.attempt_id, row.owner_ref);
            if (result.status !== 'abandoned') {
              throw new Error('The local attempt was not abandoned. Check its status before retrying.');
            }
            toast('Local sign-in attempt abandoned. Remote authorization was not confirmed cancelled.');
            await refreshRecovery();
          } catch (error) {
            setFeedback(feedback, describeError(error));
          } finally {
            busy = false;
            abandon.disabled = false;
          }
        });
        item.append(node('span', 'event-dot'), label, abandon);
        recoveryList.append(item);
      }
      recoveryPages.hidden = recoveryCursor === 0 && rows.length <= 2;
      recoveryPrev.disabled = recoveryCursor === 0;
      recoveryNext.disabled = rows.length <= 2;
    } catch (error) {
      if (generation === recoveryGeneration && dialog.open && !attempt) {
        setFeedback(feedback, `Could not load unfinished sign-ins: ${describeError(error)}`);
      }
    }
  }

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

  function showAttempt(value) {
    attempt = { ...attempt, ...value };
    recoveryGeneration += 1;
    recovery.hidden = true;
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
    const code = attempt.user_code;
    byId('login-user-code').hidden = !code || !PENDING.has(status);
    byId('login-code-value').textContent = code || '';
    byId('login-progress-help').textContent = status === 'abandoned'
      ? 'The previous authorization may still be active. Check your provider account before trying again.'
      : status === 'interrupted'
        ? 'You can close this attempt, but AgentBridge cannot confirm that authorization stopped.'
        : PENDING.has(status) && attempt.browser_opened === true
          ? 'Complete authorization in the separate private browser window. AgentBridge checks the account identity before connecting it.'
          : PENDING.has(status)
            ? 'The private browser could not open. Cancel this attempt, check that Chrome or Chromium and a desktop session are available, then try again.'
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
      if (error?.code === 'identity_changed' && attempt) {
        try {
          showAttempt(await api.loginStatus(attempt.attempt_id, attempt.owner_ref));
        } catch {
          // Keep the original identity error visible if status is unavailable.
        }
      }
      setFeedback(feedback, describeError(error));
      retryButton.hidden = !attempt || !['authorized', 'verified'].includes(attempt.status);
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
    const email = byId('login-email').value.trim();
    if (email) values.email = email;
    let completeImmediately = false;
    try {
      const started = await api.loginStart(values);
      showAttempt(started);
      if (PENDING.has(started.status)) timer = setTimeout(poll, 1000);
      else completeImmediately = started.status === 'authorized' || started.status === 'verified';
    } catch (error) {
      const details = error?.data?.details;
      if (error?.code === 'authentication_outcome_unknown'
          && typeof details?.attempt_id === 'string'
          && typeof details?.owner_ref === 'string') {
        showAttempt({ attempt_id: details.attempt_id, owner_ref: details.owner_ref,
          provider: values.provider, account_ref: values.name, status: 'interrupted' });
      }
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
  recoveryPrev.addEventListener('click', () => {
    recoveryCursor = Math.max(0, recoveryCursor - 2);
    refreshRecovery();
  });
  recoveryNext.addEventListener('click', () => {
    recoveryCursor += 2;
    refreshRecovery();
  });
  dialog.addEventListener('close', () => {
    recoveryGeneration += 1;
    if (attempt && PENDING.has(attempt.status)) toast('OAuth continues in this tab. Open Add account to return to it.');
  });

  return () => {
    setProviders();
    setFeedback(feedback, '');
    if (!attempt) {
      form.hidden = false;
      progress.hidden = true;
      step(1);
      recoveryCursor = 0;
    } else showAttempt(attempt);
    dialog.showModal();
    if (!attempt) refreshRecovery();
    (attempt ? cancelButton : byId('login-name')).focus();
  };
}
