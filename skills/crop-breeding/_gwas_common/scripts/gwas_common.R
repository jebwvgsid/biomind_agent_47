args_get <- function(args, flag, default = NULL) {
  i <- match(flag, args)
  if (is.na(i) || i == length(args)) default else args[[i + 1]]
}

args_has <- function(args, flag) {
  flag %in% args
}

split_csv <- function(value, default = character()) {
  if (is.null(value) || !nzchar(value)) return(default)
  trimws(unlist(strsplit(value, ",")))
}

detect_column <- function(cols, explicit = NULL, aliases, label, required = TRUE) {
  if (!is.null(explicit) && nzchar(explicit)) {
    if (explicit %in% cols) return(explicit)
    stop(sprintf("%s column '%s' is not present. Available: %s", label, explicit, paste(cols, collapse = ", ")))
  }
  lower <- tolower(cols)
  for (alias in aliases) {
    idx <- match(tolower(alias), lower)
    if (!is.na(idx)) return(cols[[idx]])
  }
  if (required) stop(sprintf("Could not detect %s column. Available: %s", label, paste(cols, collapse = ", ")))
  NULL
}

read_any_table <- function(path) {
  if (!file.exists(path)) stop(sprintf("File not found: %s", path))
  read.table(path, header = TRUE, sep = "", quote = "\"", comment.char = "", check.names = FALSE, stringsAsFactors = FALSE)
}

write_tsv <- function(df, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  write.table(df, path, sep = "\t", quote = FALSE, row.names = FALSE, na = "")
}

sanitize_name <- function(value) {
  value <- gsub("[^A-Za-z0-9_.-]+", "_", value)
  value <- gsub("^[._]+|[._]+$", "", value)
  ifelse(nzchar(value), value, "trait")
}

estimate_lambda_gc <- function(p) {
  p <- p[is.finite(p) & p > 0 & p <= 1]
  if (!length(p)) return(list(value = NA_real_, method = "no_valid_p_values"))
  list(value = median(qchisq(p, df = 1, lower.tail = FALSE), na.rm = TRUE) / 0.454936423119572, method = "chi2_df1")
}

standardize_summary <- function(gwas, outdir, trait, method, p_col = NULL, chr_col = NULL, pos_col = NULL, id_col = NULL, effect_col = NULL, source = "") {
  d <- read_any_table(gwas)
  dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
  cols <- names(d)
  p_name <- detect_column(cols, p_col, c("p", "pval", "pvalue", "p_value", "p.value", "p_wald", "p_lrt", "p_score"), "p-value")
  chr_name <- detect_column(cols, chr_col, c("chr", "chrom", "chromosome", "chromosome_id", "seqid"), "chromosome")
  pos_name <- detect_column(cols, pos_col, c("pos", "bp", "position", "base_pair", "start"), "position")
  id_name <- detect_column(cols, id_col, c("marker_id", "id", "snp", "snp_id", "marker", "variant", "variant_id"), "marker ID", required = FALSE)
  effect_name <- detect_column(cols, effect_col, c("beta", "effect", "estimate", "effect_size", "b"), "effect", required = FALSE)
  p <- suppressWarnings(as.numeric(d[[p_name]]))
  pos <- suppressWarnings(as.integer(d[[pos_name]]))
  marker <- if (!is.null(id_name)) as.character(d[[id_name]]) else paste0(as.character(d[[chr_name]]), ":", pos)
  marker[!nzchar(marker) | is.na(marker)] <- paste0(as.character(d[[chr_name]])[!nzchar(marker) | is.na(marker)], ":", pos[!nzchar(marker) | is.na(marker)])
  effect <- if (!is.null(effect_name)) d[[effect_name]] else ""
  out <- data.frame(
    marker_id = marker,
    chr = as.character(d[[chr_name]]),
    pos = pos,
    p_value = p,
    trait = trait,
    method = method,
    effect = effect,
    source = source,
    stringsAsFactors = FALSE
  )
  valid <- is.finite(out$p_value) & out$p_value > 0 & out$p_value <= 1
  n <- sum(valid)
  suggestive <- 1 / max(n, 1)
  bonferroni <- 0.05 / max(n, 1)
  lead <- out[valid & out$p_value <= suggestive, , drop = FALSE]
  lead$threshold <- ifelse(lead$p_value <= bonferroni, "bonferroni_0.05", "suggestive")
  lead <- lead[order(lead$p_value), , drop = FALSE]
  write_tsv(out, file.path(outdir, "summary_stats.tsv"))
  write_tsv(lead, file.path(outdir, "lead_snps.tsv"))
  lambda <- estimate_lambda_gc(out$p_value)
  write_tsv(data.frame(metric = "lambda_gc", value = lambda$value, method = lambda$method), file.path(outdir, "lambda_gc.tsv"))
  dir.create(file.path(outdir, "figures"), recursive = TRUE, showWarnings = FALSE)
  if (requireNamespace("CMplot", quietly = TRUE) && n > 0) {
    plot_df <- data.frame(SNP = out$marker_id, Chromosome = out$chr, Position = out$pos, P = out$p_value)
    old <- getwd()
    setwd(file.path(outdir, "figures"))
    try(CMplot::CMplot(plot_df, plot.type = "m", LOG10 = TRUE, threshold = c(suggestive, bonferroni), file = "png", memo = sanitize_name(trait), verbose = FALSE), silent = TRUE)
    try(CMplot::CMplot(plot_df, plot.type = "q", LOG10 = TRUE, file = "png", memo = sanitize_name(trait), verbose = FALSE), silent = TRUE)
    setwd(old)
  }
  report <- c(
    paste0("# Crop GWAS ", trait),
    "",
    paste0("- method: ", method),
    paste0("- source: ", ifelse(nzchar(source), source, gwas)),
    paste0("- variants with valid p-values: ", n),
    paste0("- suggestive threshold: ", signif(suggestive, 6)),
    paste0("- Bonferroni 0.05 threshold: ", signif(bonferroni, 6)),
    paste0("- lead SNPs: ", nrow(lead)),
    paste0("- lambda GC: ", signif(lambda$value, 6), " (", lambda$method, ")")
  )
  writeLines(report, file.path(outdir, "report.md"))
  cat(sprintf("{\"n_variants\":%d,\"lead_snps\":%d}\n", n, nrow(lead)), file = file.path(outdir, "result.json"))
  invisible(list(n_variants = n, lead_snps = nrow(lead)))
}

append_command <- function(path, command) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  cat(command, "\n", file = path, append = TRUE, sep = "")
}
