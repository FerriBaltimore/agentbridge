"""Native control protocol fixture. No provider, credentials or network."""
import json
import os
from pathlib import Path
import signal
import sys


def main():
    if '--ignore-term' in sys.argv:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    def send(value):
        print(json.dumps(value), flush=True)
    first = json.loads(sys.stdin.readline())
    if first.get('method') == 'initialize':
        send({'id': first['id'], 'result': {}})
        json.loads(sys.stdin.readline())  # initialized notification
        start = json.loads(sys.stdin.readline())
        if start.get('method') == 'skills/list':
            selected = ([] if '--missing-skills' in sys.argv else
                list(Path(os.environ['CODEX_HOME']).glob('skills/agentbridge-context-*/**/SKILL.md')))
            send({'id': start['id'], 'result': {'data': [{'cwd': start['params']['cwds'][0], 'skills': [
                *[{'path': str(path), 'name': 'fixture-skill'} for path in selected],
                {'path': '/fixtures/unselected/SKILL.md', 'name': 'unselected'}], 'errors': []}]}})
            start = json.loads(sys.stdin.readline())
        policy = start['params']['approvalPolicy']
        send({'id': start['id'], 'result': {'thread': {'id': 'native-fixture'}}})
        turn = json.loads(sys.stdin.readline())
        inputs = turn['params']['input']
        text = inputs[0]['text']
        picture = any(item['type'] == 'image' and item['url'].startswith('data:image/png;base64,') for item in inputs)
        send({'id': turn['id'], 'result': {'turn': {'id': 'native-turn'}}})
        decision = 'no_permission'
        if policy != 'never':
            send({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval', 'params': {
                'threadId': 'native-fixture', 'turnId': 'native-turn', 'itemId': 'command-1',
                'command': 'test-only', 'reason': str(os.getpid())}})
            response = json.loads(sys.stdin.readline())
            decision = response['result']['decision']
        result = f'{decision}|image={picture}|{text}'
        if text == 'inspect-context':
            config = start['params'].get('config', {})
            capability = os.environ.get('AGENTBRIDGE_MCP_CAPABILITY', '')
            assert not capability or capability not in json.dumps(start['params'])
            result = json.dumps({
                'developer': 'selected fixture rule' in start['params'].get('developerInstructions', ''),
                'evidence': any(item.get('type') == 'text' and 'UNTRUSTED SOURCE EVIDENCE'
                                in item.get('text', '') for item in inputs[1:]),
                'skill': any(item.get('type') == 'skill' for item in inputs[1:]),
                'mcp': 'agentbridge_execution' in config.get('mcp_servers', {}),
                'shell_disabled': config.get('features', {}).get('shell_tool') is False,
                'skills_isolated': any(item['path'] == '/fixtures/unselected/SKILL.md'
                                       and item['enabled'] is False
                                       for item in config.get('skills.config', [])),
                'project_docs_disabled': config.get('project_doc_max_bytes') == 0,
                'web_disabled': config.get('web_search') == 'disabled',
                'ephemeral': start['params'].get('ephemeral') is True,
            })
        send({'method': 'item/completed', 'params': {'threadId': 'native-fixture',
              'item': {'id': 'message-1', 'type': 'agentMessage', 'text': result}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'native-fixture',
              'turn': {'id': 'native-turn', 'status': 'completed'}}})
    else:
        send({'type': 'control_response', 'response': {'request_id': first['request_id'],
              'subtype': 'success', 'response': {'models': [{'value': 'claude-fixture', 'displayName': 'Fixture'}]}}})
        line = sys.stdin.readline()
        if not line:
            return  # model catalogue probe never submits a user message
        message = json.loads(line)
        content = message['message']['content']
        text = content[0]['text']
        picture = any(item['type'] == 'image' and item['source']['media_type'] == 'image/png' for item in content)
        send({'type': 'system', 'subtype': 'init', 'session_id': 'native-fixture'})
        send({'type': 'control_request', 'request_id': 'approval-1', 'request': {
            'subtype': 'can_use_tool', 'tool_name': 'Write', 'input': {'file_path': 'test-only', 'fixture_pid': os.getpid()}}})
        response = json.loads(sys.stdin.readline())['response']['response']
        if response['behavior'] == 'allow':
            assert response['updatedInput'] == {'file_path': 'test-only', 'fixture_pid': os.getpid()}
        result = f"{response['behavior']}|image={picture}|{text}"
        send({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': result}]}})
        send({'type': 'result', 'subtype': 'success', 'is_error': False})


if __name__ == '__main__':
    main()
