args <- commandArgs(trailingOnly = TRUE)

usage <- function() {
  cat(paste0(
    "Usage: Rscript run_sommer_multikernel_gxe.R ",
    "--model-config model_config.yaml --outdir out\n\n",
    "This production wrapper calls CRAN sommer. The model config must define ",
    "real input paths and the intended mixed-model route.\n"
  ))
}

if ("--help" %in% args || length(args) == 0) {
  usage()
  quit(status = 0)
}

getarg <- function(flag, default = NULL) {
  i <- match(flag, args)
  if (is.na(i)) default else args[[i + 1]]
}

config_file <- getarg("--model-config")
outdir <- getarg("--outdir", "genomic_prediction_sommer")
if (is.null(config_file)) {
  usage()
  stop("Missing required --model-config")
}

if (!requireNamespace("sommer", quietly = TRUE)) {
  stop("CRAN package sommer is required. Install with install.packages('sommer').")
}
if (!requireNamespace("yaml", quietly = TRUE)) {
  stop("CRAN package yaml is required. Install with install.packages('yaml').")
}
library(sommer)

read_table <- function(path) {
  read.delim(path, check.names = FALSE, stringsAsFactors = FALSE)
}

write_tsv <- function(path, data) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  write.table(data, path, sep = "\t", quote = FALSE, row.names = FALSE)
}

safe_cor <- function(x, y, method_name) {
  ok <- is.finite(x) & is.finite(y)
  if (sum(ok) < 2) return(NA_real_)
  suppressWarnings(cor(x[ok], y[ok], method = method_name))
}

calc_metrics <- function(y, pred, label) {
  ok <- is.finite(y) & is.finite(pred)
  yy <- y[ok]
  pp <- pred[ok]
  ss_res <- sum((yy - pp)^2)
  ss_tot <- sum((yy - mean(yy))^2)
  data.frame(
    model = label,
    pearson_r = safe_cor(yy, pp, "pearson"),
    spearman_rho = safe_cor(yy, pp, "spearman"),
    RMSE = sqrt(mean((yy - pp)^2)),
    MAE = mean(abs(yy - pp)),
    R2 = ifelse(ss_tot > 0, 1 - ss_res / ss_tot, NA_real_),
    n_samples = length(yy)
  )
}

numeric_candidates <- function(value, n) {
  out <- list()
  collect <- function(x, path = "") {
    if (is.null(x)) {
      return(NULL)
    }
    if (is.numeric(x) || is.integer(x)) {
      vec <- as.numeric(x)
      if (length(vec) == n) {
        out[[length(out) + 1]] <<- list(name = path, value = vec)
      }
      return(NULL)
    }
    if (is.matrix(x)) {
      for (j in seq_len(ncol(x))) {
        collect(x[, j], paste0(path, "[,", j, "]"))
      }
      return(NULL)
    }
    if (is.data.frame(x)) {
      for (nm in names(x)) {
        collect(x[[nm]], paste0(path, "$", nm))
      }
      return(NULL)
    }
    if (is.list(x)) {
      nms <- names(x)
      for (i in seq_along(x)) {
        nm <- if (!is.null(nms) && nzchar(nms[[i]])) nms[[i]] else as.character(i)
        collect(x[[i]], paste0(path, "$", nm))
      }
      return(NULL)
    }
    NULL
  }
  collect(value)
  out
}

extract_numeric_vector <- function(value, n, label) {
  candidates <- numeric_candidates(value, n)
  if (length(candidates) == 0) {
    stop(paste0(
      "Could not extract numeric ", label, " of length ", n,
      " from sommer result. Object class: ",
      paste(class(value), collapse = ",")
    ))
  }
  candidate_names <- vapply(candidates, function(item) item$name, character(1))
  preferred <- grep("fitted|pred|trait|yield|value", candidate_names, ignore.case = TRUE)
  selected <- if (length(preferred) > 0) preferred[[1]] else 1
  candidates[[selected]]$value
}

standardize_matrix <- function(m) {
  center <- apply(m, 2, function(x) mean(x, na.rm = TRUE))
  center[!is.finite(center)] <- 0
  for (j in seq_len(ncol(m))) {
    miss <- !is.finite(m[, j])
    if (any(miss)) m[miss, j] <- center[j]
  }
  scale <- apply(m, 2, sd)
  scale[!is.finite(scale) | scale == 0] <- 1
  sweep(sweep(m, 2, center, "-"), 2, scale, "/")
}

cfg <- yaml::read_yaml(config_file)
required <- c("genotype", "phenotype", "trait")
missing <- required[!vapply(required, function(k) k %in% names(cfg), logical(1))]
if (length(missing) > 0) {
  stop(paste("model_config missing required fields:", paste(missing, collapse = ",")))
}

method <- if ("method" %in% names(cfg)) cfg$method else "single_kernel_gblup"
if (method != "single_kernel_gblup") {
  stop("This wrapper currently supports method: single_kernel_gblup. For custom sommer formulas, provide a dedicated wrapper.")
}

geno <- read_table(cfg$genotype)
pheno <- read_table(cfg$phenotype)
trait <- cfg$trait
if (!"sample_id" %in% names(geno) || !"sample_id" %in% names(pheno)) {
  stop("Genotype and phenotype tables must contain sample_id")
}
if (!trait %in% names(pheno)) {
  stop(paste("Phenotype missing trait column:", trait))
}

data <- merge(pheno, geno, by = "sample_id", all = FALSE)
marker_cols <- setdiff(names(geno), "sample_id")
marker_matrix <- as.matrix(data[, marker_cols, drop = FALSE])
mode(marker_matrix) <- "numeric"
y <- as.numeric(data[[trait]])
phenotyped <- is.finite(y)
data <- data[phenotyped, , drop = FALSE]
marker_matrix <- marker_matrix[phenotyped, , drop = FALSE]
y <- y[phenotyped]
data[[trait]] <- y

fixed_effects <- character(0)
if ("fixed_effects" %in% names(cfg) && length(cfg$fixed_effects) > 0) {
  fixed_effects <- unlist(cfg$fixed_effects)
  missing_fixed <- setdiff(fixed_effects, names(data))
  if (length(missing_fixed) > 0) {
    stop(paste("Fixed-effect columns missing:", paste(missing_fixed, collapse = ",")))
  }
}

fixed_rhs <- if (length(fixed_effects) > 0) paste(fixed_effects, collapse = " + ") else "1"
fixed_formula <- as.formula(paste(trait, "~", fixed_rhs))
scaled <- standardize_matrix(marker_matrix)
K <- tcrossprod(scaled) / max(1, ncol(scaled))
rownames(K) <- data$sample_id
colnames(K) <- data$sample_id
data$sample_id <- factor(data$sample_id, levels = rownames(K))

fit <- sommer::mmer(
  fixed = fixed_formula,
  random = ~ vsr(sample_id, Gu = K),
  rcov = ~ units,
  data = data,
  verbose = FALSE
)

fitted_obj <- tryCatch(fitted(fit), error = function(e) NULL)
if (is.null(fitted_obj) && !is.null(fit$fitted)) {
  fitted_obj <- fit$fitted
}
pred <- extract_numeric_vector(fitted_obj, length(y), "fitted values")
out <- outdir
tables <- file.path(out, "tables")
model_dir <- file.path(out, "model")
repro_dir <- file.path(out, "reproducibility")
dir.create(tables, recursive = TRUE, showWarnings = FALSE)
dir.create(model_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(repro_dir, recursive = TRUE, showWarnings = FALSE)

prediction_df <- data.frame(
  sample_id = as.character(data$sample_id),
  observed_trait = y,
  predicted_trait = pred,
  GEBV = pred,
  residual = y - pred,
  model = "sommer_single_kernel_gblup"
)
write_tsv(file.path(tables, "prediction.tsv"), prediction_df)
ranked_df <- prediction_df[order(prediction_df$GEBV, decreasing = TRUE), ]
ranked_df$rank <- seq_len(nrow(ranked_df))
write_tsv(file.path(tables, "ranked_breeding_materials.tsv"), ranked_df)
write_tsv(file.path(tables, "model_performance.tsv"), calc_metrics(y, pred, "sommer_single_kernel_gblup"))

vc <- tryCatch(summary(fit)$varcomp, error = function(e) NULL)
if (!is.null(vc)) {
  write_tsv(file.path(tables, "variance_components.tsv"), as.data.frame(vc))
}

saveRDS(
  list(
    package = "sommer",
    method = method,
    trait = trait,
    fixed_effects = fixed_effects,
    marker_cols = marker_cols,
    fit = fit
  ),
  file.path(model_dir, "final_model.rds")
)

capture.output(sessionInfo(), file = file.path(repro_dir, "sessionInfo.txt"))
writeLines(paste("Rscript", paste(commandArgs(trailingOnly = TRUE), collapse = " ")), file.path(repro_dir, "commands.sh"))
writeLines(
  c(
    "# Crop GS sommer Multi-Kernel GxE",
    "",
    paste("Method:", method),
    paste("Trait:", trait),
    paste("Samples:", length(y)),
    paste("Markers:", length(marker_cols)),
    paste("Fixed effects:", ifelse(length(fixed_effects) > 0, paste(fixed_effects, collapse = ", "), "none")),
    "",
    "This wrapper runs a CRAN sommer single-kernel GBLUP route from an explicit model config."
  ),
  file.path(out, "report.md")
)

cat(paste0("wrote sommer predictions -> ", file.path(tables, "prediction.tsv"), "\n"))
