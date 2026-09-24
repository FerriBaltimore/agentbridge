"""SSE transport for the SDK's normalized, durable turn event stream."""

import json
import time

from agentbridge import BridgeError


STREAM_SECONDS = 30
IDLE_TIMEOUT_MS = 3000
TERMINAL_STATES = {'completed', 'failed', 'cancelled', 'interrupted', 'incomplete'}


def _event_bytes(event):
    data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return f'id: {event["seq"]}\ndata: {data}\n\n'.encode('utf-8')


def _write(handler, payload):
    handler.wfile.write(payload)
    handler.wfile.flush()


def _stream_error(code):
    data = json.dumps({'code': code}, separators=(',', ':'))
    return f'event: stream.error\ndata: {data}\n\n'.encode('utf-8')


def last_event_seq(value):
    if value is None or value == '':
        return 0
    try:
        seq = int(value)
    except (TypeError, ValueError):
        raise BridgeError('invalid_request', 'Last-Event-ID must be a nonnegative integer.') from None
    if not 0 <= seq <= 1_000_000_000:
        raise BridgeError('invalid_request', 'Last-Event-ID must be a nonnegative integer.')
    return seq


def serve_turn_stream(handler, bridge, turn_id, after_seq):
    """Send ordered public events; closing this connection never cancels a turn."""
    bridge.turn(turn_id)
    handler.send_response(200)
    for name, value in (
        ('Content-Type', 'text/event-stream; charset=utf-8'),
        ('Cache-Control', 'no-cache, no-store'),
        ('X-Content-Type-Options', 'nosniff'),
        ('X-Frame-Options', 'DENY'),
        ('Referrer-Policy', 'no-referrer'),
    ):
        handler.send_header(name, value)
    handler.end_headers()
    deadline = time.monotonic() + STREAM_SECONDS
    try:
        _write(handler, b'retry: 1000\n\n')
        while time.monotonic() < deadline:
            for event in bridge.turn_events_stream(
                turn_id, after_seq=after_seq, timeout_ms=IDLE_TIMEOUT_MS
            ):
                after_seq = event['seq']
                _write(handler, _event_bytes(event))
            if bridge.turn(turn_id)['state'] in TERMINAL_STATES:
                _write(handler, b'event: end\ndata: {}\n\n')
                return
            _write(handler, b': keepalive\n\n')
    except (BrokenPipeError, ConnectionResetError):
        return
    except BridgeError as error:
        try:
            _write(handler, _stream_error(error.code))
        except (BrokenPipeError, ConnectionResetError):
            pass
    except Exception:
        try:
            _write(handler, _stream_error('internal_error'))
        except (BrokenPipeError, ConnectionResetError):
            pass
