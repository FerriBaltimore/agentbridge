"""Stable error codes across the SDK and JSON API."""


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class BusyError(BridgeError):
    def __init__(self):
        super().__init__("busy", "This session or account already has an active run.")


class UnsupportedError(BridgeError):
    def __init__(self, message: str):
        super().__init__("unsupported", message)
