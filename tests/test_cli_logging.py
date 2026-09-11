import io
import logging
from agent_core.cli_logging import configure_logging, set_notice_sink


def test_destinations_and_idempotence(tmp_path):
    root = logging.getLogger()
    previous, old_level = root.handlers[:], root.level
    # Foreign handlers are retained, not silently disabled.
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    screen = []
    sink = set_notice_sink(screen.append)
    path = tmp_path / 'runtime.log'
    try:
        configure_logging(logging.INFO, path)
        count = len(root.handlers)
        for _ in range(10):
            configure_logging(logging.INFO, path)
        assert len(root.handlers) == count and foreign in root.handlers
        for name in ['BioMind.Performance', 'BioMind.Agent', 'agent_core.provider_attempt', 'agent_core.workflow.typed_node', 'httpx']:
            logging.getLogger(name).info('internal {"payload": "200 prompt accounting phase=state.load"}')
        try:
            raise ValueError('fixture failure')
        except ValueError:
            logging.getLogger('BioMind.Agent').exception('执行失败')
        assert len(screen) == 1 and '执行失败' in screen[0]
        full = path.read_text()
        assert full.count('internal {') == 5
        assert 'Traceback' in full and 'fixture failure' in full
    finally:
        set_notice_sink(sink)
        for h in list(root.handlers):
            if h not in previous:
                root.removeHandler(h); h.close()
        root.setLevel(old_level)


def test_destination_failure_once(tmp_path):
    screen = []
    sink = set_notice_sink(screen.append)
    root = logging.getLogger(); previous = root.handlers[:]; old_level = root.level
    try:
        for _ in range(3):
            configure_logging(logging.INFO, tmp_path / 'missing' / 'x.log')
        assert len(screen) == 1 and '不可写' in screen[0]
        logging.getLogger('BioMind.Agent').error('still visible')
        assert 'still visible' in screen[-1]
    finally:
        set_notice_sink(sink)
        for h in list(root.handlers):
            if h not in previous:
                root.removeHandler(h); h.close()
        root.setLevel(old_level)
