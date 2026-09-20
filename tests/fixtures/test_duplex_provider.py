"""Native control protocol fixture. No provider, credentials or network."""
import json
import os
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
