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
if (is.null(manifest_path) || !nzchar(manifest_path)) {
  stop("Usage: Rscript ingest_scrna_seurat.R --manifest /abs/path/manifest.yaml")
}

suppressPackageStartupMessages({
  library(Seurat)
  library(Matrix)
  library(yaml)
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

fill_missing_meta <- function(values, fallback) {
  values <- as.character(values)
  if (length(values) == 0) {
    return(as.character(fallback))
  }
  if (length(fallback) == 1) {
    fallback <- rep(as.character(fallback), length(values))
  } else {
    fallback <- as.character(fallback)
  }
  missing_idx <- is.na(values) | values == ""
  values[missing_idx] <- fallback[missing_idx]
  values
}

read_manifest <- function(path) {
  cfg <- yaml::read_yaml(path)
  if (is.null(cfg) || !is.list(cfg)) {
    stop("Manifest is empty or invalid YAML.")
  }
  cfg
}

normalize_samples <- function(cfg) {
  if (!is.null(cfg$samples) && length(cfg$samples) > 0) {
    out <- lapply(cfg$samples, function(item) {
      list(
        sample_id = item$sample_id %||% basename(item$path %||% "sample"),
        path = item$path %||% "",
        demo_name = item$demo_name %||% cfg$demo_name %||% "pbmc_small_builtin"
      )
    })
    return(out)
  }

  list(list(
    sample_id = cfg$sample_id %||% cfg$project_name %||% "sample1",
    path = cfg$input_path %||% "",
    demo_name = cfg$demo_name %||% "pbmc_small_builtin"
  ))
}

extract_counts <- function(input) {
  if (inherits(input, "dgCMatrix") || inherits(input, "matrix")) {
    return(input)
  }
  if (is.list(input) && length(input) > 0) {
    if ("Gene Expression" %in% names(input)) {
      return(input[["Gene Expression"]])
    }
    return(input[[1]])
  }
  stop("Unsupported 10x object returned by reader.")
}

load_one_sample <- function(sample, input_mode, project_name) {
  sample_id <- sample$sample_id %||% "sample"
  if (input_mode == "public_demo") {
    demo_name <- sample$demo_name %||% "pbmc_small_builtin"
    if (demo_name != "pbmc_small_builtin") {
      stop("Only demo_name=pbmc_small_builtin is supported in v1.")
    }
    obj <- Seurat::pbmc_small
    obj$sample_id <- sample_id
    obj$orig.ident <- sample_id
    obj@project.name <- project_name
    return(obj)
  }

  sample_path <- sample$path %||% ""
  if (!nzchar(sample_path)) {
    stop(sprintf("Sample '%s' is missing path.", sample_id))
  }

  if (input_mode == "10x_mtx") {
    counts <- extract_counts(Read10X(data.dir = sample_path))
    obj <- CreateSeuratObject(counts = counts, project = project_name)
  } else if (input_mode == "10x_h5") {
    counts <- extract_counts(Read10X_h5(filename = sample_path))
    obj <- CreateSeuratObject(counts = counts, project = project_name)
  } else if (input_mode == "seurat_rds") {
    obj <- readRDS(sample_path)
    if (!inherits(obj, "Seurat")) {
      stop(sprintf("RDS at '%s' is not a Seurat object.", sample_path))
    }
  } else {
    stop(sprintf("Unsupported input_mode: %s", input_mode))
  }

  if (input_mode == "seurat_rds") {
    if ("sample_id" %in% colnames(obj@meta.data)) {
      fallback_sample <- if ("orig.ident" %in% colnames(obj@meta.data)) obj$orig.ident else sample_id
      obj$sample_id <- fill_missing_meta(obj$sample_id, fallback_sample)
    } else {
      fallback_sample <- if ("orig.ident" %in% colnames(obj@meta.data)) obj$orig.ident else sample_id
      obj$sample_id <- as.character(fallback_sample)
    }

    if ("orig.ident" %in% colnames(obj@meta.data)) {
      obj$orig.ident <- fill_missing_meta(obj$orig.ident, obj$sample_id)
    } else {
      obj$orig.ident <- as.character(obj$sample_id)
    }
  } else {
    obj$sample_id <- sample_id
    obj$orig.ident <- sample_id
  }

  obj@project.name <- project_name
  obj
}

cfg <- read_manifest(manifest_path)
project_name <- cfg$project_name %||% "scrna_project"
input_mode <- cfg$input_mode %||% stop("Manifest must define input_mode.")
output_dir <- cfg$output_dir %||% stop("Manifest must define output_dir.")

objects_dir <- file.path(output_dir, "objects")
tables_dir <- file.path(output_dir, "tables")
plots_dir <- file.path(output_dir, "plots")
ensure_dir(objects_dir)
ensure_dir(tables_dir)
ensure_dir(plots_dir)

samples <- normalize_samples(cfg)
seurat_objects <- lapply(samples, load_one_sample, input_mode = input_mode, project_name = project_name)

merged <- seurat_objects[[1]]
if (length(seurat_objects) > 1) {
  add_ids <- vapply(samples, function(x) x$sample_id %||% "sample", character(1))
  merged <- merge(
    x = seurat_objects[[1]],
    y = seurat_objects[2:length(seurat_objects)],
    add.cell.ids = add_ids,
    project = project_name
  )
}

raw_rds <- file.path(objects_dir, "seurat_raw.rds")
saveRDS(merged, raw_rds)

sample_manifest <- data.frame(
  sample_id = vapply(samples, function(x) x$sample_id %||% "", character(1)),
  path = vapply(samples, function(x) x$path %||% "", character(1)),
  input_mode = input_mode,
  stringsAsFactors = FALSE
)
write.table(sample_manifest, file.path(tables_dir, "sample_manifest.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

cell_counts <- as.data.frame(table(merged$sample_id), stringsAsFactors = FALSE)
colnames(cell_counts) <- c("sample_id", "cell_count")
write.table(cell_counts, file.path(tables_dir, "cell_counts.tsv"), sep = "\t", row.names = FALSE, quote = FALSE)

message(sprintf("Saved raw Seurat object: %s", raw_rds))
message(sprintf("Cells: %s", ncol(merged)))
message(sprintf("Features: %s", nrow(merged)))
