import io
import logging
from agent_core.cli_logging import configure_logging, set_notice_sink




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


def test_debug_toggle_stays_file_only(tmp_path):
    root=logging.getLogger();previous=root.handlers[:];old_level=root.level
    screen=[];sink=set_notice_sink(screen.append);path=tmp_path/'trace.log'
    try:
        for level in [logging.INFO,logging.DEBUG,logging.INFO,logging.DEBUG]:
            configure_logging(level,path)
        logging.getLogger('BioMind.Performance').debug('trace fixture')
        assert not screen and path.read_text().count('trace fixture')==1
        assert len([h for h in root.handlers if getattr(h,'_biomind_owner','')=='biomind.cli'])==2
    finally:
        set_notice_sink(sink)
        for h in list(root.handlers):
            if h not in previous:root.removeHandler(h);h.close()
        root.setLevel(old_level)
