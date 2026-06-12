args <- commandArgs(trailingOnly=TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript run_edger.R --counts counts.tsv --metadata metadata.tsv --contrast factor,treatment,control --outdir out\n")
  quit(status=0)
}
getarg <- function(flag, default=NULL) { i <- match(flag, args); if (is.na(i)) default else args[[i+1]] }
counts_file <- getarg("--counts"); metadata_file <- getarg("--metadata"); outdir <- getarg("--outdir", "rnaseq_de")
contrast <- strsplit(getarg("--contrast"), ",")[[1]]
if (is.null(counts_file) || is.null(metadata_file) || length(contrast) != 3) stop("Missing required arguments")
if (!requireNamespace("edgeR", quietly=TRUE)) stop("edgeR is not installed.")
dir.create(file.path(outdir, "tables"), recursive=TRUE, showWarnings=FALSE)
cts <- read.delim(counts_file, check.names=FALSE); rownames(cts) <- cts[[1]]; cts <- cts[, -1, drop=FALSE]
meta <- read.delim(metadata_file, check.names=FALSE); rownames(meta) <- meta$sample_id
group <- factor(meta[[contrast[1]]], levels=c(contrast[3], contrast[2]))
y <- edgeR::DGEList(counts=cts[, rownames(meta), drop=FALSE], group=group)
y <- edgeR::calcNormFactors(y)
design <- model.matrix(~group)
y <- edgeR::estimateDisp(y, design)
fit <- edgeR::glmQLFit(y, design)
qlf <- edgeR::glmQLFTest(fit, coef=2)
res <- edgeR::topTags(qlf, n=Inf)$table
res$gene_id <- rownames(res)
res <- res[, c("gene_id", setdiff(names(res), "gene_id"))]
write.table(res, file.path(outdir, "tables", "de_results.full.tsv"), sep="\t", quote=FALSE, row.names=FALSE)
write.table(subset(res, FDR < 0.05), file.path(outdir, "tables", "de_results.significant.tsv"), sep="\t", quote=FALSE, row.names=FALSE)
