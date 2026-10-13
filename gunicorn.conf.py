"""Gunicorn settings: several worker processes, each with a few threads."""

import os

bind = "0.0.0.0:5441"
workers = int(os.getenv("GUNICORN_WORKERS", "4"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = 120  # above the LLM client's 45 s request deadline


def on_starting(_server):
    """Create MongoDB indexes once, in the master process."""
    from database.connection import init_db  # pylint: disable=import-outside-toplevel

    init_db()
