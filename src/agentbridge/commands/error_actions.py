"""Explicit diagnosis and reviewed classification commands, with JSON results."""
from .help import add_parser


def add_errors(sub):
    errors = add_parser(sub, 'errors', help='Inspect unknown errors and review learned classifications')
    commands = errors.add_subparsers(dest='errors_command')
    cases = add_parser(commands, 'cases', help='List stored safe error cases as a bounded JSON page')
    cases.add_argument('--limit', type=int, default=100)
    cases.add_argument('--cursor', type=int, default=0)
    case = add_parser(commands, 'case', help='Read one safe error case')
    case.add_argument('case_id', metavar='CASE')
    diagnose = add_parser(commands, 'diagnose', help='Run one explicit tool-free Cursor diagnosis, which may incur usage')
    diagnose.add_argument('case_id', metavar='CASE')
    diagnose.add_argument('--account-ref', required=True, help='Explicit bound Cursor account for diagnosis')
    diagnose.add_argument('--model', required=True, help='Explicit diagnostic model')
    diagnose.add_argument('--idempotency-key', required=True, help='Stable request key; repeats never launch another diagnosis')
    diagnose.add_argument('--timeout', type=float, default=60, help='Elapsed-time limit in seconds, 1-120 (default: 60)')
    diagnosis = add_parser(commands, 'diagnosis', help='Read a saved diagnostic operation without rerunning it')
    diagnosis.add_argument('idempotency_key', metavar='KEY')
    proposal = add_parser(commands, 'proposal', help='Read a candidate classification and its review revision')
    proposal.add_argument('proposal_id', metavar='ID')
    validate = add_parser(commands, 'validate', help='Check classification invariants without activating a rule')
    validate.add_argument('proposal_id', metavar='ID')
    activate = add_parser(commands, 'activate', help='Explicitly activate a reviewed, validated proposal')
    activate.add_argument('proposal_id', metavar='ID')
    activate.add_argument('--expected-revision', required=True, type=int, help='Proposal revision that was reviewed')
    deactivate = add_parser(commands, 'deactivate', help='Explicitly disable a learned rule while preserving its audit')
    deactivate.add_argument('rule_id', metavar='ID')
    deactivate.add_argument('--expected-revision', required=True, type=int, help='Current reviewed rule revision')
    rule = add_parser(commands, 'rule', help='Read a learned rule, revision and state')
    rule.add_argument('rule_id', metavar='ID')
    return errors


def error_command(bridge, args):
    command = args.errors_command
    if command == 'cases':
        return bridge.error_cases(limit=args.limit, cursor=args.cursor)
    if command == 'case':
        return bridge.error_case(args.case_id)
    if command == 'diagnose':
        return bridge.error_diagnose(args.case_id, account_ref=args.account_ref,
            model=args.model, idempotency_key=args.idempotency_key, timeout=args.timeout)
    if command == 'diagnosis':
        return bridge.error_diagnosis(args.idempotency_key)
    if command == 'proposal':
        return bridge.error_proposal(args.proposal_id)
    if command == 'validate':
        return bridge.error_validate(args.proposal_id)
    if command == 'activate':
        return bridge.error_activate(args.proposal_id, args.expected_revision)
    if command == 'deactivate':
        return bridge.error_deactivate(args.rule_id, args.expected_revision)
    if command == 'rule':
        return bridge.error_rule(args.rule_id)
    raise ValueError('Unknown error command.')
