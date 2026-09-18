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
    keys=('PATH','HOME','USER','LOGNAME','LANG','LC_ALL','TMPDIR','SYSTEMROOT','WINDIR','SSL_CERT_FILE','SSL_CERT_DIR')
    return {k:os.environ[k] for k in keys if k in os.environ}
