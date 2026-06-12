args <- commandArgs(trailingOnly=TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript plot_volcano.R --de-table de.tsv --outdir figures [--label-top 10]\n")
  quit(status=0)
}
getarg <- function(flag, default=NULL) { i <- match(flag, args); if (is.na(i)) default else args[[i+1]] }
de_file <- getarg("--de-table"); outdir <- getarg("--outdir", "figures"); label_top <- as.integer(getarg("--label-top", "10"))
if (is.null(de_file)) stop("--de-table is required")
dir.create(outdir, recursive=TRUE, showWarnings=FALSE)
de <- read.delim(de_file, check.names=FALSE)
if (!"gene_id" %in% names(de)) names(de)[1] <- "gene_id"
lfc_col <- if ("log2FoldChange" %in% names(de)) "log2FoldChange" else if ("logFC" %in% names(de)) "logFC" else stop("No log2FoldChange/logFC column")
p_col <- if ("padj" %in% names(de)) "padj" else if ("pvalue" %in% names(de)) "pvalue" else NA
y <- if (is.na(p_col)) de[[lfc_col]] * 0 else -log10(pmax(de[[p_col]], 1e-300))
png(file.path(outdir, "volcano.labelled.png"), width=1800, height=1500, res=220)
plot(de[[lfc_col]], y, pch=16, cex=.55, col=ifelse(abs(de[[lfc_col]]) >= 1, "#b43c3c", "#566573"),
     xlab="log2 fold change", ylab=ifelse(is.na(p_col), "screening score", paste0("-log10(", p_col, ")")), main="Crop DEG volcano")
abline(v=c(-1,1), lty=2, col="grey50")
if (!is.na(p_col)) abline(h=-log10(0.05), lty=2, col="grey50")
ord <- order(abs(de[[lfc_col]]), decreasing=TRUE)[seq_len(min(label_top, nrow(de)))]
text(de[[lfc_col]][ord], y[ord], labels=de$gene_id[ord], cex=.55, pos=3)
dev.off()
