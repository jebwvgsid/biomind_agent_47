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

if (is.null(manifest_path) || !nzchar(manifest_path)) {
  stop("Usage: Rscript find_markers_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/seurat_clustered.rds]")
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

parse_bool <- function(x, default = FALSE) {
  if (is.null(x) || length(x) == 0 || identical(x, "")) {
    return(default)
  }
  if (is.logical(x)) {
    return(isTRUE(x))
  }
  tolower(as.character(x[[1]])) %in% c("1", "true", "t", "yes", "y")
}

empty_markers <- function() {
  data.frame(
    gene = character(),
    cluster = character(),
    p_val = numeric(),
    avg_log2FC = numeric(),
    pct.1 = numeric(),
    pct.2 = numeric(),
    p_val_adj = numeric(),
    stringsAsFactors = FALSE
  )
}

cfg <- yaml::read_yaml(manifest_path)
if (is.null(cfg) || !is.list(cfg)) {
  stop("Manifest is empty or invalid YAML.")
}

output_dir <- cfg$output_dir %||% stop("Manifest must define output_dir.")
objects_dir <- file.path(output_dir, "objects")
tables_dir <- file.path(output_dir, "tables")
plots_dir <- file.path(output_dir, "plots")
ensure_dir(tables_dir)
ensure_dir(plots_dir)

input_rds <- input_rds %||% file.path(objects_dir, "seurat_clustered.rds")
obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}
if (!("seurat_clusters" %in% colnames(obj@meta.data))) {
  stop("Input object does not contain seurat_clusters.")
}

Idents(obj) <- "seurat_clusters"
marker_cfg <- cfg$markers %||% list()
only_pos <- parse_bool(marker_cfg$only_pos, default = TRUE)
min_pct <- as.numeric(marker_cfg$min_pct %||% 0.25)
logfc_threshold <- as.numeric(marker_cfg$logfc_threshold %||% 0.25)
test_use <- marker_cfg$test_use %||% "wilcox"
top_n <- as.integer(marker_cfg$top_n %||% 10)

cluster_levels <- levels(Idents(obj))
markers_df <- empty_markers()

if (length(cluster_levels) >= 2) {
  found <- suppressWarnings(
    FindAllMarkers(
      obj,
      only.pos = only_pos,
      min.pct = min_pct,
      logfc.threshold = logfc_threshold,
      test.use = test_use,
      verbose = FALSE
    )
  )
  if (nrow(found) > 0) {
    found$gene <- rownames(found)
    fc_col <- if ("avg_log2FC" %in% colnames(found)) "avg_log2FC" else if ("avg_logFC" %in% colnames(found)) "avg_logFC" else NULL
    if (!is.null(fc_col) && fc_col != "avg_log2FC") {
      found$avg_log2FC <- found[[fc_col]]
    }
    markers_df <- found
  }
}

write.table(markers_df, file.path(tables_dir, "cluster_markers.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

marker_summary <- data.frame(
  metric = c("n_clusters", "n_marker_rows", "test_use", "only_pos"),
  value = c(length(cluster_levels), nrow(markers_df), test_use, only_pos),
  stringsAsFactors = FALSE
)
write.table(marker_summary, file.path(tables_dir, "marker_summary.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

if (nrow(markers_df) > 0 && all(c("cluster", "gene") %in% colnames(markers_df))) {
  fc_col <- if ("avg_log2FC" %in% colnames(markers_df)) "avg_log2FC" else NULL
  top_markers <- markers_df
  if (!is.null(fc_col)) {
    top_markers <- top_markers %>%
      group_by(cluster) %>%
      arrange(desc(.data[[fc_col]]), .by_group = TRUE) %>%
      slice_head(n = top_n) %>%
      ungroup()
  } else {
    top_markers <- top_markers %>%
      group_by(cluster) %>%
      slice_head(n = top_n) %>%
      ungroup()
  }
  dot_features <- unique(top_markers$gene)
  if (length(dot_features) > 0) {
    p_dot <- DotPlot(obj, features = dot_features) + RotatedAxis()
    ggsave(file.path(plots_dir, "marker_dotplot.png"), plot = p_dot, width = 12, height = 6, dpi = 300)
  }
}

message(sprintf("Saved marker table: %s", file.path(tables_dir, "cluster_markers.tsv")))
message(sprintf("Marker rows: %s", nrow(markers_df)))
