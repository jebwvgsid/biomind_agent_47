#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)

if ("--help" %in% args || "-h" %in% args) {
  cat("Usage: Rscript plot_gwas.R --input gwas.tsv --out-prefix out/gwas --threshold 1e-6 --title trait\n")
  cat("Input table must contain columns: SNP CHR BP P\n")
  quit(status = 0)
}

get_arg <- function(flag, default = NULL) {
  hit <- which(args == flag)
  if (length(hit) == 0 || hit == length(args)) return(default)
  args[[hit + 1]]
}

input <- get_arg("--input")
out_prefix <- get_arg("--out-prefix", "gwas")
threshold <- as.numeric(get_arg("--threshold", "1e-6"))
title <- get_arg("--title", "GWAS")

if (is.null(input)) stop("--input is required", call. = FALSE)
if (is.na(threshold) || threshold <= 0 || threshold > 1) {
  stop("--threshold must be in (0, 1]", call. = FALSE)
}

dir.create(dirname(out_prefix), recursive = TRUE, showWarnings = FALSE)

dat <- read.table(input, header = TRUE, sep = "", stringsAsFactors = FALSE, comment.char = "")
required <- c("SNP", "CHR", "BP", "P")
missing_cols <- setdiff(required, names(dat))
if (length(missing_cols) > 0) {
  stop(paste("missing required columns:", paste(missing_cols, collapse = ", ")), call. = FALSE)
}

dat$BP <- suppressWarnings(as.numeric(dat$BP))
dat$P <- suppressWarnings(as.numeric(dat$P))
dat <- dat[is.finite(dat$BP) & is.finite(dat$P) & dat$P > 0 & dat$P <= 1, ]
if (nrow(dat) == 0) stop("no valid GWAS rows after filtering", call. = FALSE)

chr_levels <- unique(dat$CHR[order(dat$CHR, dat$BP)])
dat$CHR <- factor(dat$CHR, levels = chr_levels)
dat <- dat[order(dat$CHR, dat$BP), ]
dat$logp <- -log10(dat$P)

chr_info <- aggregate(BP ~ CHR, dat, max)
chr_info$offset <- c(0, cumsum(chr_info$BP)[-nrow(chr_info)])
names(chr_info)[names(chr_info) == "BP"] <- "chr_len"
dat <- merge(dat, chr_info, by = "CHR", all.x = TRUE, sort = FALSE)
dat$pos_cum <- dat$BP + dat$offset
axis_df <- aggregate(pos_cum ~ CHR, dat, function(x) (min(x) + max(x)) / 2)

lambda <- NA_real_
chisq <- qchisq(1 - dat$P, df = 1)
if (length(chisq) > 0 && all(is.finite(chisq))) {
  lambda <- median(chisq, na.rm = TRUE) / qchisq(0.5, df = 1)
}

plot_manhattan <- function(file, device = c("png", "pdf")) {
  device <- match.arg(device)
  if (device == "png") png(file, width = 2400, height = 1200, res = 220) else pdf(file, width = 11, height = 5.5)
  on.exit(dev.off(), add = TRUE)
  cols <- rep(c("#3B6EA8", "#D55E00"), length.out = length(chr_levels))
  point_cols <- cols[as.integer(dat$CHR)]
  par(mar = c(4.5, 4.5, 2.5, 1), las = 1)
  plot(dat$pos_cum, dat$logp, pch = 20, cex = 0.35, col = point_cols,
       xaxt = "n", xlab = "Chromosome", ylab = expression(-log[10](P)),
       main = title, bty = "l")
  axis(1, at = axis_df$pos_cum, labels = axis_df$CHR, las = 2, cex.axis = 0.65)
  abline(h = -log10(threshold), col = "#B22222", lty = 2, lwd = 1.2)
}

plot_qq <- function(file, device = c("png", "pdf")) {
  device <- match.arg(device)
  if (device == "png") png(file, width = 1200, height = 1200, res = 220) else pdf(file, width = 5.5, height = 5.5)
  on.exit(dev.off(), add = TRUE)
  observed <- -log10(sort(dat$P))
  expected <- -log10(ppoints(length(observed)))
  lim <- range(c(expected, observed), finite = TRUE)
  par(mar = c(4.5, 4.5, 2.5, 1), las = 1)
  plot(expected, observed, pch = 20, cex = 0.45, col = "#3B6EA8",
       xlab = expression(Expected~~-log[10](P)),
       ylab = expression(Observed~~-log[10](P)),
       main = paste(title, "QQ plot"), bty = "l", xlim = lim, ylim = lim)
  abline(0, 1, col = "#B22222", lty = 2, lwd = 1.2)
}

plot_manhattan(paste0(out_prefix, ".manhattan.png"), "png")
plot_manhattan(paste0(out_prefix, ".manhattan.pdf"), "pdf")
plot_qq(paste0(out_prefix, ".qq.png"), "png")
plot_qq(paste0(out_prefix, ".qq.pdf"), "pdf")

sig <- dat[dat$P < threshold, c("SNP", "CHR", "BP", "P")]
write.table(sig, paste0(out_prefix, ".significant_sites.tsv"), sep = "\t",
            quote = FALSE, row.names = FALSE)

summary <- data.frame(
  n_markers = nrow(dat),
  n_chromosomes = length(chr_levels),
  threshold = threshold,
  minus_log10_threshold = -log10(threshold),
  n_significant = nrow(sig),
  lambda_gc = lambda
)
write.table(summary, paste0(out_prefix, ".plot_summary.tsv"), sep = "\t",
            quote = FALSE, row.names = FALSE)
