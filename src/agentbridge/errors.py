"""Stable error codes and safe JSON-RPC error metadata."""


RETRYABLE = {"busy", "provider_timeout", "provider_unavailable", "grantbridge_timeout",
             "store_unavailable", "quota_unknown"}
ACTION = {
    "authentication_required": "login",
    "credential_unavailable": "login",
    "credential_expired": "login",
    "identity_changed": "change_account",
    "identity_missing": "login",
    "authentication_interrupted": "inspect",
    "quota_exhausted": "wait",
    "quota_unknown": "wait",
    "rate_limited": "wait",
    "model_not_found": "change_model",
    "model_unavailable": "change_model",
    "interrupted": "resume",
    "unknown_outcome": "inspect",
    "authentication_attempt_not_ready": "wait",
    "authentication_not_verified": "check",
    "login_timeout": "login",
    "oauth_callback_port_busy": "login",
    "oauth_callback_unavailable": "login",
    "activation_unsupported": "change_account",
    "safety_blocked": "inspect",
    "billing_required": "inspect",
    "context_window_exceeded": "inspect",
    "context_window_unavailable": "inspect",
    "output_limit_exceeded": "resume",
    "budget_exhausted": "inspect",
    "max_turns_exceeded": "resume",
    "structured_output_failed": "inspect",
    "provider_connection_lost": "inspect",
    "provider_contract_unverified": "inspect",
    "provider_contract_changed": "inspect",
    "provider_contract_invalid": "inspect",
    "native_session_missing": "inspect",
    "native_session_diverged": "inspect",
}


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, phase="admission", outcome="not_started",
                 retryable=None, retry_after_ms=None, details=None):
        self.code = code
        self.phase = phase
        self.outcome = outcome
        self.retryable = False if outcome == 'unknown' or code == 'unknown_outcome' else (
            code in RETRYABLE if retryable is None else bool(retryable))
        self.retry_after_ms = retry_after_ms
        self.details = details or {}
        super().__init__(message)

    def safe_data(self):
        category = "validation"
        if (self.code in {"authentication_required", "credential_unavailable", "authorization_denied"}
                or self.code in {'credential_expired', 'identity_changed', 'identity_missing', 'activation_invalid'}
                or self.code.startswith("authentication_") or self.code in {
                    "activation_unsupported", "login_timeout", "oauth_callback_port_busy",
                    "oauth_callback_unavailable"}):
            category = "auth"
        elif self.code in {"quota_exhausted", "quota_unknown", "rate_limited"}:
            category = "quota"
        elif self.code == 'safety_blocked':
            category = 'safety'
        elif self.code == 'billing_required':
            category = 'billing'
        elif self.code in {'context_window_exceeded', 'context_window_unavailable',
                           'output_limit_exceeded',
                           'budget_exhausted', 'max_turns_exceeded', 'structured_output_failed'}:
            category = 'limit'
        elif self.code in {'unknown_outcome', 'reset_outcome_unknown', 'reset_in_progress',
                           'managed_proxy_stop_unverified', 'native_session_missing',
                           'native_session_diverged'}:
            category = 'execution'
        elif self.code == 'provider_catalog_unsupported':
            category = 'capability'
        elif self.code.startswith("provider_") or self.code in {"grantbridge_failed", "grantbridge_timeout"}:
            category = "provider"
        elif self.code in {"busy", "instance_busy", "account_busy"}:
            category = "conflict"
        elif self.code in {"cancelled", "interrupted"}:
            category = "cancel"
        elif self.code == "internal_error":
            category = "internal"
        elif self.code in {'unsupported', 'unsupported_parameter', 'unsupported_operation'}:
            category = 'capability'
        return {
            "code": self.code,
            "category": category,
            "retryable": self.retryable,
            "action": 'inspect' if self.outcome == 'unknown' else ACTION.get(self.code, "none"),
            "phase": self.phase,
            "outcome": self.outcome,
            "retry_after_ms": self.retry_after_ms,
            "details": self.details,
        }


class BusyError(BridgeError):
    def __init__(self):
        super().__init__("busy", "This session or account already has an active run.")


class UnsupportedError(BridgeError):
    def __init__(self, message: str):
        super().__init__("unsupported", message)
