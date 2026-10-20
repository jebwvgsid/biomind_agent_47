"""Native models; explicit data dimensions, fixed epochs and sealed outer folds.
The GP adapter retains upstream TModel; only size-dependent kernel/attention
parameters change. No padding invents markers. Single-row inference repeats
the same tensor to avoid the upstream squeeze() bug, returning one prediction.
"""
from pathlib import Path
import argparse,os,sys,json,hashlib
p=argparse.ArgumentParser();p.add_argument('--model',choices=['gp_waiter','cropformer','dnngp'],required=True);p.add_argument('--data',required=True);p.add_argument('--out',required=True);p.add_argument('--config',required=True);a=p.parse_args()
D=Path(a.data).resolve();O=Path(a.out).resolve();O.mkdir(parents=True,exist_ok=True)
os.environ.update(MPLCONFIGDIR=str(O/'mpl'),KERAS_HOME=str(O/'keras'),XDG_CACHE_HOME=str(O/'cache'),TF_NUM_INTEROP_THREADS='1',TF_NUM_INTRAOP_THREADS='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
import numpy as np,pandas as pd
C=json.loads(Path(a.config).read_text());S=Path(C['runtime_root']);cfg=C[a.model]
g=pd.read_csv(D/'genotype_matrix.tsv',sep='\t',index_col=0);ph=pd.read_csv(D/'phenotype.tsv',sep='\t',index_col=0);fold=pd.read_csv(D/'cv_fold_design.tsv',sep='\t',index_col=0);ids=list(g.index);assert len(set(ids))==len(ids);assert set(ids)==set(ph.index)==set(fold.index)
y=ph.loc[ids,C['target']].values.astype('float32');f=fold.loc[ids,'fold_id'].values
parts=[g]
if a.model=='dnngp':
 for n in ['expression_matrix.tsv','metabolite_matrix.tsv']:
  t=pd.read_csv(D/n,sep='\t',index_col=0);assert set(t.index)==set(ids);parts.append(t.loc[ids])
x=pd.concat(parts,axis=1).values.astype('float32');assert np.isfinite(y).all();preds=[];audits=[]
for k in sorted(set(f)):
 W=O/f'fold_{k}';W.mkdir(exist_ok=True);outer=np.flatnonzero(f!=k);test=np.flatnonzero(f==k);np.random.seed(C['seed']+int(k));train=outer.copy();inner=[]
 if a.model=='dnngp':
  # Inner holdout independent of outer-test labels; native callbacks see inner only.
  order=sorted(outer,key=lambda j:hashlib.sha256(f"{C['seed']}|{k}|{ids[j]}".encode()).digest());nval=max(3,int(np.ceil(.2*len(outer))));inner=np.array(order[:nval]);train=np.array(order[nval:])
 mu=np.nanmean(x[train],axis=0);assert np.isfinite(mu).all();filled=np.where(np.isnan(x),mu,x);sd=filled[train].std(0);sd=np.where(sd<1e-7,1.,sd);xx=(filled-mu)/sd;ym=y[train].mean();ys=y[train].std();assert ys>0;yy=(y-ym)/ys
 np.savez(W/'preprocessing.npz',mean=mu,scale=sd,y_mean=ym,y_scale=ys)
 if a.model in ['gp_waiter','cropformer']:
  import torch
  torch.set_num_threads(1);torch.manual_seed(C['seed']+int(k));device=torch.device(cfg.get('device','cpu'))
  if device.type=='cuda' and not torch.cuda.is_available():raise RuntimeError('requested CUDA but no allocated device')
  if a.model=='gp_waiter':
   from scipy.stats import linregress
   sys.path.insert(0,str(S/'sources/GP-WAITER'));from model import GP_WAITER as gp
   weights=[]
   for j in range(x.shape[1]):
    pv=1. if np.std(xx[train,j])<1e-7 else float(linregress(xx[train,j],y[train]).pvalue)
    weights.append(1.+min(10.,-np.log10(max(pv,1e-10))))
   pd.DataFrame({'feature_id':g.columns,'weight':weights,'training_fold_excludes':k}).to_csv(W/'gwas_weights.tsv',sep='\t',index=False)
   h,w=cfg['input_shape'];assert h*w==x.shape[1],'input_shape product must equal real marker count; padding forbidden'
   conv=cfg['conv_params'];H,Wd=h,w
   for c in conv:H=(H-c['kernel_size'][0])//c['stride'][0]+1;Wd=(Wd-c['kernel_size'][1])//c['stride'][1]+1
   assert H>0 and Wd==cfg['attention'][0]['embed_size1']
   net=gp.TModel(embed_size=cfg['attention'][-1]['embed_size2'],w=torch.tensor(weights,dtype=torch.float32,device=device).reshape(h,w),conv_params=conv,param=cfg['attention'],num_layers=len(cfg['attention']))
   # Same two Conv1d layers and activation topology; dimensions configured for this panel.
   if 'output_convolutions' in cfg:
    for index,cc in zip([0,2],cfg['output_convolutions']):
     net.out_r[index]=torch.nn.Conv1d(1,1,cc['kernel_size'],stride=cc['stride']);H=(H-cc['kernel_size'])//cc['stride']+1
    assert H==1,'configured output head must reduce height to one'
   xx=xx.reshape(len(ids),h,w)
  else:
   sys.path.insert(0,str(S/'entrypoints/Cropformer'));from model import SelfAttention
   net=SelfAttention(input_size=x.shape[1],**cfg['model_parameters'])
  net.to(device);opt=torch.optim.Adam(net.parameters(),lr=cfg['learning_rate']);xt=torch.tensor(xx,dtype=torch.float32,device=device);yt=torch.tensor(yy,dtype=torch.float32,device=device)
  def forward(z):
   n=z.shape[0]
   if a.model=='gp_waiter' and n==1:z=torch.cat([z,z],dim=0)
   return net(z).reshape(-1)[:n]
  hist=[]
  for epoch in range(cfg['epochs']):
   net.train();opt.zero_grad();v=forward(xt[train]);loss=(v-yt[train]).square().mean();assert torch.isfinite(loss);loss.backward();opt.step();hist.append({'epoch':epoch+1,'training_loss':float(loss.detach())})
  net.eval()
  with torch.no_grad():
   pp=forward(xt[test]).cpu().numpy()*ys+ym
   # Independent one-sample inference must agree with batched prediction.
   single=forward(xt[test[:1]]).cpu().numpy()*ys+ym
   assert np.allclose(pp[:1],single,rtol=1e-4,atol=1e-4)
  torch.save({'state_dict':net.state_dict(),'config':cfg,'feature_order':list(g.columns)},W/'model.pt');pd.DataFrame(hist).to_csv(W/'training_history.tsv',sep='\t',index=False)
 else:
  sys.path.insert(0,str(S/'sources/DNNGP/Scripts'));import tensorflow as tf,dnngp
  tf.keras.backend.clear_session();tf.random.set_seed(C['seed']+int(k));xx=xx[:,:,None]
  ret=dnngp.dnngp_model(xx[train],xx[inner],yy[train],yy[inner],xx.shape[1],cfg['epochs'],cfg['patience'],cfg['batch_size'],cfg['learning_rate'],cfg['dropout1'],cfg['dropout2'],str(W)+'/',cfg['earlystopping'],'yield',int(k))
  files=list(W.glob('*.h5'));assert len(files)==1
  net=tf.keras.models.load_model(files[0],compile=False);pp=net.predict(xx[test],verbose=0).reshape(-1)*ys+ym
  ret[2].to_csv(W/'training_history.tsv',sep='\t',index=False)
 assert len(pp)==len(test) and np.isfinite(pp).all()
 for j,v in zip(test,pp):preds.append({'sample_id':ids[j],'fold_id':int(k),'observed':float(y[j]),'predicted':float(v)})
 audits.append({'fold':int(k),'training_ids':[ids[j] for j in train],'inner_validation_ids':[ids[j] for j in inner],'outer_test_ids':[ids[j] for j in test],'epochs':cfg['epochs'],'preprocessing_fit':'training_ids only','test_labels_used_by_training':False})
 (O/'fold_audit.json').write_text(json.dumps(audits,indent=2));print(a.model,'fold',k,'PASS',flush=True)
r=pd.DataFrame(preds);assert len(r)==len(ids) and r.sample_id.nunique()==len(ids);r.to_csv(O/'oof_predictions.tsv',sep='\t',index=False);m={'status':'PASS','model':a.model,'n_oof':len(r),'rmse':float(np.sqrt(np.mean((r.predicted-r.observed)**2))),'pearson':float(r.predicted.corr(r.observed)),'purpose':'small simulated input functional validation; not paper performance reproduction','config':C,'slurm_job_id':os.getenv('SLURM_JOB_ID')};(O/'verification.json').write_text(json.dumps(m,indent=2));print(m['status'],a.model,m['rmse'],flush=True)
