import json
import sys
from contextlib import contextmanager

import pytest

from agentbridge import RunOptions
from fixtures.test_proxy_worker_fixture import MODEL, bridge_with_proxy, management_server


@contextmanager
def provider(tmp_path, monkeypatch, events, *, stderr='', exit_code=0, delay=0):
    native = tmp_path / 'native.py'
    native.write_text('import json,sys,time\n'
                      f'events = {events!r}\n'
                      'for event in events: print(json.dumps(event), flush=True)\n'
                      f'sys.stderr.write({stderr!r})\n'
                      'sys.stderr.flush()\n'
                      f'time.sleep({delay!r})\n'
                      f'sys.exit({exit_code!r})\n')
    with management_server() as port:
        bridge = bridge_with_proxy(tmp_path, monkeypatch, port,
                                   command=('/usr/bin/python3', str(native)))
        session = bridge.session('fixture', tmp_path, model=MODEL)
        yield bridge, session


@pytest.mark.parametrize(('events', 'code'), [
    ([{'type': 'turn.failed', 'error': {'message': 'secret-provider-body',
                                      'codexErrorInfo': 'usageLimitExceeded'}}], 'quota_exhausted'),
    ([{'type': 'turn.failed', 'error': {'message': 'secret-provider-body This request '
      'was blocked by our safety systems. Reason: Potentially unintended activity.'}}],
     'safety_blocked'),
    ([{'type': 'turn.failed', 'error': {'message': 'secret-provider-body',
                                      'codexErrorInfo': 'sessionBudgetExceeded'}}], 'budget_exhausted'),
])
def test_worker_retains_stable_failure_and_never_raw_error(tmp_path, monkeypatch, events, code):
    with provider(tmp_path, monkeypatch, events) as (bridge, session):
        run = bridge.submit(session['id'], 'test failure', options=RunOptions(timeout=5))
        result = run.wait(10)
        assert (result['state'], result['error']) == ('failed', code)
        observations = list(run.events())
        errors = [event.data for event in observations if event.kind == 'error']
        assert errors[-1]['code'] == code and errors[-1]['terminal'] is True
        assert errors[-1]['retryable'] is False
        assert 'secret-provider-body' not in json.dumps([e.data for e in observations])
        assert run.text == ''


def test_stderr_is_classified_only_for_failure_and_not_persisted(tmp_path, monkeypatch):
    with provider(tmp_path, monkeypatch, [], exit_code=1,
                  stderr='secret-provider-body This request was blocked by our safety systems.') as (bridge, session):
        run = bridge.submit(session['id'], 'fixture', options=RunOptions(timeout=5))
        assert run.wait(10)['error'] == 'safety_blocked'
        data = [event.data for event in run.events()]
        assert 'secret-provider-body' not in json.dumps(data)
        assert any(item.get('content_stored') is False for item in data)


def test_successful_terminal_is_not_overruled_by_unrelated_stderr(tmp_path, monkeypatch):
    with provider(tmp_path, monkeypatch, [{'type': 'turn.completed'}],
                  stderr='fixture diagnostic mentions a rate limit') as (bridge, session):
        run = bridge.submit(session['id'], 'fixture', options=RunOptions(timeout=5))
        assert run.wait(10)['state'] == 'completed'


@pytest.mark.parametrize(('exit_code', 'delay', 'expected'), [(1, 0, 'unknown_outcome'), (0, 2, 'provider_timeout')])
def test_lost_execution_is_unknown_and_never_retried(tmp_path, monkeypatch, exit_code, delay, expected):
    with provider(tmp_path, monkeypatch, [{'type': 'thread.started', 'thread_id': 'fixture-native'}],
                  exit_code=exit_code, delay=delay) as (bridge, session):
        run = bridge.submit(session['id'], 'fixture', options=RunOptions(timeout=.3, stop_grace=.1))
        result = run.wait(10)
        assert (result['state'], result['error']) == ('interrupted', expected)
        issue = [event.data for event in run.events() if event.kind == 'error'][-1]
        assert issue['outcome'] == 'unknown' and issue['retryable'] is False
        bridge.recover()
        assert len(bridge.runs()) == 1
