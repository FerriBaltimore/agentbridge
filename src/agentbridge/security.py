"""Minimize credential exposure. This is not an OS sandbox."""
import os
import re

SENSITIVE = re.compile(r'^(api[-_]?key|access[-_]?token|refresh[-_]?token|authorization|password|secret|id_token)$',re.I)


class Redactor:
    def __init__(self, values=()):
        self.values=tuple(sorted({v for v in values if isinstance(v,str) and len(v)>=4},key=len,reverse=True))

    def clean(self, value):
        if isinstance(value,dict):
            return {str(k):'[redacted]' if SENSITIVE.match(str(k)) else self.clean(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [self.clean(v) for v in value]
        if isinstance(value,str):
            for secret in self.values:value=value.replace(secret,'[redacted]')
        return value


def base_environment():
    # Avoid inheriting unrelated provider credentials, injected MCPs and active chat state.
    # Host proxy settings stay: sandboxed hosts only reach providers through them, and the
    # per-run worker is what launches the managed proxy supervisor and sidecar.
    keys=('PATH','HOME','USER','LOGNAME','LANG','LC_ALL','TMPDIR','SYSTEMROOT','WINDIR','SSL_CERT_FILE','SSL_CERT_DIR',
          'HTTP_PROXY','HTTPS_PROXY','NO_PROXY','ALL_PROXY','http_proxy','https_proxy','no_proxy','all_proxy')
    return {k:os.environ[k] for k in keys if k in os.environ}
