"""Authenticated bounded IPC for the private proxy supervisor."""

import json

from ..errors import BridgeError
from .supervisor_auth import authorized, same_user_pid
from .credential_writer import request as writer_request

MAX_REQUEST = 4096
MAX_RESPONSE = 16 * 1024


def _serve_connection(connection, supervisor):
    with connection:
        try:
            same_user_pid(connection)
            connection.settimeout(45)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_REQUEST + 1)
            if not line or len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise BridgeError("invalid_request", "The managed proxy request is invalid.")
            request = json.loads(line)
            if request == {"action": "auth_info"}:
                response = {"ok": True, "result": {"auth_fd": supervisor.auth_fd}}
            elif (not isinstance(request, dict) or
                    set(request) not in ({"action", "account_id", "auth"},
                                         {"action", "account_id", "base_url", "auth"},
                                         {"action", "account_id", "capture_id", "auth"})):
                raise BridgeError("invalid_request", "The managed proxy request is invalid.")
            else:
                if not authorized(request['auth'], supervisor.auth_token):
                    raise BridgeError('managed_proxy_auth_required',
                                      'The local supervisor authority is unavailable.')
                if request['action'] in {'credential_snapshot', 'credential_revision'}:
                    if ('base_url' in request or ('capture_id' in request) !=
                            (request['action'] == 'credential_snapshot')):
                        raise BridgeError('invalid_request', 'Invalid credential writer request.')
                    result = writer_request(supervisor, request['action'], request['account_id'],
                                            request.get('capture_id'))
                else:
                    if 'capture_id' in request:
                        raise BridgeError('invalid_request', 'Invalid managed proxy request.')
                    result = supervisor.route(request["action"], request["account_id"],
                                              request.get("base_url"))
                response = {"ok": True, "result": result}
        except BridgeError as error:
            response = {"ok": False, "error": {"code": error.code, "message": str(error)}}
        except (OSError, TypeError, ValueError, KeyError):
            response = {"ok": False, "error": {"code": "managed_proxy_unavailable",
                                                "message": "The managed proxy request failed."}}
        payload = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(payload) <= MAX_RESPONSE:
            try:
                connection.sendall(payload)
            except OSError:
                pass

