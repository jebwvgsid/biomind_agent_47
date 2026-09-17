"""Isolated managed-workflow bootstrap. No startup/config/credential reads.

Synthetic configuration and inert environment detection are test inputs.
PlanTracker, CAS, compilation, approval, graph and provider wrapper stay real.
"""
import ast,sys,os,types,json,hashlib,logging,tempfile,shlex
from pathlib import Path
C=Path(sys.argv.pop(1)).resolve()
if not str(C).startswith('/tmp/'):
 raise SystemExit('Export an explicit safe candidate to /tmp before running this bootstrap.')
os.chdir(C);sys.dont_write_bytecode=True
sys.path[:]=[str(C)]+[p for p in sys.path if '/03.毕设/' not in p]
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1';os.environ['BIOMIND_WIKI_ENABLED']='0';os.environ['LOG_LEVEL']='CRITICAL'
RUNROOT=Path(tempfile.mkdtemp(prefix='managed-bootstrap-',dir=os.environ['TMPDIR']))
OFFLINE_ALLOWED_SCRIPT_PATHS=set()
OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS={}
HOST_ALLOWED_BWRAP_ARGV=()
HOST_ALLOWED_BWRAP_REMAINING=0
def guard(event,args):
 global HOST_ALLOWED_BWRAP_REMAINING
 if event=='subprocess.Popen' and HOST_ALLOWED_BWRAP_REMAINING==1 and args[0]=='/usr/bin/bwrap' and tuple(args[1])==HOST_ALLOWED_BWRAP_ARGV and args[2]=='/' and args[3]=={}:
  HOST_ALLOWED_BWRAP_REMAINING=0
  return  # One pre-generated read-only production sandbox invocation only.
 if event=='subprocess.Popen' and args[1][:2]==['/bin/sh','-c'] and len(args[1])==3:
  command=shlex.split(args[1][2])
  if len(command)==2 and command[0]=='bash' and command[1] in OFFLINE_ALLOWED_SCRIPT_PATHS and args[3]==OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.get(command[1], {'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}) and Path(command[1]).resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve()):return
 if event=='subprocess.Popen' and len(args[1])==4 and args[1][:3]==['/bin/bash','--noprofile','--norc'] and args[1][3] in OFFLINE_ALLOWED_SCRIPT_PATHS and Path(args[1][3]).resolve().is_relative_to(Path(os.environ['TMPDIR']).resolve()) and args[3]==OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.get(args[1][3], {'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}):
  return  # Explicitly registered tiny fixture; no ambient shell environment.
 if event=='subprocess.Popen' and ((args[1]==['bash','-n'] and not os.environ.get('BASH_ENV')) or (args[1] in [['/usr/bin/bash','--noprofile','--norc','-n'], ['/bin/bash','--noprofile','--norc','-n']] and args[3]=={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'})):
  return  # Parse synthetic stdin only; no script execution or filename.
 if event=='subprocess.Popen' and len(args[1])==5 and args[1][:4]==[sys.executable,'-I','-S','-c']:
  from execution.script_validation import PYTHON_COMPILE_DRIVER
  if args[1][4]==PYTHON_COMPILE_DRIVER and args[3]=={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}:return
 if event in {'socket.connect','socket.getaddrinfo','subprocess.Popen','os.system','os.posix_spawn'}:raise RuntimeError('OFFLINE_EXTERNAL_DENIED '+event)
 if event=='open' and isinstance(args[0],(str,bytes)):
  p=os.fsdecode(args[0]);name=Path(p).name
  if '/03.毕设/' in p or (p.startswith(str(C)) and '/config/' in p) or name in {'启动.sh','startup.py'} or name.startswith('.env'):raise RuntimeError('PROTECTED_READ_DENIED '+name)
  mode=args[1] or '';flags=args[2] or 0
  if (any(x in str(mode) for x in 'wax+') or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT)) and not str(Path(p).absolute()).startswith('/tmp/') and p!='/dev/null':raise RuntimeError('OFFLINE_WRITE_DENIED')
 if event in {'os.mkdir','os.remove','os.rename','os.rmdir'} and isinstance(args[0],(str,bytes)):
  if not str(Path(os.fsdecode(args[0])).absolute()).startswith('/tmp/'):raise RuntimeError('OFFLINE_MUTATION_DENIED')
sys.addaudithook(guard)
exports={}
for p in C.rglob('*.py'):
 for n in ast.walk(ast.parse(p.read_text())):
  if isinstance(n,ast.ImportFrom) and n.module and n.module.startswith('config'):
   for a in n.names:
    if a.name.isupper():exports.setdefault(a.name,1)
exports.update({
 'LLM_API_KEY':'','RAGFLOW_API_KEY':'','WIKI_EMBEDDING_API_KEY':'','LLM_BASE_URL':'https://offline.invalid/v1','LLM_DEFAULT_MODEL':'offline-model',
 'RAGFLOW_ADDRESS':'https://offline.invalid','RERANK_MODEL_ID':'offline','KB_MAP':{},'DEFAULT_PROJECT_ID':'offline',
 'BIOMIND_STEP_WORKER_BACKEND':'legacy','BIOMIND_WIKI_ENABLED':False,'ENABLE_ROOT_CAUSE_ANALYZER':False,
 'BIOMIND_MEMORY_DIR':str(RUNROOT/'memory'),'BIOMIND_WIKI_DIR':str(RUNROOT/'wiki'),'BIOMIND_WIKI_INGEST_MODE':'off',
 'BIO_SOFTWARE_ROOT':None,'BIO_SOURCE_INSTALL_DIR':None,'ANACONDA_ROOT':None,'SKILLS_DIR':str(C/'skills'),
 'WIKI_EMBEDDING_ENABLED':False,'WIKI_EMBEDDING_BASE_URL':'','WIKI_EMBEDDING_MODEL':'offline','WIKI_EMBEDDING_DIMENSIONS':1,
 'LLM_TIMEOUT_SECONDS':30,'LLM_READONLY_TIMEOUT_SECONDS':30,'LLM_TOTAL_TIMEOUT_SECONDS':30,'LLM_FIRST_CHUNK_TIMEOUT_SECONDS':10,'LLM_STREAM_IDLE_TIMEOUT_SECONDS':10,'LLM_CANCEL_CLEANUP_TIMEOUT_SECONDS':1,
 'LLM_MAX_RETRIES':0,'GENERATE_PROVIDER_MAX_ATTEMPTS':3,'GENERATE_PROVIDER_RETRY_BASE_SECONDS':0,'GENERATE_PROVIDER_RETRY_CAP_SECONDS':0,
 'LLM_MAX_HISTORY_MESSAGES':100,'LLM_MAX_HISTORY_CHARS':500000,'LLM_STEP_CONTEXT_MESSAGES':100,'LLM_STEP_CONTEXT_CHARS':500000,
 'LLM_STEP_PLAN_CHARS':100000,'LLM_STEP_SKILL_DOC_CHARS':200000,'LLM_STEP_EPHEMERAL_CHARS':50000,
 'SCHEMA2_CONTEXT_COMPACTION_ENABLED':False,'SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS':200000,
 'SCHEMA2_CONTEXT_OUTPUT_RESERVE_TOKENS':16000,'SCHEMA2_CONTEXT_ESTIMATION_MARGIN_TOKENS':2048,'SCHEMA2_CONTEXT_SAFETY_HEADROOM_TOKENS':2048,
 'SKILL_FULL_READ_MAX_CHARS':200000,'SKILL_EXCERPT_MAX_CHARS':24000,'MAX_SKILL_BINDING_PROPOSALS_PER_STEP':4,
 'MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION':4,'MAX_SKILL_SEARCH_ROUNDS_PER_STEP':4,'SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND':4,
 'SESSION_ARCHIVE_OBSERVATION_QUEUE_SIZE':1024,'SESSION_ARCHIVE_WRITER_MAX_ATTEMPTS':3,'SESSION_ARCHIVE_SHUTDOWN_DRAIN_SECONDS':1,
 'SESSION_ARCHIVE_WRITER_BACKOFF_SECONDS':0.01,'SESSION_ARCHIVE_WRITER_BUSY_TIMEOUT_SECONDS':1,
 'GENERATE_THINKING_ENABLED':False,'RECOVERY_THINKING_ENABLED':False,'CONSULTANT_DISCOVERY_THINKING_ENABLED':False,'PLANNER_CORRECTION_THINKING_ENABLED':False,
 'GENERATE_REASONING_EFFORT':'low','RECOVERY_REASONING_EFFORT':'low','CONSULTANT_DISCOVERY_REASONING_EFFORT':'low','PLANNER_CORRECTION_REASONING_EFFORT':'low',
 'DEEPSEEK_TEMPERATURE':0.0,'CONSULTANT_HISTORY_TOKEN_BUDGET':30000,'CONSULTANT_SKILL_TOKEN_BUDGET':40000,
 'CONSULTANT_RESERVED_HEADROOM_TOKENS':16000,'CONSULTANT_HISTORY_MAX_MESSAGES':100,'CONSULTANT_CANDIDATE_SKILL_LIMIT':10,
 'PLANNER_NOMINAL_CHAR_BUDGET':500000,'PLANNER_SOFT_TOKEN_RATIO':0.6,'PLANNER_COMPRESSION_TOKEN_RATIO':0.75,'PLANNER_HARD_TOKEN_RATIO':0.95,'PLANNER_RESERVED_HEADROOM_TOKENS':16000,
})
if os.environ.get('BIOMIND_TEST_OMIT_CONTEXT_SETTINGS') == '1':
 for key in list(exports):
  if key.startswith('SCHEMA2_'):exports.pop(key)
root=types.ModuleType('config');root.__path__=[]
for name in ['config','config.config','config.reliability','config.settings','config.startup','config.logger']:
 m=root if name=='config' else types.ModuleType(name)
 for key,value in exports.items():setattr(m,key,value)
 sys.modules[name]=m
 if name!='config':setattr(root,name.split('.')[-1],m)
class Paths:
 SYSTEM_ROOT=RUNROOT;WORK_DIR=RUNROOT/'work';SYSTEM_DB_PATH=RUNROOT/'memory.sqlite';DEFAULT_OUTPUT_DIR=RUNROOT/'output'
sys.modules['config.settings'].PathManager=Paths
sys.modules['config.startup'].StartupCheck=type('StartupCheck',(),{})
sys.modules['config.startup'].ReadlineConfig=type('ReadlineConfig',(),{})
sys.modules['config.logger'].get_logger=logging.getLogger
sys.modules['config.logger'].setup_logging=lambda *a,**k:None
m=types.ModuleType('dotenv');m.load_dotenv=lambda *a,**k:False;m.dotenv_values=lambda *a,**k:{};m.find_dotenv=lambda *a,**k:'';sys.modules['dotenv']=m
import langchain_core.messages,langchain_core.callbacks
import package_manager
# Prevent the environment inspection constructor from executing commands.
package_manager.PackageManager._detect_manager=lambda self:None
package_manager.PackageManager._get_conda_root=lambda self:None
before={str(p.relative_to(C)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(C.rglob('*.py'))}
identity=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
print('SOURCE_IDENTITY',identity,flush=True)
print('SYNTHETIC_CONFIG',json.dumps(exports,sort_keys=True),flush=True)
import pytest
result_path=Path(os.environ.get('BIOMIND_OFFLINE_RESULTS') or RUNROOT/'pytest-results.json')
reports=[]
class Reporter:
 def pytest_exception_interact(self,node,call,report):
  facts=[]
  for entry in call.excinfo.traceback:
   for key in ("events","update","generated","recovery_outcome","recovery_update"):
    if key in entry.frame.f_locals:
     facts.append({"name":key,"value":entry.frame.f_locals[key]})
  (result_path.parent/(result_path.stem+"-"+node.name+"-facts.json")).write_text(json.dumps(facts,indent=2,default=str))
 def pytest_runtest_logstart(self,nodeid,location):
  reports.append({'nodeid':nodeid,'phase':'start'})
  result_path.write_text(json.dumps(reports,indent=2))
 def pytest_runtest_logreport(self,report):
  reports.append({'nodeid':report.nodeid,'phase':report.when,'outcome':report.outcome,'duration':report.duration,'failure':report.longreprtext if report.failed else ''})
  result_path.write_text(json.dumps(reports,indent=2))
import faulthandler
faulthandler.dump_traceback_later(70,repeat=False)
plugins=[Reporter()]
if os.environ.get('BIOMIND_OFFLINE_FATAL_FILE'):
 from tests.cli_fatal_plugin import PersistentFatal
 plugins.append(PersistentFatal(Path(os.environ['BIOMIND_OFFLINE_FATAL_FILE'])))
code=pytest.main(sys.argv[1:],plugins=plugins)
faulthandler.cancel_dump_traceback_later()
os.write(1, ('\nPYTEST_EXIT_CODE='+str(code)+'\nRESULTS='+str(result_path)+'\n').encode())
after={str(p.relative_to(C)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(C.rglob('*.py'))}
print('SOURCE_UNCHANGED',before==after,flush=True)
print('IMPORTED_SOURCES',json.dumps({n:str(m.__file__) for n,m in list(sys.modules.items()) if getattr(m,'__file__',None) and str(m.__file__).startswith(str(C))},sort_keys=True),flush=True)
sys.exit(code if before==after else 3)
