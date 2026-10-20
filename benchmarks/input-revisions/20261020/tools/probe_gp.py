import sys,inspect,torch,numpy as np
sys.path.insert(0,'/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/GP-WAITER')
from model import GP_WAITER as gp
torch.set_num_threads(1)
w=torch.ones(32,2)
conv=[dict(in_channels=1,out_channels=1,kernel_size=(1,1),stride=(1,1)) for i in range(4)]
param=[dict(embed_size1=2,embed_size2=8,num_heads=1),dict(embed_size1=8,embed_size2=8,num_heads=2),dict(embed_size1=8,embed_size2=8,num_heads=2)]
m=gp.TModel(embed_size=8,w=w,conv_params=conv,param=param,num_layers=3)
print(m,flush=True)
x=torch.rand(4,32,2)
try:
 y=m(x);print('shape',y.shape,flush=True);y.square().mean().backward();print('BACKWARD_OK',flush=True)
except Exception as e:print(type(e).__name__,str(e),flush=True)
m.eval()
for layer in m.layers:
 layer.attn.register_forward_pre_hook(lambda mod,args:print('ATTN_SHAPE',args[0].shape,'batch_first',mod.batch_first,flush=True))
with torch.no_grad():
 a=m(x)
 b=m(torch.cat([x[:2],x[:2]*0]))
 print('BATCH_DEPENDENCE',float((a[:2]-b[:2]).abs().max()),flush=True)
 try:print('SINGLE',m(x[:1]),flush=True)
 except Exception as e:print('SINGLE_ERROR',e,flush=True)
