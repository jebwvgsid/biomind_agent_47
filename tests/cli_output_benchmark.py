"""Same telemetry fixture through baseline and candidate logging handlers."""
import ast
from contextlib import redirect_stderr
from datetime import datetime
import io
import json
import logging
from pathlib import Path
import sys
from typing import Optional
from agent_core.cli_logging import configure_logging

baseline=Path(sys.argv[1]); result_path=Path(sys.argv[2]); folder=result_path.parent
source=ast.parse(baseline.read_text())
namespace={'logging':logging,'Path':Path,'datetime':datetime,'Optional':Optional}
selected=[n for n in source.body if isinstance(n,ast.ClassDef) and n.name in {'LoggingConfig','InteractiveConsoleFilter'}]
exec(compile(ast.Module(body=selected,type_ignores=[]),'baseline/config/logger.py','exec'),namespace)

def fixture():
    for i in range(576):
        logging.getLogger('BioMind.Performance').info('phase=state.load status=completed elapsed_ns=42')
    for name in ['BioMind.Agent','agent_core.provider_attempt','agent_core.workflow.typed_node','httpx']:
        logging.getLogger(name).info('internal prompt accounting HTTP 200 {"payload":"fixture"}')
    logging.getLogger('BioMind.Agent').warning('fixture warning')
    logging.getLogger('BioMind.Agent').error('fixture error')

results={}
for mode in ['before','after']:
    root=logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler);handler.close()
    screen=io.StringIO();path=folder/(mode+'.log')
    with redirect_stderr(screen):
        if mode=='before':namespace['LoggingConfig'].setup_logging(log_file=str(path))
        else:configure_logging(logging.INFO,path)
        # Exclude setup messages; measure identical emitted fixture only.
        screen.seek(0);screen.truncate()
        fixture()
    content=screen.getvalue()
    results[mode]={'terminal_bytes':len(content.encode()),'terminal_lines':len(content.splitlines()),'perf_lines':content.count('phase=state.load'),'file_perf_lines':path.read_text().count('phase=state.load'),'warnings':content.count('fixture warning'),'errors':content.count('fixture error'),'fixture_records':582}
result_path.write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
