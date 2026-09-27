"""Context package byte bounds count UTF-8 bytes of content and canonical JSON."""

from hashlib import sha256

import pytest

from agentbridge.context_package import (
    MAX_EVIDENCE_BYTES, MAX_INSTRUCTION_BYTES, MAX_PACKAGE_BYTES, canonical, digest, validate,
)
from agentbridge.errors import BridgeError


def _rule(instance_id, content):
    return {'instance_id': instance_id, 'revision': 1, 'kind': 'rule', 'scope': 'mission',
            'assets': [{'path': 'RULE.md', 'content': content,
                        'digest': sha256(content.encode('utf-8')).hexdigest()}]}


def _package(instructions, *, tools=()):
    selection = {'context_refs': [], 'instructions': [
        {**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
                            for asset in item['assets']]} for item in instructions],
        'exclusions': [], 'tools': list(tools)}
    return {'version': 2, 'selection_hash': digest(selection), 'instructions': instructions,
            'exclusions': [], 'evidence': [], 'tools': list(tools)}


def test_limits_hold_a_one_mebibyte_instruction_catalogue():
    assert MAX_INSTRUCTION_BYTES == 1_048_576
    assert MAX_PACKAGE_BYTES == 1_572_864
    assert MAX_EVIDENCE_BYTES == 65_536
    assert MAX_PACKAGE_BYTES > MAX_INSTRUCTION_BYTES + MAX_EVIDENCE_BYTES


def test_instruction_content_accepts_exactly_the_bound_and_rejects_one_more_byte():
    half = MAX_INSTRUCTION_BYTES // 2
    package = _package([_rule('rule-a', 'a' * half), _rule('rule-b', 'b' * half)])
    assert validate(package) is package
    over = _package([_rule('rule-a', 'a' * half), _rule('rule-b', 'b' * (half + 1))])
    with pytest.raises(BridgeError) as error:
        validate(over)
    assert error.value.code == 'invalid_context'


def test_instruction_bytes_are_utf8_not_code_points():
    # Two-byte characters: half as many code points reach the same byte bound.
    exact = _package([_rule('rule-utf8', 'é' * (MAX_INSTRUCTION_BYTES // 2))])
    assert validate(exact) is exact
    over = _package([_rule('rule-utf8', 'é' * (MAX_INSTRUCTION_BYTES // 2 + 1))])
    with pytest.raises(BridgeError) as error:
        validate(over)
    assert error.value.code == 'invalid_context'


def test_package_bound_applies_to_canonical_json_beyond_instruction_content():
    # Full instruction content plus a tool record whose bytes only the package bound counts.
    half = MAX_INSTRUCTION_BYTES // 2
    instructions = [_rule('rule-a', 'a' * half), _rule('rule-b', 'b' * half)]
    probe = _package(instructions, tools=({'name': 'fixture-tool', 'schema': ''},))
    room = MAX_PACKAGE_BYTES - len(canonical(probe).encode('utf-8'))
    assert room > 0
    fitting = _package(instructions, tools=({'name': 'fixture-tool', 'schema': 'x' * room},))
    assert len(canonical(fitting).encode('utf-8')) == MAX_PACKAGE_BYTES
    assert validate(fitting) is fitting
    over = _package(instructions, tools=({'name': 'fixture-tool', 'schema': 'x' * (room + 1)},))
    assert len(canonical(over).encode('utf-8')) == MAX_PACKAGE_BYTES + 1
    with pytest.raises(BridgeError) as error:
        validate(over)
    assert error.value.code == 'invalid_context'
