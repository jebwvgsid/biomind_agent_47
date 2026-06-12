args <- commandArgs(trailingOnly = TRUE)

usage <- function() {
  cat(paste0(
    "Usage: Rscript run_rrblup_package.R ",
    "--genotype genotype.tsv --phenotype phenotype.tsv --trait trait ",
    "--outdir out [--method rrblup|gblup] [--folds 5] ",
    "[--covariates covariates.tsv --covariate-cols PC1,PC2] ",
    "[--group-column family]\n\n",
    "This production wrapper calls CRAN rrBLUP. It is intended for formal ",
    "crop genomic prediction when rrBLUP is the selected published method.\n"
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

geno_file <- getarg("--genotype")
pheno_file <- getarg("--phenotype")
trait <- getarg("--trait")
outdir <- getarg("--outdir", "genomic_prediction_rrblup")
method <- tolower(getarg("--method", "gblup"))
folds <- as.integer(getarg("--folds", "5"))
cov_file <- getarg("--covariates")
cov_cols_arg <- getarg("--covariate-cols", "")
group_col <- getarg("--group-column")

if (is.null(geno_file) || is.null(pheno_file) || is.null(trait)) {
  usage()
  stop("Missing required --genotype, --phenotype, or --trait")
}
if (!method %in% c("rrblup", "gblup")) {
  stop("--method must be rrblup or gblup")
}

if (!requireNamespace("rrBLUP", quietly = TRUE)) {
  stop("CRAN package rrBLUP is required. Install with install.packages('rrBLUP').")
}

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

calc_metrics <- function(y, pred, label, fold = NA) {
  ok <- is.finite(y) & is.finite(pred)
  yy <- y[ok]
  pp <- pred[ok]
  if (length(yy) == 0) {
    return(data.frame(
      model = label, fold = fold, pearson_r = NA_real_, spearman_rho = NA_real_,
      RMSE = NA_real_, MAE = NA_real_, R2 = NA_real_, n_samples = 0
    ))
  }
  ss_res <- sum((yy - pp)^2)
  ss_tot <- sum((yy - mean(yy))^2)
  data.frame(
    model = label,
    fold = fold,
    pearson_r = safe_cor(yy, pp, "pearson"),
    spearman_rho = safe_cor(yy, pp, "spearman"),
    RMSE = sqrt(mean((yy - pp)^2)),
    MAE = mean(abs(yy - pp)),
    R2 = ifelse(ss_tot > 0, 1 - ss_res / ss_tot, NA_real_),
    n_samples = length(yy)
  )
}

standardize_train_test <- function(m, train_idx) {
  center <- apply(m[train_idx, , drop = FALSE], 2, function(x) mean(x, na.rm = TRUE))
  center[!is.finite(center)] <- 0
  m_imp <- m
  for (j in seq_len(ncol(m_imp))) {
    miss <- !is.finite(m_imp[, j])
    if (any(miss)) m_imp[miss, j] <- center[j]
  }
  scale <- apply(m_imp[train_idx, , drop = FALSE], 2, sd)
  scale[!is.finite(scale) | scale == 0] <- 1
  list(matrix = sweep(sweep(m_imp, 2, center, "-"), 2, scale, "/"), center = center, scale = scale)
}

make_folds <- function(data, folds, group_col) {
  n <- nrow(data)
  if (n < 3) stop("Need at least 3 phenotyped samples")
  set.seed(20260527)
  if (!is.null(group_col) && group_col %in% names(data)) {
    groups <- unique(data[[group_col]])
    groups <- groups[order(runif(length(groups)))]
    k <- max(2, min(folds, length(groups)))
    group_fold <- setNames(rep(seq_len(k), length.out = length(groups)), groups)
    return(as.integer(group_fold[as.character(data[[group_col]])]))
  }
  k <- max(2, min(folds, n))
  sample(rep(seq_len(k), length.out = n))
}

fixed_matrix <- function(data, covariate_cols) {
  if (length(covariate_cols) == 0) {
    x <- matrix(1, nrow(data), 1)
    colnames(x) <- "Intercept"
    return(x)
  }
  missing_cols <- setdiff(covariate_cols, names(data))
  if (length(missing_cols) > 0) {
    stop(paste("Covariate columns missing:", paste(missing_cols, collapse = ",")))
  }
  formula <- as.formula(paste("~", paste(covariate_cols, collapse = "+")))
  model.matrix(formula, data = data)
}

fit_rrblup_marker <- function(y, marker_matrix, x_fixed) {
  rrBLUP::mixed.solve(y = y, Z = marker_matrix, X = x_fixed)
}

predict_rrblup_marker <- function(fit, marker_matrix, x_fixed) {
  as.vector(x_fixed %*% fit$beta + marker_matrix %*% fit$u)
}

fit_predict_gblup <- function(y_train, k_train, k_test_train, x_train, x_test) {
  fit <- rrBLUP::mixed.solve(y = y_train, K = k_train, X = x_train)
  lambda <- fit$Ve / fit$Vu
  lhs <- k_train + diag(lambda, nrow(k_train))
  rhs <- y_train - as.vector(x_train %*% fit$beta)
  alpha <- tryCatch(
    solve(lhs, rhs),
    error = function(e) {
      if (!requireNamespace("MASS", quietly = TRUE)) stop(e)
      MASS::ginv(lhs) %*% rhs
    }
  )
  as.vector(x_test %*% fit$beta + k_test_train %*% alpha)
}

geno <- read_table(geno_file)
pheno <- read_table(pheno_file)
if (!"sample_id" %in% names(geno) || !"sample_id" %in% names(pheno)) {
  stop("Genotype and phenotype tables must contain sample_id")
}
if (!trait %in% names(pheno)) {
  stop(paste("Phenotype missing trait column:", trait))
}

data <- merge(pheno, geno, by = "sample_id", all = FALSE)
if (!is.null(cov_file)) {
  covariates <- read_table(cov_file)
  if (!"sample_id" %in% names(covariates)) stop("Covariates table must contain sample_id")
  data <- merge(data, covariates, by = "sample_id", all.x = TRUE)
}

marker_cols <- setdiff(names(geno), "sample_id")
if (length(marker_cols) == 0) stop("No marker columns found in genotype table")
marker_matrix <- as.matrix(data[, marker_cols, drop = FALSE])
mode(marker_matrix) <- "numeric"
y <- as.numeric(data[[trait]])
phenotyped <- is.finite(y)
data <- data[phenotyped, , drop = FALSE]
marker_matrix <- marker_matrix[phenotyped, , drop = FALSE]
y <- y[phenotyped]

covariate_cols <- character(0)
if (nzchar(cov_cols_arg)) {
  covariate_cols <- trimws(strsplit(cov_cols_arg, ",", fixed = TRUE)[[1]])
  covariate_cols <- covariate_cols[nzchar(covariate_cols)]
}
x_all <- fixed_matrix(data, covariate_cols)
fold_id <- make_folds(data, folds, group_col)

pred <- rep(NA_real_, length(y))
fold_rows <- list()
marker_effect_accumulator <- matrix(NA_real_, nrow = length(marker_cols), ncol = length(unique(fold_id)))
rownames(marker_effect_accumulator) <- marker_cols

for (fold in sort(unique(fold_id))) {
  test_idx <- which(fold_id == fold)
  train_idx <- which(fold_id != fold)
  scaled <- standardize_train_test(marker_matrix, train_idx)
  x_train <- x_all[train_idx, , drop = FALSE]
  x_test <- x_all[test_idx, , drop = FALSE]
  keep_fixed <- apply(x_train, 2, function(col) any(is.finite(col)) && sd(col) > 0)
  keep_fixed[1] <- TRUE
  x_train <- x_train[, keep_fixed, drop = FALSE]
  x_test <- x_test[, keep_fixed, drop = FALSE]
  if (method == "rrblup") {
    fit <- fit_rrblup_marker(y[train_idx], scaled$matrix[train_idx, , drop = FALSE], x_train)
    pred[test_idx] <- predict_rrblup_marker(fit, scaled$matrix[test_idx, , drop = FALSE], x_test)
    marker_effect_accumulator[, fold] <- as.vector(fit$u)
  } else {
    k_all <- tcrossprod(scaled$matrix) / max(1, ncol(scaled$matrix))
    pred[test_idx] <- fit_predict_gblup(
      y[train_idx],
      k_all[train_idx, train_idx, drop = FALSE],
      k_all[test_idx, train_idx, drop = FALSE],
      x_train,
      x_test
    )
  }
  fold_rows[[length(fold_rows) + 1]] <- data.frame(
    fold = fold,
    n_train = length(train_idx),
    n_test = length(test_idx),
    heldout_group = if (!is.null(group_col) && group_col %in% names(data)) {
      paste(unique(data[[group_col]][test_idx]), collapse = ",")
    } else {
      ""
    }
  )
}

out <- outdir
tables <- file.path(out, "tables")
model_dir <- file.path(out, "model")
repro_dir <- file.path(out, "reproducibility")
dir.create(tables, recursive = TRUE, showWarnings = FALSE)
dir.create(model_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(repro_dir, recursive = TRUE, showWarnings = FALSE)

prediction_df <- data.frame(
  sample_id = data$sample_id,
  observed_trait = y,
  predicted_trait = pred,
  GEBV = pred,
  residual = y - pred,
  fold = fold_id,
  model = paste0("rrBLUP_package_", method)
)
write_tsv(file.path(tables, "gebv_predictions.tsv"), prediction_df)
ranked_df <- prediction_df[order(prediction_df$GEBV, decreasing = TRUE), ]
ranked_df$rank <- seq_len(nrow(ranked_df))
write_tsv(file.path(tables, "ranked_breeding_materials.tsv"), ranked_df)
write_tsv(file.path(tables, "cross_validation_results.tsv"), do.call(rbind, fold_rows))

overall <- calc_metrics(y, pred, paste0("rrBLUP_package_", method), "overall")
fold_metrics <- do.call(rbind, lapply(sort(unique(fold_id)), function(fold) {
  idx <- which(fold_id == fold)
  calc_metrics(y[idx], pred[idx], paste0("rrBLUP_package_", method), fold)
}))
perf <- rbind(overall, fold_metrics)
perf$n_markers <- length(marker_cols)
perf$n_covariates <- length(covariate_cols)
perf$cv <- if (!is.null(group_col)) paste0("grouped:", group_col) else "kfold"
write_tsv(file.path(tables, "model_performance.tsv"), perf)

scaled_final <- standardize_train_test(marker_matrix, seq_along(y))
x_final <- fixed_matrix(data, covariate_cols)
if (method == "rrblup") {
  final_fit <- fit_rrblup_marker(y, scaled_final$matrix, x_final)
  marker_effects <- data.frame(marker_id = marker_cols, effect = as.vector(final_fit$u))
  write_tsv(file.path(tables, "marker_effects.tsv"), marker_effects)
} else {
  k_final <- tcrossprod(scaled_final$matrix) / max(1, ncol(scaled_final$matrix))
  final_fit <- rrBLUP::mixed.solve(y = y, K = k_final, X = x_final)
  write_tsv(
    file.path(tables, "genomic_relationship_diag.tsv"),
    data.frame(sample_id = data$sample_id, diag_G = diag(k_final))
  )
}

model_artifact <- list(
  method = method,
  trait = trait,
  marker_cols = marker_cols,
  covariate_cols = covariate_cols,
  marker_center = scaled_final$center,
  marker_scale = scaled_final$scale,
  fit = final_fit,
  package = "rrBLUP"
)
saveRDS(model_artifact, file.path(model_dir, "final_model.rds"))

capture.output(sessionInfo(), file = file.path(repro_dir, "sessionInfo.txt"))
writeLines(paste("Rscript", paste(commandArgs(trailingOnly = TRUE), collapse = " ")), file.path(repro_dir, "commands.sh"))

report <- c(
  "# Crop Genomic Prediction",
  "",
  paste("Method: CRAN rrBLUP", method),
  paste("Trait:", trait),
  paste("Samples:", length(y)),
  paste("Markers:", length(marker_cols)),
  paste("Validation:", if (!is.null(group_col)) paste("grouped by", group_col) else paste(length(unique(fold_id)), "fold CV")),
  "",
  "Use this output as a formal rrBLUP/GBLUP result only after checking trait design, QC, covariates, and validation assumptions."
)
writeLines(report, file.path(out, "report.md"))

cat(paste0("wrote rrBLUP package predictions -> ", file.path(tables, "gebv_predictions.tsv"), "\n"))
