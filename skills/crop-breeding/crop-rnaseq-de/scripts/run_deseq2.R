args <- commandArgs(trailingOnly=TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript run_deseq2.R --counts counts.tsv --metadata metadata.tsv --contrast factor,treatment,control --formula '~ condition' --outdir out\n")
  quit(status=0)
}
getarg <- function(flag, default=NULL) { i <- match(flag, args); if (is.na(i)) default else args[[i+1]] }
counts_file <- getarg("--counts")
metadata_file <- getarg("--metadata")
contrast <- strsplit(getarg("--contrast"), ",")[[1]]
formula_text <- getarg("--formula", "~ condition")
outdir <- getarg("--outdir", "rnaseq_de")
if (is.null(counts_file) || is.null(metadata_file) || length(contrast) != 3) stop("Missing required arguments")
if (!requireNamespace("DESeq2", quietly=TRUE)) stop("DESeq2 is not installed; use run_tpm_fc_filter.py for no-replicate screening or install DESeq2.")
dir.create(file.path(outdir, "tables"), recursive=TRUE, showWarnings=FALSE)
cts <- read.delim(counts_file, check.names=FALSE)
rownames(cts) <- cts[[1]]
cts <- as.matrix(cts[, -1, drop=FALSE])
meta <- read.delim(metadata_file, check.names=FALSE)
rownames(meta) <- meta$sample_id
cts <- cts[, rownames(meta), drop=FALSE]
dds <- DESeq2::DESeqDataSetFromMatrix(round(cts), meta, as.formula(formula_text))
dds <- DESeq2::DESeq(dds)
res <- as.data.frame(DESeq2::results(dds, contrast=contrast))
res$gene_id <- rownames(res)
res <- res[, c("gene_id", setdiff(names(res), "gene_id"))]
write.table(res, file.path(outdir, "tables", "de_results.full.tsv"), sep="\t", quote=FALSE, row.names=FALSE)
write.table(subset(res, !is.na(padj) & padj < 0.05), file.path(outdir, "tables", "de_results.significant.tsv"), sep="\t", quote=FALSE, row.names=FALSE)
writeLines(capture.output(sessionInfo()), file.path(outdir, "reproducibility", "sessionInfo.txt"))
