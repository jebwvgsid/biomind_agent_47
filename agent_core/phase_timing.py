"""Best-effort local timings; never load state or publish business events."""
from contextlib import contextmanager
from functools import wraps
import logging
from time import perf_counter_ns

logger = logging.getLogger("BioMind.Performance")


def phase_started():
    return perf_counter_ns()


def phase_finished(stage, started, *, status="completed"):
    # Logging handlers are integrations too: their failure must not change the
    # measured operation's result, exception, or control flow.
    try:
        logger.info("phase=%s status=%s elapsed_ns=%d", stage, status,
                    perf_counter_ns() - started)
    except Exception:
        pass


@contextmanager
def phase(stage):
    started = phase_started()
    status = "failed"
    try:
        yield
        status = "completed"
    finally:
        phase_finished(stage, started, status=status)


def timed_phase(stage):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            with phase(stage):
                return function(*args, **kwargs)
        return measured
    return decorate
