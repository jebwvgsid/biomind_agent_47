args <- commandArgs(trailingOnly = TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript run_pav_sv_gwas.R --marker-matrix pav.tsv --phenotype pheno.tsv --trait trait --model MLM|FarmCPU --outdir results [--covariates cov.tsv]\n")
  quit(status = 0)
}

script_file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
script_dir <- dirname(normalizePath(script_file, winslash = "/", mustWork = TRUE))
source(normalizePath(file.path(script_dir, "..", "..", "_gwas_common", "scripts", "gwas_common.R"), winslash = "/", mustWork = TRUE))

marker_matrix <- args_get(args, "--marker-matrix")
phenotype <- args_get(args, "--phenotype")
trait <- args_get(args, "--trait")
model <- args_get(args, "--model", "MLM")
covariates <- args_get(args, "--covariates")
outdir <- args_get(args, "--outdir")
p_col <- args_get(args, "--p-col")
chr_col <- args_get(args, "--chr-col")
pos_col <- args_get(args, "--pos-col")
id_col <- args_get(args, "--id-col")
dry_run <- args_has(args, "--dry-run")

if (is.null(marker_matrix) || is.null(phenotype) || is.null(trait) || is.null(outdir)) {
  stop("--marker-matrix, --phenotype, --trait, and --outdir are required")
}
if (!(model %in% c("MLM", "FarmCPU"))) {
  stop("--model must be MLM or FarmCPU for this wrapper")
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
    stop("GAPIT function is unavailable. Install GAPIT3 from https://github.com/jiabowang/GAPIT before running PAV/SV GWAS.")
  }
}

phe <- read_any_table(phenotype)
sample_col <- detect_column(names(phe), NULL, c("Taxa", "taxa", "sample_id", "sample", "iid", "id"), "sample")
if (!(trait %in% names(phe))) stop(sprintf("Trait column '%s' is not present in phenotype table", trait))
Y <- phe[, c(sample_col, trait), drop = FALSE]
names(Y)[1] <- "Taxa"

G <- read_any_table(marker_matrix)
if (ncol(G) < 2) stop("Marker matrix must contain a sample ID column plus marker columns")
names(G)[1] <- "Taxa"

CV <- NULL
if (!is.null(covariates) && nzchar(covariates)) {
  CV <- read_any_table(covariates)
  cv_sample <- detect_column(names(CV), NULL, c("Taxa", "taxa", "sample_id", "sample", "iid", "id"), "sample")
  names(CV)[names(CV) == cv_sample] <- "Taxa"
}

workdir <- file.path(outdir, "pav_sv_work")
dir.create(workdir, recursive = TRUE, showWarnings = FALSE)
old <- getwd()
setwd(workdir)
on.exit(setwd(old), add = TRUE)

if (!dry_run) {
  GAPIT(Y = Y, G = G, CV = CV, model = model, PCA.total = 0, file.output = TRUE)
}

setwd(old)
hits <- list.files(workdir, pattern = "GWAS.*Results|GWAS.Results|GAPIT.*GWAS", recursive = TRUE, full.names = TRUE, ignore.case = TRUE)
if (length(hits)) {
  standardize_summary(hits[[1]], outdir, trait, paste0("PAV-SV-", model), p_col, chr_col, pos_col, id_col, source = hits[[1]])
} else {
  writeLines(c(
    paste0("# Crop PAV/SV GWAS ", trait),
    "",
    paste0("- model: ", model),
    paste0("- dry_run: ", dry_run),
    "- association result: not found yet"
  ), file.path(outdir, "report.md"))
  cat(sprintf("{\"dry_run\":%s,\"result_files\":0}\n", tolower(as.character(dry_run))), file = file.path(outdir, "result.json"))
}

cat(sprintf("PAV/SV GWAS complete or recorded -> %s\n", outdir))
