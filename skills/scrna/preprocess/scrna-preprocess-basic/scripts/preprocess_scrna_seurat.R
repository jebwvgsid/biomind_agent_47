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
  stop("Usage: Rscript preprocess_scrna_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/seurat_qc.rds]")
}

suppressPackageStartupMessages({
  library(Seurat)
  library(yaml)
  library(ggplot2)
  library(dplyr)
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

normalize_string_vector <- function(x) {
  if (is.null(x) || length(x) == 0) {
    return(character())
  }
  if (is.character(x) && length(x) == 1) {
    return(trimws(unlist(strsplit(x, ","))))
  }
  unique(trimws(as.character(unlist(x))))
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

input_rds <- input_rds %||% file.path(objects_dir, "seurat_qc.rds")
output_rds <- output_rds_arg %||% file.path(objects_dir, "seurat_norm.rds")

obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}

pre_cfg <- cfg$preprocess %||% list()
method <- tolower(pre_cfg$method %||% "lognorm")
nfeatures <- as.integer(pre_cfg$nfeatures %||% 2000)
scale_features_mode <- tolower(pre_cfg$scale_features %||% "variable")
regress_vars <- normalize_string_vector(pre_cfg$regress_vars)
regress_vars <- regress_vars[regress_vars %in% colnames(obj@meta.data)]

if (method == "sctransform") {
  if (!requireNamespace("sctransform", quietly = TRUE)) {
    stop("SCTransform requested but the 'sctransform' package is not installed.")
  }
  obj <- SCTransform(
    obj,
    vars.to.regress = if (length(regress_vars) > 0) regress_vars else NULL,
    variable.features.n = nfeatures,
    verbose = FALSE
  )
} else {
  obj <- NormalizeData(obj, verbose = FALSE)
  obj <- FindVariableFeatures(
    obj,
    selection.method = "vst",
    nfeatures = nfeatures,
    verbose = FALSE
  )
  scale_features <- VariableFeatures(obj)
  if (scale_features_mode == "all" || length(scale_features) == 0) {
    scale_features <- rownames(obj)
  }
  obj <- ScaleData(
    obj,
    features = scale_features,
    vars.to.regress = if (length(regress_vars) > 0) regress_vars else NULL,
    verbose = FALSE
  )
}

variable_features <- VariableFeatures(obj)
write.table(
  data.frame(gene = variable_features, stringsAsFactors = FALSE),
  file.path(tables_dir, "variable_features.tsv"),
  sep = "\t",
  row.names = FALSE,
  quote = FALSE
)

summary_df <- data.frame(
  metric = c("method", "n_variable_features", "scale_features", "regress_vars"),
  value = c(
    method,
    length(variable_features),
    scale_features_mode,
    paste(regress_vars, collapse = ",")
  ),
  stringsAsFactors = FALSE
)
write.table(summary_df, file.path(tables_dir, "preprocess_summary.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

if (length(variable_features) > 0) {
  p_var <- VariableFeaturePlot(obj)
  top_points <- head(variable_features, 10)
  if (length(top_points) > 0) {
    p_var <- LabelPoints(plot = p_var, points = top_points, repel = TRUE)
  }
  ggsave(file.path(plots_dir, "variable_features.png"), plot = p_var, width = 8, height = 6, dpi = 300)
}

saveRDS(obj, output_rds)

message(sprintf("Saved preprocessed Seurat object: %s", output_rds))
message(sprintf("Method: %s", method))
message(sprintf("Variable features: %s", length(variable_features)))
