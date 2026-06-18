#!/usr/bin/env Rscript

parse_args <- function() {
  raw <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", raw, value = TRUE)
  script <- if (length(file_arg)) sub("^--file=", "", file_arg[[1]]) else "run_enrichment.R"
  args <- commandArgs(trailingOnly = TRUE)
  out <- list(.script = script, .command = paste(c("Rscript", script, args), collapse = " "))
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) stop("Unexpected argument: ", key)
    name <- sub("^--", "", key)
    if (i == length(args) || startsWith(args[[i + 1]], "--")) {
      out[[name]] <- TRUE
      i <- i + 1
    } else {
      out[[name]] <- args[[i + 1]]
      i <- i + 2
    }
  }
  out
}

get_arg <- function(args, name, default = NULL, required = FALSE) {
  value <- args[[name]]
  if (is.null(value) || identical(value, TRUE) || identical(value, "")) {
    if (required) stop("Missing required --", name)
    return(default)
  }
  value
}

detect_sep <- function(path) {
  lines <- readLines(path, n = 5, warn = FALSE)
  lines <- lines[nzchar(lines)]
  if (!length(lines)) stop("Empty file: ", path)
  sample <- paste(lines, collapse = "\n")
  if (grepl("\t", sample, fixed = TRUE)) return("\t")
  if (grepl(",", sample, fixed = TRUE)) return(",")
  ""
}

read_any_table <- function(path) {
  sep <- detect_sep(path)
  tab <- tryCatch(
    read.table(path, header = TRUE, sep = sep, quote = "", comment.char = "",
               stringsAsFactors = FALSE, check.names = FALSE, fill = TRUE),
    error = function(e) NULL
  )
  if (is.null(tab) || nrow(tab) == 0 || ncol(tab) == 0) {
    tab <- read.table(path, header = FALSE, sep = sep, quote = "", comment.char = "",
                      stringsAsFactors = FALSE, check.names = FALSE, fill = TRUE)
    names(tab) <- paste0("V", seq_len(ncol(tab)))
  }
  tab
}

read_lines_as_ids <- function(path) {
  lines <- readLines(path, warn = FALSE)
  lines <- lines[nzchar(trimws(lines))]
  lines <- lines[!startsWith(trimws(lines), "#")]
  ids <- vapply(strsplit(trimws(lines), "\\s+"), `[`, character(1), 1)
  if (length(ids) && tolower(ids[[1]]) %in% c("gene", "gene_id", "id", "genes", "geneid")) {
    ids <- ids[-1]
  }
  ids
}

pick_col <- function(tab, requested, fallback = 1) {
  if (!is.null(requested) && requested %in% names(tab)) return(requested)
  lower <- setNames(names(tab), tolower(names(tab)))
  if (!is.null(requested) && tolower(requested) %in% names(lower)) return(lower[[tolower(requested)]])
  names(tab)[[fallback]]
}

read_ids <- function(path, column) {
  tab <- read_any_table(path)
  if (!(column %in% names(tab)) && ncol(tab) == 1) {
    ids <- trimws(as.character(read_lines_as_ids(path)))
    ids <- ids[!tolower(ids) %in% c("", "na", "nan", "none", "null", "-", ".")]
    return(unique(ids))
  }
  col <- pick_col(tab, column, 1)
  ids <- trimws(as.character(tab[[col]]))
  ids <- ids[!tolower(ids) %in% c("", "na", "nan", "none", "null", "-", ".")]
  unique(ids)
}

read_ranked <- function(path, gene_col, rank_col) {
  tab <- read_any_table(path)
  gcol <- pick_col(tab, gene_col, 1)
  rcol <- pick_col(tab, rank_col, min(2, ncol(tab)))
  genes <- trimws(as.character(tab[[gcol]]))
  score <- suppressWarnings(as.numeric(tab[[rcol]]))
  keep <- !is.na(score) & nzchar(genes)
  tab <- data.frame(gene_id = genes[keep], score = score[keep], stringsAsFactors = FALSE)
  tab <- tab[order(abs(tab$score), decreasing = TRUE), ]
  tab <- tab[!duplicated(tab$gene_id), ]
  ranks <- tab$score
  names(ranks) <- tab$gene_id
  sort(ranks, decreasing = TRUE)
}

read_term2gene <- function(path) {
  tab <- read_any_table(path)
  if (ncol(tab) < 2) stop("TERM2GENE requires at least two columns")
  term_col <- pick_col(tab, "term_id", 1)
  gene_col <- pick_col(tab, "gene_id", 2)
  out <- data.frame(
    term_id = trimws(as.character(tab[[term_col]])),
    gene_id = trimws(as.character(tab[[gene_col]])),
    stringsAsFactors = FALSE
  )
  out <- out[nzchar(out$term_id) & nzchar(out$gene_id), ]
  unique(out)
}

read_term2name <- function(path) {
  if (is.null(path) || !file.exists(path)) return(NULL)
  tab <- read_any_table(path)
  if (ncol(tab) < 2) return(NULL)
  term_col <- pick_col(tab, "term_id", 1)
  name_col <- pick_col(tab, "term_name", 2)
  out <- data.frame(term_id = as.character(tab[[term_col]]), term_name = as.character(tab[[name_col]]), stringsAsFactors = FALSE)
  out <- out[!duplicated(out$term_id), ]
  out
}

write_blocked <- function(outdir, reasons, command) {
  dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
  text <- c("# Enrichment Run Blocked", "", "## Reasons", paste0("- ", reasons), "", "## Command", "", paste0("`", command, "`"))
  writeLines(text, file.path(outdir, "blocked_report.md"))
}

run_ora <- function(foreground, background, term2gene, min_size, max_size) {
  term2gene <- term2gene[term2gene$gene_id %in% background, ]
  fg <- intersect(foreground, background)
  bg_n <- length(unique(background))
  fg_n <- length(unique(fg))
  by_term <- split(term2gene$gene_id, term2gene$term_id)
  rows <- lapply(names(by_term), function(term) {
    genes <- unique(by_term[[term]])
    term_size <- length(genes)
    if (term_size < min_size || term_size > max_size) return(NULL)
    overlap <- intersect(fg, genes)
    k <- length(overlap)
    p <- phyper(k - 1, term_size, bg_n - term_size, fg_n, lower.tail = FALSE)
    data.frame(
      term_id = term,
      term_size = term_size,
      foreground_count = fg_n,
      background_count = bg_n,
      overlap_count = k,
      gene_ratio = ifelse(fg_n > 0, k / fg_n, NA_real_),
      bg_ratio = ifelse(bg_n > 0, term_size / bg_n, NA_real_),
      pvalue = p,
      genes = paste(sort(overlap), collapse = ";"),
      stringsAsFactors = FALSE
    )
  })
  res <- do.call(rbind, rows[!vapply(rows, is.null, logical(1))])
  if (is.null(res) || nrow(res) == 0) return(data.frame())
  res$padj <- p.adjust(res$pvalue, method = "BH")
  res <- res[order(res$padj, res$pvalue, decreasing = FALSE), ]
  rownames(res) <- NULL
  res
}

run_gsea <- function(ranks, term2gene, min_size, max_size) {
  if (!requireNamespace("fgsea", quietly = TRUE)) {
    stop("fgsea is required for --method gsea but is not installed")
  }
  term2gene <- term2gene[term2gene$gene_id %in% names(ranks), ]
  pathways <- split(term2gene$gene_id, term2gene$term_id)
  pathways <- lapply(pathways, unique)
  pathways <- pathways[lengths(pathways) >= min_size & lengths(pathways) <= max_size]
  if (!length(pathways)) return(data.frame())
  fg <- fgsea::fgsea(pathways = pathways, stats = ranks, minSize = min_size, maxSize = max_size)
  res <- as.data.frame(fg, stringsAsFactors = FALSE)
  if ("leadingEdge" %in% names(res)) {
    res$leadingEdge <- vapply(res$leadingEdge, function(x) paste(x, collapse = ";"), character(1))
  }
  names(res)[names(res) == "pathway"] <- "term_id"
  names(res)[names(res) == "pval"] <- "pvalue"
  names(res)[names(res) == "padj"] <- "padj"
  res <- res[order(res$padj, res$pvalue, decreasing = FALSE), ]
  rownames(res) <- NULL
  res
}

plot_results <- function(res, outdir, fdr, top_n) {
  xdg_cache <- file.path(tempdir(), "xdg-cache")
  dir.create(file.path(xdg_cache, "fontconfig"), showWarnings = FALSE, recursive = TRUE)
  Sys.setenv(XDG_CACHE_HOME = xdg_cache)
  pdf(file.path(outdir, "enrichment_plots.pdf"), width = 8, height = 6)
  on.exit(dev.off(), add = TRUE)
  plot_body(res, fdr, top_n)
  png(file.path(outdir, "enrichment_plots.png"), width = 1600, height = 1200, res = 180)
  plot_body(res, fdr, top_n)
  dev.off()
}

plot_body <- function(res, fdr, top_n) {
  if (!nrow(res)) {
    plot.new()
    text(0.5, 0.5, "No tested terms after filtering")
    return(invisible(NULL))
  }
  sig <- res[!is.na(res$padj) & res$padj <= fdr, , drop = FALSE]
  dat <- if (nrow(sig)) sig else res
  dat <- head(dat, top_n)
  labels <- if ("term_name" %in% names(dat)) dat$term_name else dat$term_id
  score <- -log10(pmax(dat$padj, .Machine$double.xmin))
  ord <- order(score, decreasing = FALSE)
  par(mar = c(5, 12, 3, 1))
  barplot(score[ord], names.arg = labels[ord], horiz = TRUE, las = 1,
          xlab = "-log10(FDR)", main = "Top enrichment terms", cex.names = 0.7)
}

write_report <- function(outdir, args, method, res, filtered, fg_count, bg_count, mapped_count, command, limitations) {
  species <- get_arg(args, "species", "not_provided")
  database <- get_arg(args, "database-name", "not_provided")
  id_type <- get_arg(args, "gene-id-type", "not_provided")
  fdr <- get_arg(args, "fdr", "0.05")
  lines <- c(
    "# Enrichment Report",
    "",
    paste0("- Species: ", species),
    paste0("- Gene ID type: ", id_type),
    paste0("- Database: ", database),
    paste0("- Method: ", method),
    paste0("- Multiple testing correction: BH"),
    paste0("- FDR threshold: ", fdr),
    paste0("- Foreground genes: ", fg_count),
    paste0("- Background genes: ", bg_count),
    paste0("- Mapped foreground genes: ", mapped_count),
    paste0("- Tested terms: ", nrow(res)),
    paste0("- Significant terms: ", nrow(filtered)),
    "",
    "## Top Terms"
  )
  if (nrow(filtered)) {
    top <- head(filtered, 10)
    label <- if ("term_name" %in% names(top)) top$term_name else top$term_id
    lines <- c(lines, paste0("- ", label, " (FDR=", signif(top$padj, 3), ")"))
  } else {
    lines <- c(lines, "- No terms passed the FDR threshold.")
  }
  lines <- c(lines, "", "## Limitations")
  if (length(limitations)) {
    lines <- c(lines, paste0("- ", limitations))
  } else {
    lines <- c(lines, "- No additional limitations were detected by the wrapper.")
  }
  lines <- c(lines, "", "## Command", "", paste0("`", command, "`"))
  writeLines(lines, file.path(outdir, "report.md"))
}

main <- function() {
  args <- parse_args()
  outdir <- get_arg(args, "outdir", required = TRUE)
  dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
  method <- tolower(get_arg(args, "method", "auto"))
  term2gene_path <- get_arg(args, "term2gene", required = TRUE)
  min_size <- as.integer(get_arg(args, "min-size", "3"))
  max_size <- as.integer(get_arg(args, "max-size", "500"))
  fdr <- as.numeric(get_arg(args, "fdr", "0.05"))
  top_n <- as.integer(get_arg(args, "top-n", "30"))
  limitations <- character()

  term2gene <- read_term2gene(term2gene_path)
  term2name <- read_term2name(get_arg(args, "term2name", NULL))

  if (method == "auto") {
    method <- if (!is.null(args[["ranked"]])) "gsea" else "ora"
  }

  if (method == "ora") {
    fg <- read_ids(get_arg(args, "foreground", required = TRUE), get_arg(args, "foreground-column", "gene_id"))
    if (!is.null(args[["background"]])) {
      bg <- read_ids(get_arg(args, "background", required = TRUE), get_arg(args, "background-column", "gene_id"))
    } else {
      bg <- unique(term2gene$gene_id)
      limitations <- c(limitations, "No background file was provided; the annotation universe from TERM2GENE was used.")
    }
    res <- run_ora(fg, bg, term2gene, min_size, max_size)
    mapped_count <- length(intersect(fg, unique(term2gene$gene_id)))
    fg_count <- length(unique(fg))
    bg_count <- length(unique(bg))
  } else if (method == "gsea") {
    ranks <- read_ranked(
      get_arg(args, "ranked", required = TRUE),
      get_arg(args, "ranked-gene-column", "gene_id"),
      get_arg(args, "rank-column", "stat")
    )
    res <- tryCatch(
      run_gsea(ranks, term2gene, min_size, max_size),
      error = function(e) {
        write_blocked(outdir, conditionMessage(e), args$.command)
        stop(e)
      }
    )
    fg_count <- length(ranks)
    bg_count <- length(ranks)
    mapped_count <- length(intersect(names(ranks), unique(term2gene$gene_id)))
  } else {
    stop("--method must be auto, ora, or gsea")
  }

  if (nrow(res) && !is.null(term2name)) {
    res <- merge(res, term2name, by = "term_id", all.x = TRUE, sort = FALSE)
    res <- res[order(res$padj, res$pvalue, decreasing = FALSE), ]
  }
  filtered <- if (nrow(res)) res[!is.na(res$padj) & res$padj <= fdr, , drop = FALSE] else res
  write.table(res, file.path(outdir, "enrichment_results.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
  write.table(filtered, file.path(outdir, "enrichment_results.filtered.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
  plot_results(res, outdir, fdr, top_n)
  write_report(outdir, args, method, res, filtered, fg_count, bg_count, mapped_count, args$.command, limitations)
  writeLines(c("#!/usr/bin/env bash", "set -euo pipefail", args$.command), file.path(outdir, "reproduce.sh"))
  cat("wrote enrichment results -> ", file.path(outdir, "enrichment_results.tsv"), "\n", sep = "")
}

main()
