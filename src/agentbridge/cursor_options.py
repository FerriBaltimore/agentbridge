"""Map explicit reasoning effort to a parameter advertised by the Cursor SDK."""
from .errors import BridgeError


REASONING_PARAMETERS = {'effort', 'reasoning_effort', 'reasoningEffort'}


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def model_selection(model_id, effort, *, sdk, api_key, catalog=None):
    """Validate effort before creating an agent; do not choose another model.

    Catalog lookup does not submit an inference request. Unknown parameter
    names and unavailable catalogs fail closed instead of dropping the effort.
    ``catalog`` permits an already observed catalog from the same account.
    """
    if effort is None:
        return model_id
    if not isinstance(model_id, str) or not model_id:
        raise BridgeError('unsupported_parameter', 'Cursor reasoning effort requires an explicit model.')
    if not isinstance(effort, str) or not effort:
        raise BridgeError('unsupported_parameter', 'Cursor reasoning effort must be a non-empty string.')
    if not api_key:
        raise BridgeError('credential_unavailable', 'An explicit Cursor credential is required.')
    if catalog is None:
        try:
            catalog = sdk.Cursor.models.list(api_key=api_key)
        except Exception:
            raise BridgeError('provider_unavailable', 'The Cursor model catalog is unavailable.') from None
    if not isinstance(catalog, (list, tuple)):
        raise BridgeError('provider_protocol_error', 'The Cursor model catalog is invalid.')
    matches = [model for model in catalog if _field(model, 'id') == model_id]
    if len(matches) != 1:
        raise BridgeError('model_unavailable', 'The selected Cursor model is not uniquely present in the account catalog.')
    parameters = _field(matches[0], 'parameters', ())
    if not isinstance(parameters, (list, tuple)):
        raise BridgeError('provider_protocol_error', 'The Cursor model parameters are invalid.')
    if any(not isinstance(_field(parameter, 'id'), str) or not _field(parameter, 'id') for parameter in parameters):
        raise BridgeError('provider_protocol_error', 'The Cursor model parameter identifiers are invalid.')
    parameters = [parameter for parameter in parameters if _field(parameter, 'id') in REASONING_PARAMETERS]
    if len(parameters) != 1:
        raise BridgeError('unsupported_parameter', 'This Cursor model does not advertise a reasoning effort parameter.')
    parameter = parameters[0]
    choices = _field(parameter, 'values', ())
    if not isinstance(choices, (list, tuple)) or effort not in [_field(value, 'value') for value in choices]:
        raise BridgeError('unsupported_parameter', 'This reasoning effort is not advertised for the selected Cursor model.')
    try:
        return sdk.ModelSelection(id=model_id, params=[sdk.ModelParameterValue(id=_field(parameter, 'id'), value=effort)])
    except (AttributeError, TypeError):
        raise BridgeError('unsupported_parameter', 'This Cursor SDK cannot encode model parameters.') from None
