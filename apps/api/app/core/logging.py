"""Structured logging (JSON to stdout for Cloud Run)."""
import logging, sys


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter('{"level":"%(levelname)s","msg":"%(message)s","logger":"%(name)s"}'))
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)
