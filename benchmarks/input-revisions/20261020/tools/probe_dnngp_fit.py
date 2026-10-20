import sys,os,pathlib,numpy as np,pandas as pd,traceback
R=pathlib.Path(__file__).resolve().parents[1];W=R/'evidence/dnngp-fit-probe';W.mkdir(exist_ok=True);os.chdir(W)
os.environ.update(TF_CPP_MIN_LOG_LEVEL='2',TF_NUM_INTEROP_THREADS='1',TF_NUM_INTRAOP_THREADS='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MPLCONFIGDIR=str(W/'mpl'),KERAS_HOME=str(W/'keras'))
sys.path.insert(0,str(R.parent/'benchmarks/runtime_setup_20261014/output/sources/DNNGP/Scripts'))
import dnngp
D=R.parent/'benchmarks/03.输入修复26.10.20/00.共享数据/multiomics';x=np.hstack([pd.read_csv(D/n,sep='\t',index_col=0).values for n in ['genotype_matrix.tsv','expression_matrix.tsv','metabolite_matrix.tsv']]).astype('float32');y=pd.read_csv(D/'phenotype.tsv',sep='\t').iloc[:,1].values.astype('float32');x=(x-x[:16].mean(0))/(x[:16].std(0)+1e-6);y=(y-y[:16].mean())/y[:16].std()
try:
 r=dnngp.dnngp_model(x[:16,:,None],x[16:20,:,None],y[:16],y[16:20],148,2,10,4,.001,.2,.2,str(W)+'/',20,'probe',1);print('RETURN',type(r),repr(r)[:600],flush=True)
except Exception:traceback.print_exc()
print('FILES',list(W.iterdir()),flush=True)
