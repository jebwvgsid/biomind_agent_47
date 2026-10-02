"""Fresh process/new session registry-only receipt. No software launch permitted."""
import asyncio,importlib.abc,json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
REPO=Path(__file__).resolve().parents[3];sys.path[:0]=[str(REPO),str(REPO/'integrations/pi_runtime')]
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='config' or fullname.startswith('config.') or fullname in {'dotenv','bio_agents','agent_core.installer'}:raise ImportError('FORBIDDEN:'+fullname)
sys.meta_path.insert(0,Guard())
from domain_adapter.service import DomainService
from agent_core.execution_profile import release_host_profile
async def main():
 receipt=json.loads((Path(__file__).parent/'rna-registration.json').read_text())
 with tempfile.TemporaryDirectory(prefix='biomind-rna-fresh-') as td:
  root=Path(td);prefs=root/'preferences';prefs.write_text('metadata reader')
  s=DomainService(cwd=root,data_root=root/'data',install_root=REPO,session_id='rna-new-read-only-session',registry_path=receipt['authority'],preferences_path=prefs,skills_root=root/'skills',execution_profile='native')
  await s.call('request.begin',{'request_id':'fresh-registry-read','role':'consult'},host_token=s.host_token)
  with patch('subprocess.Popen',side_effect=AssertionError('REAL_SOFTWARE_EXECUTION_FORBIDDEN')):
   selected=await s.call('environments.select',{'requirements':{'tools':['Rscript'],'packages':['fgsea']}})
  release_host_profile(s)
  print(json.dumps({'scope':'new process and new temporary session; metadata only','selection':selected,'subprocess_count':0,'R_fgsea_ORA_TAD':'NOT_RUN','scientific_startup':'NOT_RUN'},ensure_ascii=False,indent=2))
asyncio.run(main())
