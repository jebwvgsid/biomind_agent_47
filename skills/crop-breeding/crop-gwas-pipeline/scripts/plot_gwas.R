args <- commandArgs(trailingOnly = TRUE)
if ("--help" %in% args || length(args) == 0) {
  cat("Usage: Rscript plot_gwas.R --gwas summary.tsv --outdir figures --trait trait [--p-col P --chr-col chr --pos-col pos]\n")
  quit(status = 0)
}

script_file <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
script_dir <- dirname(normalizePath(script_file, winslash = "/", mustWork = TRUE))
source(normalizePath(file.path(script_dir, "..", "..", "_gwas_common", "scripts", "gwas_common.R"), winslash = "/", mustWork = TRUE))

gwas <- args_get(args, "--gwas")
outdir <- args_get(args, "--outdir", "figures")
trait <- args_get(args, "--trait", "trait")
p_col <- args_get(args, "--p-col")
chr_col <- args_get(args, "--chr-col")
pos_col <- args_get(args, "--pos-col")
id_col <- args_get(args, "--id-col")
if (is.null(gwas)) stop("--gwas is required")
standardize_summary(gwas, outdir, trait, "GWAS", p_col, chr_col, pos_col, id_col, source = gwas)
