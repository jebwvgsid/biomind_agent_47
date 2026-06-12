args <- commandArgs(trailingOnly = TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript run_gapit3_models.R --genotype geno.tsv --phenotype pheno.tsv --trait trait --models MLM,MLMM,FarmCPU,BLINK --outdir results [--covariates cov.tsv]\n")
  quit(status = 0)
}

script_file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
script_dir <- dirname(normalizePath(script_file, winslash = "/", mustWork = TRUE))
source(normalizePath(file.path(script_dir, "..", "..", "_gwas_common", "scripts", "gwas_common.R"), winslash = "/", mustWork = TRUE))

genotype <- args_get(args, "--genotype")
phenotype <- args_get(args, "--phenotype")
trait <- args_get(args, "--trait")
models <- split_csv(args_get(args, "--models", "MLM,MLMM,FarmCPU,BLINK"))
covariates <- args_get(args, "--covariates")
outdir <- args_get(args, "--outdir")
p_col <- args_get(args, "--p-col")
chr_col <- args_get(args, "--chr-col")
pos_col <- args_get(args, "--pos-col")
id_col <- args_get(args, "--id-col")
dry_run <- args_has(args, "--dry-run")

if (is.null(genotype) || is.null(phenotype) || is.null(trait) || is.null(outdir)) {
  stop("--genotype, --phenotype, --trait, and --outdir are required")
}

dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
commands_log <- file.path(outdir, "reproducibility", "commands.sh")
dir.create(dirname(commands_log), recursive = TRUE, showWarnings = FALSE)
cat("", file = commands_log)
append_command(commands_log, paste("Rscript", script_file, paste(args, collapse = " ")))

if (!dry_run) {
  if (!exists("GAPIT", mode = "function")) {
    suppressWarnings(suppressMessages(require(GAPIT3, quietly = TRUE)))
  }
  if (!exists("GAPIT", mode = "function")) {
    stop("GAPIT function is unavailable. Install GAPIT3 from https://github.com/jiabowang/GAPIT before running this wrapper.")
  }
}

phe <- read_any_table(phenotype)
sample_col <- detect_column(names(phe), NULL, c("Taxa", "taxa", "sample_id", "sample", "iid", "id"), "sample")
if (!(trait %in% names(phe))) stop(sprintf("Trait column '%s' is not present in phenotype table", trait))
Y <- phe[, c(sample_col, trait), drop = FALSE]
names(Y)[1] <- "Taxa"
CV <- NULL
if (!is.null(covariates) && nzchar(covariates)) {
  CV <- read_any_table(covariates)
  cv_sample <- detect_column(names(CV), NULL, c("Taxa", "taxa", "sample_id", "sample", "iid", "id"), "sample")
  names(CV)[names(CV) == cv_sample] <- "Taxa"
}
G <- read_any_table(genotype)

workdir <- file.path(outdir, "gapit_work")
dir.create(workdir, recursive = TRUE, showWarnings = FALSE)
old <- getwd()
setwd(workdir)
on.exit(setwd(old), add = TRUE)

result_files <- character()
for (model in models) {
  model <- trimws(model)
  if (!nzchar(model)) next
  model_dir <- file.path(workdir, model)
  dir.create(model_dir, recursive = TRUE, showWarnings = FALSE)
  setwd(model_dir)
  if (!dry_run) {
    GAPIT(Y = Y, G = G, CV = CV, model = model, PCA.total = 0, file.output = TRUE)
  }
  hits <- list.files(model_dir, pattern = "GWAS.*Results|GWAS.Results|GAPIT.*GWAS", recursive = TRUE, full.names = TRUE, ignore.case = TRUE)
  result_files <- c(result_files, hits)
}
setwd(old)

if (length(result_files)) {
  standardized <- file.path(outdir, models[[1]])
  dir.create(standardized, recursive = TRUE, showWarnings = FALSE)
  standardize_summary(result_files[[1]], outdir, trait, paste(models, collapse = ","), p_col, chr_col, pos_col, id_col, source = result_files[[1]])
} else {
  writeLines(c(
    paste0("# Crop GWAS GAPIT3 ", trait),
    "",
    paste0("- models: ", paste(models, collapse = ",")),
    paste0("- dry_run: ", dry_run),
    "- association result: not found yet"
  ), file.path(outdir, "report.md"))
  cat(sprintf("{\"dry_run\":%s,\"result_files\":0}\n", tolower(as.character(dry_run))), file = file.path(outdir, "result.json"))
}

cat(sprintf("GAPIT3 models complete or recorded -> %s\n", outdir))
