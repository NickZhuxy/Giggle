"""Gunicorn settings: several worker processes, each with a few threads."""

import os
import shutil

# Prometheus multiprocess mode: every worker writes metrics files here.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/giggle-prometheus")

bind = "0.0.0.0:5441"
workers = int(os.getenv("GUNICORN_WORKERS", "4"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = 120  # above the LLM client's 45 s request deadline


def on_starting(_server):
    """Reset the metrics directory and create MongoDB indexes, once."""
    from database.connection import init_db  # pylint: disable=import-outside-toplevel

    metrics_dir = os.environ["PROMETHEUS_MULTIPROC_DIR"]
    shutil.rmtree(metrics_dir, ignore_errors=True)
    os.makedirs(metrics_dir)
    init_db()


def child_exit(_server, worker):
    """Drop the live-gauge files of a worker that exited."""
    from prometheus_client import (
        multiprocess,
    )  # pylint: disable=import-outside-toplevel

    multiprocess.mark_process_dead(worker.pid)
