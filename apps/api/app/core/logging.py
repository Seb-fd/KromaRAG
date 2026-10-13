"""Structured logging (JSON to stdout for Cloud Run). Never log secrets."""
import logging
import re
import sys

_DSN_RE = re.compile(r"://[^/\s]*:[^/\s]*@")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key|bearer|clerk[_-]?secret|sk_(live|test)_)[^\s\"']*")


def _scrub(msg: str) -> str:
    try:
        s = _DSN_RE.sub("://[redacted]@", msg)
        return _API_KEY_RE.sub("[redacted-key]", s)
    except Exception:
        return msg


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            args = record.args
            if isinstance(args, dict):
                record.args = {k: (_scrub(v) if isinstance(v, str) else v) for k, v in args.items()}
            elif isinstance(args, tuple):
                record.args = tuple(_scrub(a) if isinstance(a, str) else a for a in args)
        except Exception:
            pass
        return True


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('{"level":"%(levelname)s","msg":"%(message)s","logger":"%(name)s"}'))
    h.addFilter(_ScrubFilter())
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)
