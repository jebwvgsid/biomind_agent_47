a=commandArgs(TRUE);D=a[1];O=a[2];dir.create(O,recursive=TRUE,showWarnings=FALSE);suppressPackageStartupMessages(library(rrBLUP))
rd=function(n)read.delim(file.path(D,n),check.names=FALSE,na.strings=c('NA',''))
wr=function(x,n)write.table(x,file.path(O,n),sep='\t',quote=FALSE,row.names=FALSE)
metrics=list()
for(branch in c('epav','sv')){
 g=rd(if(branch=='epav')'epav_matrix.tsv' else 'sv_genotype_matrix.tsv');rownames(g)=g$sample_id;g=as.matrix(g[,-1]);storage.mode(g)='double';p=rd(paste0(branch,'_phenotype.tsv'));cv=rd(paste0(branch,'_cv_folds.tsv'));stopifnot(!anyDuplicated(p$sample_id),setequal(p$sample_id,cv$sample_id),all(p$sample_id %in% rownames(g)));names(p)[2]='observed';p=merge(p,cv,by='sample_id');pred=list();audit=list()
 for(k in sort(unique(p$fold_id))){tr=p[p$fold_id!=k,];te=p[p$fold_id==k,];A=g[tr$sample_id,,drop=FALSE];B=g[te$sample_id,,drop=FALSE];mu=colMeans(A,na.rm=TRUE);keep=is.finite(mu);A=A[,keep,drop=FALSE];B=B[,keep,drop=FALSE];mu=mu[keep]
  for(j in seq_along(mu)){A[is.na(A[,j]),j]=mu[j];B[is.na(B[,j]),j]=mu[j]};ss=apply(A,2,sd);keep2=is.finite(ss)&ss>0;A=A[,keep2,drop=FALSE];B=B[,keep2,drop=FALSE];mu=mu[keep2];ss=ss[keep2];stopifnot(ncol(A)>1)
  A=sweep(sweep(A,2,mu),2,ss,'/');B=sweep(sweep(B,2,mu),2,ss,'/');m=mixed.solve(y=tr$observed,Z=A);te$predicted=as.numeric(m$beta)+as.vector(B%*%m$u);stopifnot(all(is.finite(te$predicted)));pred[[k]]=te;audit[[k]]=data.frame(fold=k,n_train=nrow(tr),n_test=nrow(te),n_markers=ncol(A),train_ids=paste(tr$sample_id,collapse=';'),test_ids=paste(te$sample_id,collapse=';'));saveRDS(list(fit=m,mean=mu,scale=ss,markers=colnames(A),training_ids=tr$sample_id),file.path(O,paste0(branch,'_fold',k,'.rds')))
 }
 q=do.call(rbind,pred);stopifnot(nrow(q)==nrow(p),!anyDuplicated(q$sample_id));wr(q,paste0(branch,'_oof_predictions.tsv'));wr(do.call(rbind,audit),paste0(branch,'_fold_audit.tsv'));metrics[[branch]]=data.frame(branch=branch,status='PASS',n_samples=nrow(p),input_features=ncol(g),RMSE=sqrt(mean((q$observed-q$predicted)^2)),Pearson=cor(q$observed,q$predicted));print(metrics[[branch]])
}
wr(do.call(rbind,metrics),'verification.tsv')
