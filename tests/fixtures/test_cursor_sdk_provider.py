"""Subprocess fixture for the actual Cursor worker with an isolated SDK double."""
from dataclasses import dataclass
from types import SimpleNamespace
import json
import sys

from agentbridge.cursor_worker import execute


@dataclass
class Options:
    model: str
    api_key: str
    local: object
    tools: object


class Agent:
    agent_id = 'fixture-cursor-session'

    @classmethod
    def create(cls, options):
        assert options.api_key == 'fixture-key-never-real'
        assert options.tools == []
        return cls()

    @classmethod
    def resume(cls, native_id, options):
        assert native_id == cls.agent_id
        return cls.create(options)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, prompt):
        return self

    def messages(self):
        yield {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'fixture-key-never-real'}]}}

    def wait(self):
        return SimpleNamespace(status='finished')


sdk = SimpleNamespace(AgentOptions=Options, LocalAgentOptions=lambda **values: values, Agent=Agent)
if __name__ == '__main__':
    execute(json.load(sys.stdin), lambda event: print(json.dumps(event), flush=True), sdk=sdk)
