"""Conversation queue CLI backed exclusively by the public SDK methods."""

from .help import add_parser


def add_queues(sub):
    queues = add_parser(sub, 'queues', help='List, edit and dispatch persistent conversation input')
    commands = queues.add_subparsers(dest='queues_command', required=True)
    for name, description in (
        ('list', 'List pending messages in execution order'),
        ('add', 'Persist a message for automatic delivery'),
        ('move', 'Move a pending message to a zero-based position'),
        ('delete', 'Remove a pending message without executing it'),
        ('dispatch', 'Steer the active turn or interrupt it for a pending message'),
        ('pause', 'Pause automatic dispatch; keep the active turn running'),
        ('resume', 'Resume pending messages without repeating dispatched work'),
    ):
        parser = add_parser(commands, name, help=description)
        parser.add_argument('instance_id')
        if name in ('move', 'delete', 'dispatch'):
            parser.add_argument('message_id')
        if name != 'list':
            parser.add_argument('--expected-version', type=int)
        if name == 'list':
            parser.add_argument('--limit', type=int, default=100)
            parser.add_argument('--cursor', type=int, default=0)
        if name == 'add':
            parser.add_argument('content')
            parser.add_argument('--position', type=int)
            parser.add_argument('--idempotency-key')
        if name == 'move':
            parser.add_argument('--position', type=int, required=True)
        if name == 'dispatch':
            parser.add_argument('--mode', choices=('steer', 'interrupt'), required=True)
            parser.add_argument('--expected-turn-id')


def queue_command(bridge, args):
    values = vars(args).copy()
    method = values.pop('queues_command')
    values.pop('action')
    values.pop('root')
    return getattr(bridge, 'queue_' + method)(**values)
