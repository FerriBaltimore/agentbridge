"""Durable run state, event following and explicit recovery."""
import asyncio
import json
import math
import time

from . import usage
from .continuity import unresolved
from .errors import BridgeError, BusyError
from .models import RunOptions, TERMINAL, page_values
from .store import dumps
from .message_projection import text as project_text


def _timeout(value):
    if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                              or not math.isfinite(value) or value < 0):
        raise BridgeError('invalid_timeout', 'Timeout must be finite and nonnegative.')


class Run:
    def __init__(self, bridge, run_id):
        self.bridge, self.id = bridge, run_id

    @property
    def snapshot(self):
        return self.bridge.store.get('runs', self.id)

    @property
    def status(self):
        return self.snapshot['state']

    @property
    def session(self):
        return self.bridge.get_session(self.snapshot['session_id'])

    @property
    def text(self):
        return self.message['text']

    @property
    def message(self):
        return project_text(self._observations())

    @property
    def consumption(self):
        return usage.summarize(self._observations())

    @property
    def subagents(self):
        agents = {}
        for event in self._observations():
            if event.kind == 'subagent':
                agents[event.data['agent_id']] = {**event.data, 'observed_at': event.at}
        return {'coverage': 'partial', 'agents': list(agents.values())}

    def _observations(self):
        """Internal readers consume every page; public events remain bounded."""
        after = 0
        while True:
            page = self.bridge.store.events(run_id=self.id, after=after, limit=1000)
            if not page:
                return
            yield from page
            after = page[-1].seq

    def events(self, *, after=0, limit=1000, follow=False, timeout=None):
        limit, after = page_values(limit, after)
        _timeout(timeout)
        started = time.monotonic()
        remaining = limit
        while True:
            batch = self.bridge.store.events(run_id=self.id, after=after, limit=remaining)
            for event in batch:
                after = event.seq
                yield event
            remaining -= len(batch)
            if remaining == 0:
                return
            if batch:
                continue
            if not follow:
                return
            self.bridge.recover(turn_id=self.id)
            if self.status in TERMINAL:
                yield from self.bridge.store.events(run_id=self.id, after=after, limit=remaining)
                return
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError('Event wait timed out; the run remains active.')
            time.sleep(0.05)

    async def aevents(self, *, after=0, timeout=None):
        _, after = page_values(1, after)
        _timeout(timeout)
        started = time.monotonic()
        while True:
            batch = await asyncio.to_thread(
                lambda: self.bridge.store.events(run_id=self.id, after=after))
            for event in batch:
                after = event.seq
                yield event
            if batch:
                continue
            await asyncio.to_thread(self.bridge.recover, turn_id=self.id)
            if await asyncio.to_thread(lambda: self.status in TERMINAL):
                events = await asyncio.to_thread(
                    lambda: self.bridge.store.events(run_id=self.id, after=after))
                for event in events:
                    yield event
                return
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError('Event wait timed out; the run remains active.')
            await asyncio.sleep(0.05)

    def wait(self, timeout=None):
        _timeout(timeout)
        started = time.monotonic()
        while self.status not in TERMINAL:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError('Wait timed out; the run remains active.')
            self.bridge.recover(turn_id=self.id)
            time.sleep(0.05)
        self.bridge.close()
        return self.snapshot

    async def await_result(self, timeout=None):
        return await asyncio.to_thread(self.wait, timeout)

    def stop(self, *, wait=False, timeout=15):
        self.bridge.store.stop(self.id)
        return self.wait(timeout) if wait else self.snapshot

    def resume(self, prompt=None, *, options=None, request_key=None):
        if self.status not in TERMINAL:
            raise BusyError()
        row = self.snapshot
        pending = unresolved(list(self._observations()))
        if prompt is None:
            prompt = ('Continue the interrupted work. Verify effects before repeating them. '
                      'Original request:\n' + row['prompt'])
        if pending:
            prompt += '\nUnknown previous outcomes:\n' + dumps(pending)
        if not self.session['native_id']:
            bundle = self.bridge.export_context(row['session_id'])
            prompt = bundle.text + '\n\n' + prompt
        return self.bridge.submit(
            row['session_id'], prompt,
            options=options or RunOptions(**json.loads(row['options'])),
            request_key=request_key)
