"""Run with the absolute biomind Python -I -S; read distribution metadata only."""
import importlib.machinery
import importlib.metadata
import json
from pathlib import Path
import sys
import sysconfig

assert sys.flags.isolated and sys.flags.no_site
site=Path(sysconfig.get_path('purelib'));rows={}
for name in ['prompt-toolkit','wcwidth','rich','pytest','langchain-core']:
    ds=[d for d in importlib.metadata.distributions(path=[str(site)]) if d.metadata.get('Name','').lower().replace('_','-')==name]
    rows[name]=[{'name':d.metadata['Name'],'version':d.version,'location':str(d.locate_file('')),'installer':d.read_text('INSTALLER'),'requires':d.requires} for d in ds]
data={'executable':sys.executable,'version':sys.version,'site_packages':str(site),'isolated':True,'site_disabled':True,'distributions':rows,'prompt_toolkit_spec':str(importlib.machinery.PathFinder.find_spec('prompt_toolkit',[str(site)]))}
path=Path(sys.argv[1]);assert not path.exists();path.write_text(json.dumps(data,indent=2));print(json.dumps(data))
