"""Public event snapshots and an incremental, provider-neutral turn iterator."""
import math
import time

from .errors import BridgeError, UnsupportedError
from .event_contract import public_event
from .models import TERMINAL, page_values


def _timeout_seconds(value):
    if value is None:
        return None
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        raise BridgeError('invalid_timeout', 'timeout_ms must be finite and nonnegative.')
    return value / 1000


class EventStreamMixin:
    def _turn_event_page(self, run, after_seq, limit, timeout):
        """Return one saved page, optionally waiting for its first event."""
        started = time.monotonic()
        next_recovery_at = started + 0.5
        while True:
            page = self.store.events(run_id=run.id, after=after_seq, limit=limit)
            if page:
                return page
            if run.status in TERMINAL:
                return []
            now = time.monotonic()
            if timeout is not None and now - started >= timeout:
                return []
            if now >= next_recovery_at:
                self.recover(turn_id=run.id)
                next_recovery_at = time.monotonic() + 0.5
                continue
            wait = min(0.05, next_recovery_at - now)
            if timeout is not None:
                wait = min(wait, timeout - (now - started))
            time.sleep(max(0, wait))

    def turn_events(self, turn_id, *, after_seq=0, limit=1000, follow=False, timeout_ms=None):
        timeout = _timeout_seconds(timeout_ms)
        limit, after_seq = page_values(limit, after_seq)
        run = self.run(turn_id)
        engine = self.account(run.session['account_id']).engine
        events = (self._turn_event_page(run, after_seq, limit, timeout) if follow
                  else run.events(after=after_seq, limit=limit))
        return [public_event(event, engine) for event in events]

    def instance_events(self, instance_id, *, after_seq=0, limit=1000, follow=False,
                        timeout_ms=None):
        if follow:
            raise UnsupportedError('Conversation event follow is not supported; poll with after_seq.')
        _timeout_seconds(timeout_ms)
        session = self.get_session(instance_id)
        engine = self.account(session['account_id']).engine
        events = self.store.events(session_id=instance_id, after=after_seq, limit=limit)
        return [public_event(event, engine) for event in events]

    def turn_events_stream(self, turn_id, *, after_seq=0, timeout_ms=None):
        """Replay then follow saved events until terminal state or idle timeout.

        A caller may close this iterator without cancelling the underlying turn.
        The cursor advances only across events actually yielded to the caller.
        """
        _, cursor = page_values(1, after_seq)
        timeout = _timeout_seconds(timeout_ms)
        run = self.run(turn_id)
        engine = self.account(run.session['account_id']).engine
        while True:
            page = self._turn_event_page(run, cursor, 200, timeout)
            if not page:
                return
            for event in page:
                cursor = event.seq
                yield public_event(event, engine)
