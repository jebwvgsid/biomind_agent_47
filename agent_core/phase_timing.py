"""Best-effort local timings; never load state or publish business events."""
from contextlib import contextmanager
from functools import wraps
import logging
from time import perf_counter_ns

logger = logging.getLogger("BioMind.Performance")


def phase_started(stage="", *, output=None, message=""):
    started = perf_counter_ns()
    try:
        if stage:
            logger.info("phase=%s status=started", stage)
        if output is not None and message:
            output.print_once(f"phase:{stage}:{started}", message, purpose="telemetry")
    except Exception:
        pass
    return started


def phase_finished(stage, started, *, status="completed"):
    # Logging handlers are integrations too: their failure must not change the
    # measured operation's result, exception, or control flow.
    try:
        logger.info("phase=%s status=%s elapsed_ns=%d", stage, status,
                    perf_counter_ns() - started)
    except Exception:
        pass


@contextmanager
def phase(stage, *, output=None, message=""):
    started = phase_started(stage, output=output, message=message)
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
