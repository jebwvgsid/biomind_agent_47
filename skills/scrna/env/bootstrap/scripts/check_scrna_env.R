args <- commandArgs(trailingOnly = TRUE)

packages_arg <- NULL
if (length(args) >= 2) {
  for (i in seq(1, length(args) - 1, by = 1)) {
    if (args[[i]] == "--packages") {
      packages_arg <- args[[i + 1]]
      break
    }
  }
}

required_packages <- c("Seurat", "Matrix", "yaml", "ggplot2", "patchwork", "dplyr")
if (!is.null(packages_arg) && nzchar(packages_arg)) {
  required_packages <- unique(trimws(unlist(strsplit(packages_arg, ","))))
}

message("scRNA environment preflight")
message(sprintf("R version: %s", R.version.string))

failed <- character()
loaded_versions <- list()

for (pkg in required_packages) {
  ok <- suppressPackageStartupMessages(
    require(pkg, character.only = TRUE, quietly = TRUE)
  )
  if (!ok) {
    failed <- c(failed, pkg)
    next
  }
  loaded_versions[[pkg]] <- as.character(packageVersion(pkg))
}

if (length(loaded_versions) > 0) {
  for (pkg in names(loaded_versions)) {
    message(sprintf("OK %s %s", pkg, loaded_versions[[pkg]]))
  }
}

if (length(failed) > 0) {
  stop(sprintf("Missing R packages: %s", paste(failed, collapse = ", ")))
}

message("scRNA environment ready")
