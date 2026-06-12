args <- commandArgs(trailingOnly=TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript plot_ma.R --de-table de.tsv --outdir figures\n")
  quit(status=0)
}
getarg <- function(flag, default=NULL) { i <- match(flag, args); if (is.na(i)) default else args[[i+1]] }
de_file <- getarg("--de-table"); outdir <- getarg("--outdir", "figures")
if (is.null(de_file)) stop("--de-table is required")
dir.create(outdir, recursive=TRUE, showWarnings=FALSE)
de <- read.delim(de_file, check.names=FALSE)
lfc_col <- if ("log2FoldChange" %in% names(de)) "log2FoldChange" else if ("logFC" %in% names(de)) "logFC" else stop("No log2FoldChange/logFC column")
x <- if ("baseMean" %in% names(de)) log10(de$baseMean + 1) else if ("mean_tpm" %in% names(de)) log10(de$mean_tpm + 1) else seq_len(nrow(de))
png(file.path(outdir, "ma_plot.png"), width=1700, height=1300, res=220)
plot(x, de[[lfc_col]], pch=16, cex=.55, col="#3b6f88", xlab="log10 mean expression + 1", ylab="log2 fold change", main="Crop DEG MA plot")
abline(h=c(-1,0,1), lty=c(2,1,2), col="grey50")
dev.off()
