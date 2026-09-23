"""Allowlisted model controls from the local proxy's client catalog."""

from ..errors import BridgeError
from ..models import model_id


_EFFORTS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})
_MODALITIES = frozenset({'text', 'image', 'audio', 'video'})


def _positive_window(value):
    return value if type(value) is int and 0 < value <= 10_000_000 else None


def catalog_metadata(payload):
    """Discard everything except typed, bounded controls for model IDs."""
    entries = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(entries, list) or len(entries) > 500:
        return {}
    result = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            identifier = model_id(raw.get('slug') or raw.get('id'))
        except BridgeError:
            continue
        window = _positive_window(raw.get('context_window'))
        levels = raw.get('supported_reasoning_levels')
        efforts = []
        if isinstance(levels, list) and len(levels) <= 20:
            for level in levels:
                value = level.get('effort') if isinstance(level, dict) else level
                if isinstance(value, str) and value in _EFFORTS and value not in efforts:
                    efforts.append(value)
        default = raw.get('default_reasoning_level')
        if default not in efforts:
            default = None
        modalities = raw.get('input_modalities')
        modalities = ([value for value in modalities
                       if isinstance(value, str) and value in _MODALITIES]
                      if isinstance(modalities, list) and len(modalities) <= 20 else [])
        result[identifier] = {
            'reasoning_efforts': efforts,
            'default_reasoning_effort': default,
            'context_windows': [window] if window is not None else [],
            'input_modalities': list(dict.fromkeys(modalities)),
        }
    return result
