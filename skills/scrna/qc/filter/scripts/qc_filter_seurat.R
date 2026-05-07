args <- commandArgs(trailingOnly = TRUE)

get_arg <- function(flag, default = NULL) {
  if (!(flag %in% args)) {
    return(default)
  }
  idx <- match(flag, args)
  if (is.na(idx) || idx == length(args)) {
    return(default)
  }
  args[[idx + 1]]
}

manifest_path <- get_arg("--manifest")
input_rds <- get_arg("--input-rds")
output_rds_arg <- get_arg("--output-rds")

if (is.null(manifest_path) || !nzchar(manifest_path)) {
  stop("Usage: Rscript qc_filter_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/raw.rds]")
}

suppressPackageStartupMessages({
  library(Seurat)
  library(yaml)
  library(ggplot2)
  library(patchwork)
})

`%||%` <- function(x, y) {
  if (is.null(x) || length(x) == 0 || identical(x, "")) {
    y
  } else {
    x
  }
}

ensure_dir <- function(path) {
  if (!dir.exists(path)) {
    dir.create(path, recursive = TRUE, showWarnings = FALSE)
  }
}

collapse_fail_reasons <- function(row) {
  failed <- names(row)[!as.logical(row)]
  if (length(failed) == 0) {
    return("")
  }
  paste(failed, collapse = ";")
}

default_mt_pattern <- function(species) {
  species <- tolower(species %||% "")
  if (species == "mouse") {
    return("^mt-")
  }
  "^MT-"
}

cfg <- yaml::read_yaml(manifest_path)
if (is.null(cfg) || !is.list(cfg)) {
  stop("Manifest is empty or invalid YAML.")
}

output_dir <- cfg$output_dir %||% stop("Manifest must define output_dir.")
objects_dir <- file.path(output_dir, "objects")
tables_dir <- file.path(output_dir, "tables")
plots_dir <- file.path(output_dir, "plots")
ensure_dir(objects_dir)
ensure_dir(tables_dir)
ensure_dir(plots_dir)

input_rds <- input_rds %||% file.path(objects_dir, "seurat_raw.rds")
output_rds <- output_rds_arg %||% file.path(objects_dir, "seurat_qc.rds")

obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}

mt_pattern <- cfg$mt_pattern %||% default_mt_pattern(cfg$species)
if (!("percent.mt" %in% colnames(obj@meta.data))) {
  matched_features <- grep(mt_pattern, rownames(obj), value = TRUE)
  if (length(matched_features) > 0) {
    obj[["percent.mt"]] <- PercentageFeatureSet(obj, pattern = mt_pattern)
  } else {
    obj[["percent.mt"]] <- 0
  }
}

qc <- cfg$qc %||% list()
min_features <- qc$min_features %||% 200
max_features <- qc$max_features %||% Inf
min_counts <- qc$min_counts %||% 0
max_counts <- qc$max_counts %||% Inf
max_percent_mt <- qc$max_percent_mt %||% 20

before_n <- ncol(obj)

violin_features <- c("nFeature_RNA", "nCount_RNA", "percent.mt")
present_features <- violin_features[violin_features %in% colnames(obj@meta.data)]
if (length(present_features) > 0) {
  p_vln <- VlnPlot(obj, features = present_features, ncol = length(present_features), pt.size = 0.1)
  ggsave(file.path(plots_dir, "qc_violin.png"), plot = p_vln, width = 12, height = 4, dpi = 300)
}

p_scatter_1 <- FeatureScatter(obj, feature1 = "nCount_RNA", feature2 = "nFeature_RNA")
p_scatter_2 <- FeatureScatter(obj, feature1 = "nCount_RNA", feature2 = "percent.mt")
ggsave(file.path(plots_dir, "qc_scatter.png"), plot = p_scatter_1 + p_scatter_2, width = 10, height = 4, dpi = 300)

qc_metrics <- obj@meta.data
qc_metrics$cell_barcode <- rownames(qc_metrics)
qc_metrics$pass_min_features <- qc_metrics$nFeature_RNA >= min_features
qc_metrics$pass_max_features <- qc_metrics$nFeature_RNA <= max_features
qc_metrics$pass_min_counts <- qc_metrics$nCount_RNA >= min_counts
qc_metrics$pass_max_counts <- qc_metrics$nCount_RNA <= max_counts
qc_metrics$pass_percent_mt <- qc_metrics$percent.mt <= max_percent_mt
pass_cols <- c(
  "pass_min_features",
  "pass_max_features",
  "pass_min_counts",
  "pass_max_counts",
  "pass_percent_mt"
)
qc_metrics$pass_qc <- Reduce(`&`, qc_metrics[pass_cols])
qc_metrics$qc_fail_reasons <- apply(qc_metrics[pass_cols], 1, collapse_fail_reasons)
qc_metrics$threshold_min_features <- min_features
qc_metrics$threshold_max_features <- max_features
qc_metrics$threshold_min_counts <- min_counts
qc_metrics$threshold_max_counts <- max_counts
qc_metrics$threshold_max_percent_mt <- max_percent_mt

meta_cols_to_append <- setdiff(colnames(qc_metrics), colnames(obj@meta.data))
for (col in meta_cols_to_append) {
  obj[[col]] <- qc_metrics[[col]]
}

filtered <- subset(obj, cells = qc_metrics$cell_barcode[qc_metrics$pass_qc])

after_n <- ncol(filtered)
saveRDS(filtered, output_rds)

qc_summary <- data.frame(
  metric = c("cells_before", "cells_after", "cells_removed", "min_features", "max_features", "min_counts", "max_counts", "max_percent_mt", "mt_pattern"),
  value = c(before_n, after_n, before_n - after_n, min_features, max_features, min_counts, max_counts, max_percent_mt, mt_pattern),
  stringsAsFactors = FALSE
)
write.table(qc_summary, file.path(tables_dir, "qc_summary.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)
write.table(qc_metrics, file.path(tables_dir, "qc_metrics.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

message(sprintf("Saved filtered Seurat object: %s", output_rds))
message(sprintf("Cells before QC: %s", before_n))
message(sprintf("Cells after QC: %s", after_n))
