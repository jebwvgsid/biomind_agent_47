args <- commandArgs(trailingOnly = TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript run_rmvp_gwas.R --vcf variants.vcf.gz|--genotype geno.tsv --phenotype pheno.tsv --trait trait --models GLM,MLM,FarmCPU --threads 4 --outdir results\n")
  quit(status = 0)
}

script_file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
script_dir <- dirname(normalizePath(script_file, winslash = "/", mustWork = TRUE))
source(normalizePath(file.path(script_dir, "..", "..", "_gwas_common", "scripts", "gwas_common.R"), winslash = "/", mustWork = TRUE))

vcf <- args_get(args, "--vcf")
genotype <- args_get(args, "--genotype")
phenotype <- args_get(args, "--phenotype")
trait <- args_get(args, "--trait")
models <- split_csv(args_get(args, "--models", "GLM,MLM,FarmCPU"))
threads <- as.integer(args_get(args, "--threads", "1"))
outdir <- args_get(args, "--outdir")
p_col <- args_get(args, "--p-col")
chr_col <- args_get(args, "--chr-col")
pos_col <- args_get(args, "--pos-col")
id_col <- args_get(args, "--id-col")
dry_run <- args_has(args, "--dry-run")

if ((is.null(vcf) && is.null(genotype)) || (!is.null(vcf) && !is.null(genotype))) {
  stop("Provide exactly one of --vcf or --genotype")
}
if (is.null(phenotype) || is.null(trait) || is.null(outdir)) {
  stop("--phenotype, --trait, and --outdir are required")
}

dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
commands_log <- file.path(outdir, "reproducibility", "commands.sh")
dir.create(dirname(commands_log), recursive = TRUE, showWarnings = FALSE)
cat("", file = commands_log)
append_command(commands_log, paste("Rscript", script_file, paste(args, collapse = " ")))

if (!dry_run && !requireNamespace("rMVP", quietly = TRUE)) {
  stop("rMVP is unavailable. Install the rMVP package before running this wrapper.")
}

phe <- read_any_table(phenotype)
sample_col <- detect_column(names(phe), NULL, c("Taxa", "taxa", "sample_id", "sample", "iid", "id"), "sample")
if (!(trait %in% names(phe))) stop(sprintf("Trait column '%s' is not present in phenotype table", trait))
phe_mvp <- phe[, c(sample_col, trait), drop = FALSE]
names(phe_mvp)[1] <- "Taxa"

workdir <- file.path(outdir, "rmvp_work")
dir.create(workdir, recursive = TRUE, showWarnings = FALSE)
prefix <- file.path(workdir, "rmvp_input")
old <- getwd()
setwd(workdir)
on.exit(setwd(old), add = TRUE)

if (!dry_run) {
  if (!is.null(vcf)) {
    rMVP::MVP.Data(fileVCF = vcf, out = prefix)
    if (!requireNamespace("bigmemory", quietly = TRUE)) {
      stop("bigmemory is required to attach rMVP genotype matrices")
    }
    geno <- bigmemory::attach.big.matrix(paste0(prefix, ".geno.desc"))
    map <- read.table(paste0(prefix, ".geno.map"), header = TRUE)
  } else {
    geno <- as.matrix(read_any_table(genotype)[, -1, drop = FALSE])
    map <- NULL
  }
  rMVP::MVP(
    phe = phe_mvp,
    geno = geno,
    map = map,
    method = models,
    ncpus = threads,
    file.output = TRUE
  )
}

setwd(old)
hits <- list.files(workdir, pattern = "assoc|GWAS|MVP", recursive = TRUE, full.names = TRUE, ignore.case = TRUE)
hits <- hits[file.info(hits)$isdir == FALSE]
if (length(hits)) {
  standardize_summary(hits[[1]], outdir, trait, paste0("rMVP-", paste(models, collapse = ",")), p_col, chr_col, pos_col, id_col, source = hits[[1]])
} else {
  writeLines(c(
    paste0("# Crop GWAS rMVP ", trait),
    "",
    paste0("- models: ", paste(models, collapse = ",")),
    paste0("- dry_run: ", dry_run),
    "- association result: not found yet"
  ), file.path(outdir, "report.md"))
  cat(sprintf("{\"dry_run\":%s,\"result_files\":0}\n", tolower(as.character(dry_run))), file = file.path(outdir, "result.json"))
}

cat(sprintf("rMVP GWAS complete or recorded -> %s\n", outdir))
