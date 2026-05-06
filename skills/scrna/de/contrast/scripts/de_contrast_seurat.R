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
  stop("Usage: Rscript de_contrast_seurat.R --manifest /abs/path/manifest.yaml [--input-rds /abs/path/seurat_annotated.rds]")
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

guess_group_by <- function(obj) {
  candidates <- c("sample_id", "condition", "orig.ident", "group")
  for (col in candidates) {
    if (col %in% colnames(obj@meta.data)) {
      values <- unique(as.character(obj@meta.data[[col]]))
      values <- values[!is.na(values) & values != ""]
      if (length(values) >= 2) {
        return(col)
      }
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
ensure_dir(tables_dir)
ensure_dir(plots_dir)

default_input <- file.path(objects_dir, "seurat_annotated.rds")
if (!file.exists(default_input)) {
  default_input <- file.path(objects_dir, "seurat_clustered.rds")
}
input_rds <- input_rds %||% default_input

obj <- readRDS(input_rds)
if (!inherits(obj, "Seurat")) {
  stop("Input RDS is not a Seurat object.")
}

de_cfg <- cfg$de %||% list()
group_by <- de_cfg$group_by %||% guess_group_by(obj)
if (is.null(group_by) || !nzchar(group_by)) {
  stop("Unable to infer a DE grouping column. Set de.group_by in the manifest.")
}
if (!(group_by %in% colnames(obj@meta.data))) {
  stop(sprintf("DE grouping column '%s' was not found in the object metadata.", group_by))
}

subset_column <- de_cfg$subset_column %||% NULL
subset_values <- normalize_string_vector(de_cfg$subset_value)
if (!is.null(subset_column) && nzchar(subset_column) && length(subset_values) > 0) {
  if (!(subset_column %in% colnames(obj@meta.data))) {
    stop(sprintf("DE subset column '%s' was not found in the object metadata.", subset_column))
  }
  obj <- subset(obj, cells = rownames(obj@meta.data)[as.character(obj@meta.data[[subset_column]]) %in% subset_values])
}

Idents(obj) <- group_by
available_idents <- levels(Idents(obj))
available_idents <- available_idents[nzchar(available_idents)]
ident_1 <- as.character(de_cfg$ident_1 %||% "")
ident_2 <- as.character(de_cfg$ident_2 %||% "")

if (!nzchar(ident_1) || !nzchar(ident_2)) {
  if (length(available_idents) == 2) {
    ident_1 <- available_idents[[1]]
    ident_2 <- available_idents[[2]]
  } else {
    stop("DE contrast labels are ambiguous. Set de.ident_1 and de.ident_2 in the manifest.")
  }
}

test_use <- de_cfg$test_use %||% "wilcox"
min_pct <- as.numeric(de_cfg$min_pct %||% 0.1)
logfc_threshold <- as.numeric(de_cfg$logfc_threshold %||% 0.25)
latent_vars <- normalize_string_vector(de_cfg$latent_vars)
latent_vars <- latent_vars[latent_vars %in% colnames(obj@meta.data)]

results <- suppressWarnings(
  FindMarkers(
    obj,
    ident.1 = ident_1,
    ident.2 = ident_2,
    test.use = test_use,
    min.pct = min_pct,
    logfc.threshold = logfc_threshold,
    latent.vars = if (length(latent_vars) > 0) latent_vars else NULL,
    verbose = FALSE
  )
)

results$gene <- rownames(results)
results$comparison <- paste(ident_1, "vs", ident_2)
results$group_by <- group_by
if (!is.null(subset_column) && nzchar(subset_column) && length(subset_values) > 0) {
  results$subset_column <- subset_column
  results$subset_value <- paste(subset_values, collapse = ",")
}

write.table(results, file.path(tables_dir, "de_results.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

fc_col <- if ("avg_log2FC" %in% colnames(results)) "avg_log2FC" else if ("avg_logFC" %in% colnames(results)) "avg_logFC" else NULL
sig_mask <- if (!is.null(fc_col) && "p_val_adj" %in% colnames(results)) {
  !is.na(results$p_val_adj) & results$p_val_adj < 0.05
} else {
  rep(FALSE, nrow(results))
}

summary_df <- data.frame(
  metric = c("group_by", "ident_1", "ident_2", "n_rows", "n_significant"),
  value = c(group_by, ident_1, ident_2, nrow(results), sum(sig_mask)),
  stringsAsFactors = FALSE
)
write.table(summary_df, file.path(tables_dir, "de_summary.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

if (nrow(results) > 0 && !is.null(fc_col) && "p_val_adj" %in% colnames(results)) {
  volcano_df <- results
  volcano_df$neg_log10_padj <- -log10(pmax(volcano_df$p_val_adj, .Machine$double.xmin))
  volcano_df$is_significant <- volcano_df$p_val_adj < 0.05 & abs(volcano_df[[fc_col]]) >= logfc_threshold
  p_volcano <- ggplot(volcano_df, aes(x = .data[[fc_col]], y = neg_log10_padj, color = is_significant)) +
    geom_point(alpha = 0.7, size = 1.2) +
    scale_color_manual(values = c("FALSE" = "grey70", "TRUE" = "#d7301f")) +
    labs(
      x = fc_col,
      y = "-log10 adjusted p-value",
      title = paste("DE:", ident_1, "vs", ident_2)
    ) +
    theme_minimal(base_size = 12)
  ggsave(file.path(plots_dir, "de_volcano.png"), plot = p_volcano, width = 8, height = 6, dpi = 300)
}

message(sprintf("Saved DE results: %s", file.path(tables_dir, "de_results.tsv")))
message(sprintf("Comparison: %s vs %s", ident_1, ident_2))
