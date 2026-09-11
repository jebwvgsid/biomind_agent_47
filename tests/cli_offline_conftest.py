import sys,types,os
from pathlib import Path
root=Path(__file__).parent
# Import executor itself, avoiding unrelated execution package eager imports.
p=types.ModuleType('execution');p.__path__=[str(root/'execution')];sys.modules['execution']=p
def guard(event,args):
 if event=='socket.connect':raise RuntimeError('network denied')
 if event=='open' and isinstance(args[0],(str,bytes)):
  path=os.fsdecode(args[0]); name=Path(path).name
  if '03.毕设' in path or name.startswith('.env') or name in {'startup.py','启动.sh'}:raise RuntimeError('original/credential path denied')
sys.addaudithook(guard)
