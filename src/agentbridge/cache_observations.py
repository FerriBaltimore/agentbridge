"""Conservative cache telemetry from persisted native usage observations."""


def _counter(tokens, key):
    value = tokens.get(key)
    return value if type(value) is int and value >= 0 else None


def project(observation):
    """Keep native reports separate from verified upstream cache accounting.

    Codex and provider translators may synthesize zero for absent counters.
    A native zero cannot establish a cache miss or a free cache write. Session
    totals and latest observations overlap, so this projection never adds them.
    """
    tokens = observation.get('tokens')
    tokens = tokens if isinstance(tokens, dict) else {}
    reads = _counter(tokens, 'cached_input_tokens')
    writes = _counter(tokens, 'cache_write_input_tokens')
    return {
        'read_tokens_reported': reads,
        'write_tokens_reported': writes,
        'source': observation.get('source'),
        'scope': observation.get('scope'),
        'aggregation': observation.get('aggregation'),
        'upstream_verified': False,
        'zero_is_conclusive': False,
        'cost': None,
        'reason': ('not_reported' if reads is None and writes is None
                   else 'native_report_only'),
    }
