args <- commandArgs(trailingOnly = TRUE)

usage <- function() {
  cat(paste0(
    "Usage: Rscript run_bglr_bayesian.R ",
    "--genotype genotype.tsv --phenotype phenotype.tsv --trait trait ",
    "--model BRR|BayesA|BayesB|BayesC|BL|RKHS --outdir out ",
    "[--folds 5] [--group-column family] ",
    "[--covariates covariates.tsv --covariate-cols PC1,PC2] ",
    "[--niter 12000 --burnin 2000 --thin 5 --seed 20260527]\n\n",
    "This production wrapper calls CRAN BGLR for Bayesian genomic selection.\n"
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
model <- getarg("--model", "BRR")
outdir <- getarg("--outdir", "genomic_prediction_bglr")
folds <- as.integer(getarg("--folds", "5"))
group_col <- getarg("--group-column")
cov_file <- getarg("--covariates")
cov_cols_arg <- getarg("--covariate-cols", "")
niter <- as.integer(getarg("--niter", "12000"))
burnin <- as.integer(getarg("--burnin", "2000"))
thin <- as.integer(getarg("--thin", "5"))
seed <- as.integer(getarg("--seed", "20260527"))

if (is.null(geno_file) || is.null(pheno_file) || is.null(trait) || is.null(outdir)) {
  usage()
  stop("Missing required --genotype, --phenotype, --trait, or --outdir")
}

valid_models <- c("BRR", "BayesA", "BayesB", "BayesC", "BL", "RKHS")
if (!model %in% valid_models) {
  stop(paste("--model must be one of", paste(valid_models, collapse = ",")))
}

if (!requireNamespace("BGLR", quietly = TRUE)) {
  stop("CRAN package BGLR is required. Install with install.packages('BGLR').")
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
  set.seed(seed)
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
  if (length(covariate_cols) == 0) return(NULL)
  missing_cols <- setdiff(covariate_cols, names(data))
  if (length(missing_cols) > 0) {
    stop(paste("Covariate columns missing:", paste(missing_cols, collapse = ",")))
  }
  model.matrix(as.formula(paste("~", paste(covariate_cols, collapse = "+"))), data = data)
}

build_eta <- function(marker_matrix, fixed_x, model_name) {
  eta <- list()
  if (!is.null(fixed_x)) {
    eta$fixed <- list(X = fixed_x, model = "FIXED")
  }
  if (model_name == "RKHS") {
    eta$markers <- list(K = tcrossprod(marker_matrix) / max(1, ncol(marker_matrix)), model = "RKHS")
  } else {
    eta$markers <- list(X = marker_matrix, model = model_name)
  }
  eta
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
fixed_x <- fixed_matrix(data, covariate_cols)
fold_id <- make_folds(data, folds, group_col)

pred <- rep(NA_real_, length(y))
fold_rows <- list()
set.seed(seed)
for (fold in sort(unique(fold_id))) {
  test_idx <- which(fold_id == fold)
  train_idx <- which(fold_id != fold)
  scaled <- standardize_train_test(marker_matrix, train_idx)
  y_cv <- y
  y_cv[test_idx] <- NA
  eta <- build_eta(scaled$matrix, fixed_x, model)
  fit <- BGLR::BGLR(y = y_cv, ETA = eta, nIter = niter, burnIn = burnin, thin = thin, verbose = FALSE)
  pred[test_idx] <- fit$yHat[test_idx]
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
  model = paste0("BGLR_", model)
)
write_tsv(file.path(tables, "prediction.tsv"), prediction_df)
ranked_df <- prediction_df[order(prediction_df$GEBV, decreasing = TRUE), ]
ranked_df$rank <- seq_len(nrow(ranked_df))
write_tsv(file.path(tables, "ranked_breeding_materials.tsv"), ranked_df)
write_tsv(file.path(tables, "cross_validation_results.tsv"), do.call(rbind, fold_rows))

overall <- calc_metrics(y, pred, paste0("BGLR_", model), "overall")
fold_metrics <- do.call(rbind, lapply(sort(unique(fold_id)), function(fold) {
  idx <- which(fold_id == fold)
  calc_metrics(y[idx], pred[idx], paste0("BGLR_", model), fold)
}))
perf <- rbind(overall, fold_metrics)
perf$n_markers <- length(marker_cols)
perf$n_covariates <- length(covariate_cols)
perf$cv <- if (!is.null(group_col)) paste0("grouped:", group_col) else "kfold"
perf$niter <- niter
perf$burnin <- burnin
perf$thin <- thin
write_tsv(file.path(tables, "model_performance.tsv"), perf)

scaled_final <- standardize_train_test(marker_matrix, seq_along(y))
eta_final <- build_eta(scaled_final$matrix, fixed_x, model)
final_fit <- BGLR::BGLR(y = y, ETA = eta_final, nIter = niter, burnIn = burnin, thin = thin, verbose = FALSE)
if (!is.null(final_fit$ETA$markers$b)) {
  write_tsv(
    file.path(tables, "posterior_marker_effects.tsv"),
    data.frame(marker_id = marker_cols, posterior_mean_effect = as.vector(final_fit$ETA$markers$b))
  )
}

saveRDS(
  list(
    package = "BGLR",
    model = model,
    trait = trait,
    marker_cols = marker_cols,
    covariate_cols = covariate_cols,
    marker_center = scaled_final$center,
    marker_scale = scaled_final$scale,
    fit = final_fit
  ),
  file.path(model_dir, "final_model.rds")
)

capture.output(sessionInfo(), file = file.path(repro_dir, "sessionInfo.txt"))
writeLines(paste("Rscript", paste(commandArgs(trailingOnly = TRUE), collapse = " ")), file.path(repro_dir, "commands.sh"))
writeLines(
  c(
    "# Crop GS BGLR Bayesian",
    "",
    paste("Method: CRAN BGLR", model),
    paste("Trait:", trait),
    paste("Samples:", length(y)),
    paste("Markers:", length(marker_cols)),
    paste("MCMC:", "nIter", niter, "burnIn", burnin, "thin", thin),
    "",
    "Use posterior effects as model-based prioritization evidence, not causal validation."
  ),
  file.path(out, "report.md")
)

cat(paste0("wrote BGLR predictions -> ", file.path(tables, "prediction.tsv"), "\n"))
