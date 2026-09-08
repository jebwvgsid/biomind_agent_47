"""Keep two legacy in-memory binder assertions while publishing into real temp store."""
import pytest
from tests import test_recovery_projection as recorded

@pytest.mark.parametrize('name', [
    'test_binder_persists_install_packet_for_resume',
    'test_completion_failure_packet_carries_step_archive_inspection_ref',
])
def test_legacy_projection_assertions_with_isolated_real_object_store(tmp_path, monkeypatch, name):
    original = recorded.CausalFailurePacketBinder
    destinations = []
    def isolated_binder(*args, **kwargs):
        assert not args and kwargs['data_path'] == '/fixture'
        destinations.append(str(tmp_path))
        return original(**{**kwargs, 'data_path': str(tmp_path)})
    monkeypatch.setattr(recorded, 'CausalFailurePacketBinder', isolated_binder)
    case = recorded.RecoveryProjectionTests(methodName=name)
    case.setUp()
    try:
        getattr(case, name)()
    finally:
        case.tearDown()
    assert destinations
    assert list(tmp_path.rglob('state_contract/objects/*.json'))
