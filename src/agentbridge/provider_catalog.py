"""Retired direct-provider catalog adapter.

Model discovery is served by verified local proxy observations. The module
remains importable for callers that used the old internal adapter, but it
cannot start a provider process or read native credentials.
"""

from .errors import BridgeError


def models(_account):
    raise BridgeError('unsupported_operation', 'Models must be observed through the local proxy.')


def main():
    raise SystemExit('Direct provider catalog discovery is unavailable.')


if __name__ == '__main__':
    main()
