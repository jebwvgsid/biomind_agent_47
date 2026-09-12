"""Opt-in fatal outlet for guarded synthetic children; independent of capture.

Imported only after the original bootstrap imports pytest. Keep the descriptor
open until OS process exit, including after pytest unconfigure. The pre-existing
70-second dump_traceback_later destination and schedule are left alone.
"""
import faulthandler
import json
import os
from pathlib import Path
import resource
import stat

import pytest


class PersistentFatal:
    def __init__(self, path):
        path = Path(path).resolve()
        results = Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']).resolve()
        if path.parent != results or path.name != 'fatal.txt':
            raise ValueError('Fatal outlet must belong to this isolated result directory')
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(self.fd, 0o600)
        assert self.fd not in (0, 1, 2)
        assert stat.S_IMODE(os.fstat(self.fd).st_mode) == 0o600
        # Only this new, synthetic process: never create a raw memory/core artifact.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        self.events = results / 'fatal-outlet.json'
        self.records = []

    def arm(self, phase, config):
        faulthandler.enable(file=self.fd, all_threads=True)
        row = {'phase': phase, 'pid': os.getpid(), 'fd': self.fd,
               'enabled': faulthandler.is_enabled(), 'mode': '0600',
               'capture': config.getoption('capture'),
               'pytest_faulthandler_plugin': config.pluginmanager.hasplugin('faulthandler'),
               'watchdog': 'original bootstrap 70s; separate pre-existing destination',
               'core_limit': list(resource.getrlimit(resource.RLIMIT_CORE))}
        os.write(self.fd, ('FATAL_OUTLET ' + json.dumps(row) + '\n').encode())
        os.fsync(self.fd)
        self.records.append(row)
        self.events.write_text(json.dumps(self.records, indent=2))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self, session):
        # All pytest_configure handlers (including builtin faulthandler's dup
        # of captured stderr) have completed before this hook.
        self.arm('sessionstart_after_configure', session.config)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_unconfigure(self, config):
        try:
            yield
        finally:
            # Builtin pytest unconfigure disables/restores its own handler.
            # Re-arm afterward; deliberately do not close the raw descriptor.
            self.arm('after_unconfigure', config)
