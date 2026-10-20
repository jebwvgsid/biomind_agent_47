import sys,inspect,os
sys.path.insert(0,'/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/DNNGP/Scripts')
import dnngp,config_dnngp
for m in [dnngp,config_dnngp]:
 for k in dir(m):
  v=getattr(m,k)
  if getattr(v,'__module__','')==m.__name__:
   try:s=str(inspect.signature(v))
   except:s='?'
   print(m.__name__,k,s,repr(getattr(v,'__doc__',None)),flush=True)
