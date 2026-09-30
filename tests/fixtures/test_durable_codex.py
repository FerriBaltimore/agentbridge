"""Offline native protocol fixture with the pinned rollout and SQLite home layout."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


def main():
    if '--version' in sys.argv:
        print('codex-cli 0.153.0')
        return
    home = Path(os.environ['CODEX_HOME'])
    resumed = 'resume' in sys.argv
    native_id = sys.argv[sys.argv.index('resume') + 1] if resumed else str(uuid4())
    folder = home / 'sessions' / '2026' / '09' / '30'
    folder.mkdir(parents=True, exist_ok=True)
    rollout = folder / ('rollout-fixture-' + native_id + '.jsonl')
    if resumed:
        assert rollout.is_file(), 'Native continuity is required.'
    else:
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': native_id, 'cli_version': '0.153.0'}}) + '\n')
    previous = [json.loads(line)['text'] for line in rollout.read_text().splitlines()[1:]]
    prompt = sys.stdin.read()
    with rollout.open('a') as out:
        out.write(json.dumps({'type': 'message', 'text': prompt}) + '\n')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, rollout_path TEXT)')
        db.execute('INSERT OR REPLACE INTO threads VALUES (?,?)', (native_id, str(rollout)))
    send({'type': 'thread.started', 'thread_id': native_id})
    if prompt.startswith('hold:'):
        until = time.monotonic() + 20
        while not Path(prompt[5:]).exists() and time.monotonic() < until:
            time.sleep(.02)
    send({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps({
        'native_id': native_id, 'previous': previous, 'resumed': resumed})}})
    send({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})


def send(value):
    print(json.dumps(value), flush=True)


if __name__ == '__main__':
    main()
