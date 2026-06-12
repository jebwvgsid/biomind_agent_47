args <- commandArgs(trailingOnly=TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript plot_heatmap.R --tpm-matrix gene_tpm.tsv --de-table de.tsv --outdir figures [--top-n 50]\n")
  quit(status=0)
}
getarg <- function(flag, default=NULL) { i <- match(flag, args); if (is.na(i)) default else args[[i+1]] }
tpm_file <- getarg("--tpm-matrix"); de_file <- getarg("--de-table"); outdir <- getarg("--outdir", "figures"); top_n <- as.integer(getarg("--top-n", "50"))
if (is.null(tpm_file) || is.null(de_file)) stop("--tpm-matrix and --de-table are required")
dir.create(outdir, recursive=TRUE, showWarnings=FALSE)
tpm <- read.delim(tpm_file, check.names=FALSE); names(tpm)[1] <- "gene_id"
de <- read.delim(de_file, check.names=FALSE); names(de)[1] <- "gene_id"
lfc_col <- if ("log2FoldChange" %in% names(de)) "log2FoldChange" else if ("logFC" %in% names(de)) "logFC" else stop("No log2FoldChange/logFC column")
genes <- head(de$gene_id[order(abs(de[[lfc_col]]), decreasing=TRUE)], top_n)
m <- as.matrix(tpm[match(genes, tpm$gene_id), setdiff(names(tpm), "gene_id"), drop=FALSE])
rownames(m) <- genes
m <- log2(m + 1)
m <- t(scale(t(m)))
m[!is.finite(m)] <- 0
png(file.path(outdir, "top_deg_heatmap.png"), width=1800, height=1800, res=220)
heatmap(m, scale="none", margins=c(8,8), col=colorRampPalette(c("#2166ac", "white", "#b2182b"))(51))
dev.off()
