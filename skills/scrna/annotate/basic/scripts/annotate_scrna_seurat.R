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
markers_table_arg <- get_arg("--markers-table")

if (is.null(manifest_path) || !nzchar(manifest_path)) {
  stop("Usage: Rscript annotate_scrna_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/seurat_clustered.rds]")
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
  unique(trimws(as.character(unlist(x))))
}

normalize_cluster_labels <- function(x) {
  if (is.null(x) || length(x) == 0) {
    return(setNames(character(), character()))
  }
  if (is.list(x) && !is.null(names(x)) && any(nzchar(names(x)))) {
    values <- vapply(x, function(item) as.character(item %||% ""), character(1))
    names(values) <- names(x)
    return(values[nzchar(names(values))])
  }
  out <- c()
  for (item in x) {
    cluster_id <- as.character(item$cluster %||% item$id %||% "")
    label <- as.character(item$label %||% item$cell_type %||% "")
    if (nzchar(cluster_id) && nzchar(label)) {
      out[[cluster_id]] <- label
    }
  }
  out
}

normalize_marker_rules <- function(x) {
  if (is.null(x) || length(x) == 0 || !is.list(x)) {
    return(list())
  }
  if (!is.null(names(x)) && any(nzchar(names(x)))) {
    out <- list()
    for (nm in names(x)) {
      genes <- normalize_string_vector(x[[nm]])
      if (length(genes) > 0) {
        out[[nm]] <- genes
      }
    }
    return(out)
  }
  out <- list()
  for (item in x) {
    label <- as.character(item$label %||% item$name %||% "")
    genes <- normalize_string_vector(item$genes)
    if (nzchar(label) && length(genes) > 0) {
      out[[label]] <- genes
    }
  }
  out
}

load_reference_object <- function(annotation_cfg) {
  ref_rds <- annotation_cfg$reference_rds %||% NULL
  ref_builtin <- tolower(annotation_cfg$reference_builtin %||% "")
  if (!is.null(ref_rds) && nzchar(ref_rds)) {
    return(readRDS(ref_rds))
  }
  if (!nzchar(ref_builtin)) {
    return(NULL)
  }
  if (!requireNamespace("celldex", quietly = TRUE)) {
    stop("annotation.reference_builtin requested but the 'celldex' package is not installed.")
  }
  builtin_map <- list(
    hpca = celldex::HumanPrimaryCellAtlasData,
    blueprint_encode = celldex::BlueprintEncodeData,
    mouse_rna_seq = celldex::MouseRNAseqData,
    immgen = celldex::ImmGenData
  )
  loader <- builtin_map[[ref_builtin]]
  if (is.null(loader)) {
    stop(sprintf("Unsupported annotation.reference_builtin: %s", ref_builtin))
  }
  loader()
}

top_markers_by_cluster <- function(markers_path) {
  if (is.null(markers_path) || !nzchar(markers_path) || !file.exists(markers_path)) {
    return(setNames(character(), character()))
  }
  markers <- tryCatch(
    read.table(markers_path, header = TRUE, sep = "\t", quote = "", stringsAsFactors = FALSE, check.names = FALSE),
    error = function(e) NULL
  )
  if (is.null(markers) || nrow(markers) == 0 || !all(c("cluster", "gene") %in% colnames(markers))) {
    return(setNames(character(), character()))
  }
  fc_col <- if ("avg_log2FC" %in% colnames(markers)) "avg_log2FC" else if ("avg_logFC" %in% colnames(markers)) "avg_logFC" else NULL
  if (!is.null(fc_col)) {
    markers <- markers[order(markers$cluster, -markers[[fc_col]]), , drop = FALSE]
  }
  markers <- markers[!duplicated(markers$cluster), , drop = FALSE]
  out <- markers$gene
  names(out) <- as.character(markers$cluster)
  out
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

input_rds <- input_rds %||% file.path(objects_dir, "seurat_clustered.rds")
output_rds <- output_rds_arg %||% file.path(objects_dir, "seurat_annotated.rds")
markers_table <- markers_table_arg %||% file.path(tables_dir, "cluster_markers.tsv")

obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}
if (!("seurat_clusters" %in% colnames(obj@meta.data))) {
  stop("Input object does not contain seurat_clusters.")
}

annotation_cfg <- cfg$annotation %||% list()
method <- tolower(annotation_cfg$method %||% "marker-first")
cluster_ids <- sort(unique(as.character(obj$seurat_clusters)))
annotation_method <- rep("unresolved_cluster_label", length(cluster_ids))
cluster_map <- setNames(paste0("Cluster_", cluster_ids), cluster_ids)
evidence <- rep("no marker rules or explicit reference provided", length(cluster_ids))

direct_labels <- normalize_cluster_labels(annotation_cfg$cluster_labels)
marker_rules <- normalize_marker_rules(annotation_cfg$marker_rules)

if (length(direct_labels) > 0) {
  matched_clusters <- intersect(names(direct_labels), cluster_ids)
  cluster_map[matched_clusters] <- direct_labels[matched_clusters]
  annotation_method[match(matched_clusters, cluster_ids)] <- "cluster_label_map"
  evidence[match(matched_clusters, cluster_ids)] <- "manifest.annotation.cluster_labels"
} else if (length(marker_rules) > 0 && method == "marker-first") {
  assay_name <- DefaultAssay(obj)
  avg_result <- suppressWarnings(AverageExpression(obj, group.by = "seurat_clusters", assays = assay_name, slot = "data", verbose = FALSE))
  avg_expr <- if (is.list(avg_result)) avg_result[[assay_name]] else avg_result
  if (!is.null(avg_expr) && ncol(avg_expr) > 0) {
    for (cluster_id in cluster_ids) {
      best_label <- NULL
      best_score <- -Inf
      best_genes <- character()
      if (!(cluster_id %in% colnames(avg_expr))) {
        next
      }
      for (label in names(marker_rules)) {
        genes <- intersect(marker_rules[[label]], rownames(avg_expr))
        if (length(genes) == 0) {
          next
        }
        score <- mean(as.numeric(avg_expr[genes, cluster_id, drop = TRUE]), na.rm = TRUE)
        if (is.finite(score) && score > best_score) {
          best_score <- score
          best_label <- label
          best_genes <- genes
        }
      }
      if (!is.null(best_label) && nzchar(best_label)) {
        cluster_map[[cluster_id]] <- best_label
        annotation_method[match(cluster_id, cluster_ids)] <- "marker_rules"
        evidence[match(cluster_id, cluster_ids)] <- paste(best_genes, collapse = ",")
      }
    }
  }
} else if (method == "singler" || !is.null(annotation_cfg$reference_rds) || nzchar(annotation_cfg$reference_builtin %||% "")) {
  if (!requireNamespace("SingleR", quietly = TRUE)) {
    stop("SingleR-based annotation requested but the 'SingleR' package is not installed.")
  }
  if (!requireNamespace("SummarizedExperiment", quietly = TRUE)) {
    stop("SingleR-based annotation requested but the 'SummarizedExperiment' package is not installed.")
  }
  ref <- load_reference_object(annotation_cfg)
  if (is.null(ref)) {
    stop("SingleR annotation requested but no explicit reference was supplied.")
  }
  labels_col <- annotation_cfg$reference_labels_column %||% annotation_cfg$reference_label_field %||% "label.main"
  if (!(labels_col %in% colnames(SummarizedExperiment::colData(ref)))) {
    stop(sprintf("Reference labels column '%s' was not found in the supplied reference.", labels_col))
  }
  assay_name <- DefaultAssay(obj)
  pred <- SingleR::SingleR(
    test = GetAssayData(obj, assay = assay_name, slot = "data"),
    ref = ref,
    labels = SummarizedExperiment::colData(ref)[[labels_col]]
  )
  pred_labels <- as.character(pred$labels)
  split_labels <- split(pred_labels, as.character(obj$seurat_clusters))
  for (cluster_id in names(split_labels)) {
    label_table <- sort(table(split_labels[[cluster_id]]), decreasing = TRUE)
    cluster_map[[cluster_id]] <- names(label_table)[1]
    annotation_method[match(cluster_id, cluster_ids)] <- "singler_cluster_majority"
    evidence[match(cluster_id, cluster_ids)] <- sprintf("majority_fraction=%.3f", max(label_table) / sum(label_table))
  }
}

cell_clusters <- as.character(obj$seurat_clusters)
cell_types <- unname(cluster_map[cell_clusters])
cell_types[is.na(cell_types) | cell_types == ""] <- paste0("Cluster_", cell_clusters[is.na(cell_types) | cell_types == ""])
obj$cell_type <- factor(cell_types)

top_markers <- top_markers_by_cluster(markers_table)
annotation_table <- data.frame(
  cluster = cluster_ids,
  cell_type = unname(cluster_map[cluster_ids]),
  annotation_method = annotation_method,
  evidence = evidence,
  top_marker = unname(top_markers[cluster_ids]),
  stringsAsFactors = FALSE
)
write.table(annotation_table, file.path(tables_dir, "celltype_annotation.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

if ("umap" %in% names(obj@reductions)) {
  p_umap <- DimPlot(obj, reduction = "umap", group.by = "cell_type", label = TRUE)
  ggsave(file.path(plots_dir, "umap_celltypes.png"), plot = p_umap, width = 8, height = 6, dpi = 300)
}

saveRDS(obj, output_rds)

message(sprintf("Saved annotated Seurat object: %s", output_rds))
message(sprintf("Annotation table: %s", file.path(tables_dir, "celltype_annotation.tsv")))
