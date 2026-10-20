args=commandArgs(TRUE); D=args[1]; O=args[2];dir.create(O,recursive=TRUE,showWarnings=FALSE)
suppressPackageStartupMessages(library(rrBLUP))
source('/public/home/liusiqi/03.毕设/benchmarks/runtime_setup_20261014/output/sources/CERIS_JGRA/Sub_functions.r')
read=function(n) read.delim(file.path(D,n),check.names=FALSE)
write=function(x,n) write.table(x,file.path(O,n),sep='\t',row.names=FALSE,quote=FALSE)
p=read('phenotype_multi_environment.tsv');idx=read('window_coverage.tsv');g=read('genotype_matrix.tsv');rownames(g)=g$sample_id;g=as.matrix(g[,-1]);storage.mode(g)='double';stopifnot(nrow(p)==895,nrow(g)==31)
z=aggregate(trait_yield~sample_id+environment_id,p,mean);z=merge(z,idx[idx$eligible=='True',]);z$environmental_index=z$window_mean_temperature
fit=function(dat) {
 out=list()
 for(s in sort(unique(dat$sample_id))){d=dat[dat$sample_id==s,];if(length(unique(d$environment_id))<3||sd(d$environmental_index)==0)next;m=lm(trait_yield~environmental_index,d);v=summary(m);out[[s]]=data.frame(sample_id=s,intercept=coef(m)[1],slope=coef(m)[2],intercept_se=v$coefficients[1,2],slope_se=v$coefficients[2,2],n_env=nrow(d),R2=v$r.squared)}
 do.call(rbind,out)
}
full=fit(z);stopifnot(nrow(full)>=10,all(is.finite(full$intercept)),all(is.finite(full$slope)));write(full,'reaction_norm.tsv')
out=list();skipped=list()
for(e in sort(unique(z$source_group))){tr=z[z$source_group!=e,];te=z[z$source_group==e,];pa=fit(tr);r=merge(te,pa,by='sample_id');r$predicted=r$intercept+r$slope*r$environmental_index;r$held_out_group=e;r$extrapolation=r$environmental_index<min(tr$environmental_index)|r$environmental_index>max(tr$environmental_index);out[[e]]=r;skipped[[e]]=data.frame(held_out_group=e,n_test=nrow(te),n_predicted=nrow(r))}
v=do.call(rbind,out);write(v,'environment_group_oof.tsv');write(do.call(rbind,skipped),'environment_group_coverage.tsv')
# Independent genotype holdout for marker prediction of reaction-norm parameters.
set.seed(20260603);fold=sample(rep(1:5,length.out=nrow(full)));full$fold=fold;gp=list()
for(k in 1:5){tr=full[fold!=k,];te=full[fold==k,];A=g[tr$sample_id,,drop=FALSE];B=g[te$sample_id,,drop=FALSE];mu=colMeans(A,na.rm=TRUE);keep=is.finite(mu);A=A[,keep,drop=FALSE];B=B[,keep,drop=FALSE];mu=mu[keep];stopifnot(length(mu)>0);for(j in seq_along(mu)){A[is.na(A[,j]),j]=mu[j];B[is.na(B[,j]),j]=mu[j]};sdv=apply(A,2,sd);sdv[!is.finite(sdv)|sdv==0]=1;A=sweep(sweep(A,2,mu),2,sdv,'/');B=sweep(sweep(B,2,mu),2,sdv,'/');r=te
 for(t in c('intercept','slope')){m=mixed.solve(y=tr[[t]],Z=A);r[[paste0(t,'_predicted')]]=as.numeric(m$beta)+as.vector(B%*%m$u)}
 gp[[k]]=r}
pp=do.call(rbind,gp);stopifnot(all(is.finite(pp$intercept_predicted)),all(is.finite(pp$slope_predicted)));write(pp,'genotype_oof.tsv');recon=merge(z,pp,by='sample_id');recon$predicted=recon$intercept_predicted+recon$slope_predicted*recon$environmental_index;write(recon,'genotype_oof_phenotypes.tsv')
metrics=data.frame(stage=c('heldout_weather_group','heldout_genotype'),n=c(nrow(v),nrow(recon)),RMSE=c(sqrt(mean((v$trait_yield-v$predicted)^2)),sqrt(mean((recon$trait_yield-recon$predicted)^2))));write(metrics,'verification.tsv');write(data.frame(status='PASS',raw_plots=nrow(p),eligible_env=length(unique(z$environment_id)),fit_genotypes=nrow(full),weather_groups=length(unique(z$source_group))), 'status.tsv');print(metrics)
