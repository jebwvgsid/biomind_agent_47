"""Offline A/B/C target suite, as a file so multiprocessing spawn can reopen it."""
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO/'integrations/pi_runtime/tests'),
               str(REPO/'tests'), str(REPO/'integrations/pi_runtime')]

if __name__ == '__main__':
    # The existing fixture installs the no-provider/config import guard first.
    import test_external_access_integration
    modules = ['test_registry_atomic_20261002', 'test_install_targets_20261002',
               'test_runtime_candidates_20261002', 'test_scoped_launch_20261002',
               'test_environment_binding_20261002', 'test_main_request_admission_20261002',
               'test_diagnostic_budget_20261002', 'test_scientific_delivery_20261002',
               'test_runtime_diagnostics_20261002']
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
