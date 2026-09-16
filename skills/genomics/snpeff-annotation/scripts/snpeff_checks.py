"""Small build-receipt projection. No command dispatch or acceptance policy."""
from pathlib import Path
import re


def build_facts(*, returncode, predictor, load_returncode, log):
    """Keep process, materialization, load and quality observations independent.

    load_returncode=None means no load receipt. Quality categories can overlap;
    this function does not invent a threshold or authorize skipping validation.
    """
    path = Path(predictor)
    try:
        size = path.stat().st_size if path.is_file() else None
        error = None
    except OSError as exc:
        size, error = None, type(exc).__name__ + ': ' + str(exc)
    lines = str(log).splitlines()
    fatal = [line for line in lines if re.search(
        r'\bFATAL\b|Database check failed|Exception in thread', line, re.I)]
    quality = [line for line in lines if re.search(
        r'Total Errors|\bCDS\b|\bSTART\b|\bSTOP\b|warning|skip', line, re.I)]
    return {'process_returncode': returncode, 'process_passed': type(returncode) is int and returncode == 0,
            'predictor': str(path), 'predictor_size': size, 'predictor_error': error,
            'predictor_present_nonempty': size is not None and size > 0,
            'load_returncode': load_returncode, 'load_status': 'NOT_RUN' if load_returncode is None else
                'passed' if type(load_returncode) is int and load_returncode == 0 else 'failed',
            'fatal_lines': fatal, 'quality_lines': quality, 'quality_acceptance': 'REQUIRES_PLAN_AND_REVIEW'}
