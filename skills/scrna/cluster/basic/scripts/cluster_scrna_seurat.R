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
  stop("Usage: Rscript cluster_scrna_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/seurat_norm.rds]")
}

suppressPackageStartupMessages({
  library(Seurat)
  library(yaml)
  library(ggplot2)
  library(patchwork)
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

parse_dims <- function(x, default = 1:20) {
  if (is.null(x) || length(x) == 0 || identical(x, "")) {
    return(default)
  }
  if (is.numeric(x)) {
    return(as.integer(x))
  }
  if (is.character(x) && length(x) == 1) {
    text <- trimws(x)
    if (grepl("^\\d+:\\d+$", text)) {
      bounds <- as.integer(strsplit(text, ":")[[1]])
      return(seq.int(bounds[1], bounds[2]))
    }
    return(as.integer(trimws(unlist(strsplit(text, ",")))))
  }
  as.integer(unlist(x))
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

choose_sample_group <- function(obj) {
  for (col in c("sample_id", "orig.ident")) {
    if (col %in% colnames(obj@meta.data)) {
      return(col)
    }
  }
  NULL
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

input_rds <- input_rds %||% file.path(objects_dir, "seurat_norm.rds")
output_rds <- output_rds_arg %||% file.path(objects_dir, "seurat_clustered.rds")

obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}

cluster_cfg <- cfg$cluster %||% list()
requested_pcs <- as.integer(cluster_cfg$pca_npcs %||% 30)
dims <- parse_dims(cluster_cfg$cluster_dims %||% 1:20, default = 1:20)
resolution <- as.numeric(cluster_cfg$resolution %||% 0.6)
run_tsne <- parse_bool(cluster_cfg$run_tsne, default = TRUE)

features <- VariableFeatures(obj)
if (length(features) == 0) {
  features <- rownames(obj)
}

if (ncol(obj) < 3 || length(features) < 2) {
  stop("Clustering requires at least 3 cells and 2 features.")
}

npcs <- min(requested_pcs, max(ncol(obj) - 1, 2), length(features))
if (npcs < 2) {
  stop("Unable to compute at least 2 principal components for clustering.")
}

obj <- RunPCA(obj, features = features, npcs = npcs, verbose = FALSE)
usable_dims <- dims[dims >= 1 & dims <= npcs]
if (length(usable_dims) == 0) {
  usable_dims <- seq_len(min(20, npcs))
}

png(file.path(plots_dir, "pca_elbow.png"), width = 1600, height = 1200, res = 200)
print(ElbowPlot(obj, ndims = min(npcs, 50)))
dev.off()

obj <- FindNeighbors(obj, dims = usable_dims, verbose = FALSE)
obj <- FindClusters(obj, resolution = resolution, verbose = FALSE)
obj <- tryCatch(
  RunUMAP(obj, dims = usable_dims, verbose = FALSE),
  error = function(e) {
    message(sprintf("Skipping UMAP: %s", e$message))
    obj
  }
)

if ("umap" %in% names(obj@reductions)) {
  p_clusters <- DimPlot(obj, reduction = "umap", group.by = "seurat_clusters", label = TRUE)
  ggsave(file.path(plots_dir, "umap_clusters.png"), plot = p_clusters, width = 8, height = 6, dpi = 300)
}

sample_group <- choose_sample_group(obj)
if (!is.null(sample_group) && "umap" %in% names(obj@reductions)) {
  p_sample <- DimPlot(obj, reduction = "umap", group.by = sample_group)
  ggsave(file.path(plots_dir, "umap_sample.png"), plot = p_sample, width = 8, height = 6, dpi = 300)
}

if (run_tsne) {
  tsne_perplexity <- floor((ncol(obj) - 1) / 3)
  if (tsne_perplexity >= 1) {
    obj <- tryCatch(
      RunTSNE(obj, dims = usable_dims, perplexity = min(30, tsne_perplexity), check_duplicates = FALSE, verbose = FALSE),
      error = function(e) {
        message(sprintf("Skipping t-SNE: %s", e$message))
        obj
      }
    )
    if ("tsne" %in% names(obj@reductions)) {
      p_tsne <- DimPlot(obj, reduction = "tsne", group.by = "seurat_clusters", label = TRUE)
      ggsave(file.path(plots_dir, "tsne_clusters.png"), plot = p_tsne, width = 8, height = 6, dpi = 300)
    }
  } else {
    message("Skipping t-SNE because the object has too few cells for a stable perplexity.")
  }
}

cluster_sizes <- as.data.frame(table(obj$seurat_clusters), stringsAsFactors = FALSE)
colnames(cluster_sizes) <- c("cluster", "cell_count")
write.table(cluster_sizes, file.path(tables_dir, "cluster_sizes.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

cluster_summary <- data.frame(
  metric = c("pca_npcs", "dims_used", "resolution", "n_clusters"),
  value = c(
    npcs,
    paste(usable_dims, collapse = ","),
    resolution,
    nrow(cluster_sizes)
  ),
  stringsAsFactors = FALSE
)
write.table(cluster_summary, file.path(tables_dir, "cluster_summary.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

saveRDS(obj, output_rds)

message(sprintf("Saved clustered Seurat object: %s", output_rds))
message(sprintf("Clusters: %s", nrow(cluster_sizes)))
