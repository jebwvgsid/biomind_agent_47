#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(Matrix)
  library(SummarizedExperiment)
  library(splatter)
  library(AlphaSimR)
})

script_dir <- function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) == 0) {
    return(getwd())
  }
  dirname(normalizePath(sub("^--file=", "", file_arg[[1]])))
}

get_arg <- function(name, default = NULL) {
  args <- commandArgs(trailingOnly = TRUE)
  prefix <- paste0(name, "=")
  inline <- args[startsWith(args, prefix)]
  if (length(inline) > 0) {
    return(sub(prefix, "", inline[[1]], fixed = TRUE))
  }
  idx <- match(name, args)
  if (!is.na(idx) && idx < length(args)) {
    return(args[[idx + 1]])
  }
  default
}

write_tsv <- function(x, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  write.table(x, file = path, sep = "\t", quote = FALSE, row.names = FALSE, na = "")
}

write_plain <- function(x, path, col_names = FALSE) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  write.table(x, file = path, sep = "\t", quote = FALSE, row.names = FALSE, col.names = col_names)
}

safe_num <- function(x, digits = 6) {
  ifelse(is.na(x), NA, round(as.numeric(x), digits))
}

benchmark_dir <- normalizePath(file.path(script_dir(), "..", ".."), mustWork = FALSE)
default_outdir <- file.path(benchmark_dir, "fixtures_professional")
outdir <- normalizePath(get_arg("--outdir", default_outdir), mustWork = FALSE)
seed <- as.integer(get_arg("--seed", "20260526"))

simulate_splatter <- function(root, seed) {
  set.seed(seed)
  out <- file.path(root, "splatter")
  dir.create(out, recursive = TRUE, showWarnings = FALSE)

  params <- splatter::newSplatParams()
  params <- splatter::setParam(params, "nGenes", 500)
  params <- splatter::setParam(params, "batchCells", 180)
  params <- splatter::setParam(params, "group.prob", c(0.34, 0.33, 0.33))
  params <- splatter::setParam(params, "de.prob", 0.12)
  params <- splatter::setParam(params, "de.facLoc", 0.2)
  params <- splatter::setParam(params, "de.facScale", 0.4)

  sce <- splatter::splatSimulate(params, method = "groups", verbose = FALSE)
  counts <- SummarizedExperiment::assay(sce, "counts")
  counts <- Matrix::Matrix(as.matrix(counts), sparse = TRUE)
  gene_ids <- rownames(counts)
  cell_ids <- colnames(counts)
  if (is.null(gene_ids)) {
    gene_ids <- sprintf("SPLAT_GENE_%04d", seq_len(nrow(counts)))
    rownames(counts) <- gene_ids
  }
  if (is.null(cell_ids)) {
    cell_ids <- sprintf("SPLAT_CELL_%04d", seq_len(ncol(counts)))
    colnames(counts) <- cell_ids
  }

  Matrix::writeMM(counts, file.path(out, "matrix.mtx"))
  write_plain(data.frame(gene_ids, gene_ids, "Gene Expression"), file.path(out, "features.tsv"))
  write_plain(data.frame(cell_ids), file.path(out, "barcodes.tsv"))

  coldata <- as.data.frame(SummarizedExperiment::colData(sce))
  group <- if ("Group" %in% names(coldata)) as.character(coldata$Group) else rep("Group1", ncol(counts))
  batch <- if ("Batch" %in% names(coldata)) as.character(coldata$Batch) else rep("Batch1", ncol(counts))
  lib_size <- Matrix::colSums(counts)
  detected_genes <- Matrix::colSums(counts > 0)
  cell_meta <- data.frame(
    cell_id = cell_ids,
    group = group,
    batch = batch,
    library_size = as.integer(lib_size),
    detected_genes = as.integer(detected_genes)
  )
  write_tsv(cell_meta, file.path(out, "cell_metadata.tsv"))

  dense <- as.matrix(counts)
  marker_rows <- list()
  groups <- unique(group)
  for (g in groups) {
    in_group <- group == g
    rest <- group != g
    log2fc <- log2((rowMeans(dense[, in_group, drop = FALSE]) + 1) /
      (rowMeans(dense[, rest, drop = FALSE]) + 1))
    keep <- head(order(abs(log2fc), decreasing = TRUE), 20)
    marker_rows[[g]] <- data.frame(
      gene_id = gene_ids[keep],
      group = g,
      marker_rank = seq_along(keep),
      log2fc = safe_num(log2fc[keep], 5),
      truth_source = "splatter_simulated_group_signal"
    )
  }
  write_tsv(do.call(rbind, marker_rows), file.path(out, "truth_markers.tsv"))

  rowdata <- as.data.frame(SummarizedExperiment::rowData(sce))
  de_cols <- grep("^DEFac", names(rowdata), value = TRUE)
  if (length(de_cols) > 0) {
    de_summary <- cbind(data.frame(gene_id = gene_ids), rowdata[, de_cols, drop = FALSE])
  } else {
    de_summary <- data.frame(gene_id = gene_ids, de_factor_available = FALSE)
  }
  write_tsv(de_summary, file.path(out, "group_de_summary.tsv"))

  truth <- data.frame(
    metric = c(
      "simulator",
      "splatter_version",
      "seed",
      "n_cells",
      "n_genes",
      "n_groups",
      "matrix_format"
    ),
    value = c(
      "splatter",
      as.character(utils::packageVersion("splatter")),
      seed,
      ncol(counts),
      nrow(counts),
      length(groups),
      "MatrixMarket 10x-like counts"
    )
  )
  write_tsv(truth, file.path(out, "simulation_truth.tsv"))
}

fit_marker <- function(x, y) {
  if (length(unique(x)) < 2) {
    return(c(beta = 0, pvalue = NA))
  }
  fit <- summary(stats::lm(y ~ x))
  coef_row <- fit$coefficients[2, ]
  c(beta = coef_row[["Estimate"]], pvalue = coef_row[["Pr(>|t|)"]])
}

simulate_alphasimr <- function(root, seed) {
  set.seed(seed + 1L)
  out <- file.path(root, "alphasimr")
  dir.create(out, recursive = TRUE, showWarnings = FALSE)

  n_ind <- 60L
  n_chr <- 3L
  snp_per_chr <- 40L
  founder <- AlphaSimR::quickHaplo(nInd = n_ind, nChr = n_chr, segSites = 160)
  sim_param <- AlphaSimR::SimParam$new(founder)
  sim_param$addTraitA(nQtlPerChr = 12, mean = 100, var = 25)
  sim_param$setVarE(h2 = 0.45)
  sim_param$addSnpChip(nSnpPerChr = snp_per_chr)
  pop <- AlphaSimR::newPop(founder, simParam = sim_param)
  pop <- AlphaSimR::setPheno(pop, simParam = sim_param)

  geno <- tryCatch(
    AlphaSimR::pullSnpGeno(pop, snpChip = 1, simParam = sim_param),
    error = function(e) AlphaSimR::pullSegSiteGeno(pop, simParam = sim_param)
  )
  geno <- as.matrix(geno)
  if (nrow(geno) != n_ind && ncol(geno) == n_ind) {
    geno <- t(geno)
  }
  marker_ids <- colnames(geno)
  if (is.null(marker_ids) || any(marker_ids == "")) {
    marker_ids <- sprintf("ASR_SNP_%04d", seq_len(ncol(geno)))
    colnames(geno) <- marker_ids
  }
  sample_ids <- sprintf("ASR_CROP_%03d", seq_len(nrow(geno)))
  rownames(geno) <- sample_ids

  gv_vec <- as.numeric(AlphaSimR::gv(pop)[, 1])
  pheno_vec <- as.numeric(AlphaSimR::pheno(pop)[, 1])
  if (all(is.na(pheno_vec))) {
    pheno_vec <- gv_vec + stats::rnorm(length(gv_vec), sd = stats::sd(gv_vec))
  }

  geno_df <- data.frame(sample_id = sample_ids, geno, check.names = FALSE)
  write_tsv(geno_df, file.path(out, "genotype_matrix.tsv"))

  pheno_df <- data.frame(
    sample_id = sample_ids,
    trait_yield = safe_num(pheno_vec, 5),
    true_genetic_value = safe_num(gv_vec, 5),
    simulator = "AlphaSimR"
  )
  write_tsv(pheno_df, file.path(out, "phenotype.tsv"))

  env_ids <- c("E1_drought", "E2_control", "E3_high_input")
  env_effect <- c(-4, 0, 4)
  stress <- c(0.82, 0.35, 0.12)
  gxe_slope <- as.numeric(scale(gv_vec))
  multi_env <- do.call(rbind, lapply(seq_along(env_ids), function(i) {
    data.frame(
      sample_id = sample_ids,
      environment_id = env_ids[[i]],
      trait_yield = safe_num(pheno_vec + env_effect[[i]] + gxe_slope * c(-1.8, 0, 1.2)[[i]] +
        stats::rnorm(length(pheno_vec), sd = 1.4), 5),
      stress_index = stress[[i]],
      true_genetic_value = safe_num(gv_vec, 5),
      true_environment_effect = env_effect[[i]]
    )
  }))
  write_tsv(multi_env, file.path(out, "phenotype_multi_environment.tsv"))

  stats <- t(apply(geno, 2, fit_marker, y = pheno_vec))
  effect_truth <- t(apply(geno, 2, fit_marker, y = gv_vec))
  signal_count <- min(12L, ncol(geno))
  signal_idx <- head(order(abs(effect_truth[, "beta"]), decreasing = TRUE), signal_count)
  marker_chr <- paste0("Chr", ((seq_len(ncol(geno)) - 1L) %/% snp_per_chr) + 1L)
  marker_pos <- (((seq_len(ncol(geno)) - 1L) %% snp_per_chr) + 1L) * 100000L

  gwas <- data.frame(
    marker_id = marker_ids,
    chrom = marker_chr,
    pos = marker_pos,
    beta = safe_num(stats[, "beta"], 6),
    pvalue = format(stats[, "pvalue"], scientific = TRUE, digits = 6),
    is_model_signal_truth = seq_len(ncol(geno)) %in% signal_idx
  )
  write_tsv(gwas, file.path(out, "gwas_summary.tsv"))

  marker_anno <- data.frame(
    marker_id = marker_ids,
    chrom = marker_chr,
    pos = marker_pos,
    gene_id = sprintf("ASR_GENE_%04d", ceiling(seq_len(ncol(geno)) / 2)),
    model_signal_truth = seq_len(ncol(geno)) %in% signal_idx,
    true_genetic_value_beta = safe_num(effect_truth[, "beta"], 6)
  )
  write_tsv(marker_anno, file.path(out, "marker_annotation.tsv"))

  marker_effects <- data.frame(
    marker_id = marker_ids[signal_idx],
    effect_allele = "ALT",
    effect = safe_num(effect_truth[signal_idx, "beta"], 6),
    trait_direction = ifelse(effect_truth[signal_idx, "beta"] >= 0, "high", "low"),
    truth_source = "association_with_AlphaSimR_true_genetic_value"
  )
  write_tsv(marker_effects, file.path(out, "marker_effects.tsv"))

  truth <- data.frame(
    metric = c(
      "simulator",
      "AlphaSimR_version",
      "seed",
      "n_individuals",
      "n_chromosomes",
      "n_markers",
      "n_qtl_per_chromosome",
      "target_h2",
      "trait_model"
    ),
    value = c(
      "AlphaSimR",
      as.character(utils::packageVersion("AlphaSimR")),
      seed + 1L,
      nrow(geno),
      n_chr,
      ncol(geno),
      12L,
      0.45,
      "additive"
    )
  )
  write_tsv(truth, file.path(out, "simulation_truth.tsv"))
}

read_metric <- function(path, metric, default = "not_generated") {
  if (!file.exists(path)) {
    return(default)
  }
  x <- tryCatch(read.delim(path, stringsAsFactors = FALSE), error = function(e) NULL)
  if (is.null(x) || !all(c("metric", "value") %in% names(x))) {
    return(default)
  }
  hit <- x$value[x$metric == metric]
  if (length(hit) == 0) {
    default
  } else {
    as.character(hit[[1]])
  }
}

write_manifest <- function(root, seed) {
  manifest <- file.path(benchmark_dir, "manifests", "legacy_optional_r_fixtures_manifest.yaml")
  msprime_version <- read_metric(file.path(root, "msprime", "simulation_truth.tsv"), "msprime_version")
  lines <- c(
    "fixture_set_id: biomind-professional-fixtures",
    "version: 0.2.0",
    paste0("seed: ", seed),
    "simulators:",
    paste0("  msprime: ", msprime_version),
    paste0("  splatter: ", as.character(utils::packageVersion("splatter"))),
    paste0("  AlphaSimR: ", as.character(utils::packageVersion("AlphaSimR"))),
    "root: fixtures_professional",
    "notes:",
    "  - msprime active fixtures are generated by scripts/generate/generate_msprime_fixtures.py.",
    "  - splatter and AlphaSimR fixtures are legacy optional data and are not active by default.",
    "  - Truth files describe simulator parameters and model-derived signal labels.",
    "",
    "task_fixtures:",
    "  BM-SCRNA-INGEST-QC-001:",
    "    - fixtures_professional/splatter/matrix.mtx",
    "    - fixtures_professional/splatter/features.tsv",
    "    - fixtures_professional/splatter/barcodes.tsv",
    "    - fixtures_professional/splatter/cell_metadata.tsv",
    "    - fixtures_professional/splatter/simulation_truth.tsv",
    "  BM-SCRNA-FULL-002:",
    "    - fixtures_professional/splatter/matrix.mtx",
    "    - fixtures_professional/splatter/features.tsv",
    "    - fixtures_professional/splatter/barcodes.tsv",
    "    - fixtures_professional/splatter/cell_metadata.tsv",
    "    - fixtures_professional/splatter/truth_markers.tsv",
    "    - fixtures_professional/splatter/group_de_summary.tsv",
    "  BM-CROP-GP-GWAS-013:",
    "    - fixtures_professional/msprime/genotype_matrix.tsv",
    "    - fixtures_professional/msprime/phenotype.tsv",
    "    - fixtures_professional/msprime/gwas_summary.tsv",
    "    - fixtures_professional/msprime/marker_annotation.tsv",
    "    - fixtures_professional/alphasimr/genotype_matrix.tsv",
    "    - fixtures_professional/alphasimr/phenotype.tsv",
    "    - fixtures_professional/alphasimr/gwas_summary.tsv",
    "    - fixtures_professional/alphasimr/marker_annotation.tsv",
    "  BM-CROP-DEEP-GP-016:",
    "    - fixtures_professional/msprime/genotype_matrix.tsv",
    "    - fixtures_professional/msprime/phenotype.tsv",
    "    - fixtures_professional/msprime/gwas_summary.tsv",
    "    - fixtures_professional/msprime/marker_annotation.tsv",
    "    - fixtures_professional/alphasimr/genotype_matrix.tsv",
    "    - fixtures_professional/alphasimr/phenotype.tsv",
    "    - fixtures_professional/alphasimr/gwas_summary.tsv",
    "    - fixtures_professional/alphasimr/marker_annotation.tsv",
    "  BM-CROP-GXE-PANGENOME-017:",
    "    - fixtures_professional/alphasimr/genotype_matrix.tsv",
    "    - fixtures_professional/alphasimr/phenotype.tsv",
    "    - fixtures_professional/alphasimr/phenotype_multi_environment.tsv",
    "    - fixtures_professional/alphasimr/marker_effects.tsv",
    "  BM-CROP-POPGEN-MARKER-018:",
    "    - fixtures_professional/msprime/msprime_variants.vcf",
    "    - fixtures_professional/msprime/haplotypes.fasta",
    "    - fixtures_professional/msprime/sample_groups.tsv",
    "    - fixtures_professional/msprime/marker_effects.tsv"
  )
  writeLines(lines, manifest, useBytes = TRUE)
}

dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
simulate_splatter(outdir, seed)
simulate_alphasimr(outdir, seed)
write_manifest(outdir, seed)

cat("Generated WSL/R professional fixtures under ", outdir, "\n", sep = "")
cat("Manifest: ", file.path(benchmark_dir, "manifests", "legacy_optional_r_fixtures_manifest.yaml"), "\n", sep = "")
