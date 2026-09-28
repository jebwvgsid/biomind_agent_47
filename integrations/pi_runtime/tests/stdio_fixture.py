"""STDIO fixture using the actual service/permission/owner/executor policy."""
import asyncio
from pathlib import Path
import sys

sys.dont_write_bytecode=True
from test_python_domain import DomainService,REPO
import python_service

root=Path(sys.argv[1]).resolve()
service=DomainService(cwd=root,data_root=root/'data',install_root=REPO,session_id='stdio-fixture',
    skills_root=root/'skills',preferences_path=root/'preferences.md',registry_path=root/'registry.json')
asyncio.run(python_service.serve(service))
