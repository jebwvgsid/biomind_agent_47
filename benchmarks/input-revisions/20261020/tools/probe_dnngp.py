import faulthandler,sys,inspect,os
faulthandler.enable();faulthandler.dump_traceback_later(60,repeat=False)
print('start',flush=True)
sys.path.insert(0,'/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/DNNGP/Scripts')
import tensorflow as tf
print('TF',tf.__version__,flush=True)
import dnngp
print('IMPORTED',flush=True)
for k in dir(dnngp):
 v=getattr(dnngp,k)
 if inspect.isfunction(v) or inspect.isclass(v):
  try:print(k,inspect.signature(v),flush=True)
  except:pass
