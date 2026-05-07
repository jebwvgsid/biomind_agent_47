args <- commandArgs(trailingOnly = TRUE)

get_arg <- function(flag, default = NULL) {
  if (!(flag %in% args)) {
    return(default)
  }
  idx <- match(flag, args)
  if (is.na(idx) || idx == length(args)) {
    return(default)
  }
  args[[idx + 1]]
}

has_flag <- function(flag) {
  flag %in% args
}

input_path <- get_arg("--input")
output_path <- get_arg("--output")
object_name <- get_arg("--object")
list_only <- has_flag("--list-only")

if (is.null(input_path) || !nzchar(input_path)) {
  stop("Usage: Rscript convert_rdata_to_rds.R --input /abs/path/file.rdata [--object object_name --output /abs/path/file.rds] [--list-only]")
}

if (!file.exists(input_path)) {
  stop(sprintf("Input file not found: %s", input_path))
}

env <- new.env(parent = emptyenv())
loaded_names <- load(input_path, envir = env)

message("Loaded objects:")
for (nm in loaded_names) {
  obj <- get(nm, envir = env)
  message(sprintf("- %s : %s", nm, paste(class(obj), collapse = ", ")))
}

if (list_only) {
  quit(save = "no", status = 0)
}

if (is.null(output_path) || !nzchar(output_path)) {
  stop("Please provide --output when not using --list-only.")
}

seurat_candidates <- loaded_names[
  vapply(loaded_names, function(nm) inherits(get(nm, envir = env), "Seurat"), logical(1))
]

target_name <- object_name
if (is.null(target_name) || !nzchar(target_name)) {
  if (length(seurat_candidates) == 1) {
    target_name <- seurat_candidates[[1]]
  } else if (length(seurat_candidates) == 0) {
    stop("No Seurat object found in the .rdata file. Use --list-only to inspect objects first.")
  } else {
    stop(sprintf(
      "Multiple Seurat objects found: %s. Please re-run with --object <name>.",
      paste(seurat_candidates, collapse = ", ")
    ))
  }
}

if (!(target_name %in% loaded_names)) {
  stop(sprintf("Object '%s' not found in %s", target_name, input_path))
}

target_obj <- get(target_name, envir = env)
if (!inherits(target_obj, "Seurat")) {
  stop(sprintf("Object '%s' is not a Seurat object. Found class: %s", target_name, paste(class(target_obj), collapse = ", ")))
}

saveRDS(target_obj, file = output_path)
message(sprintf("Saved Seurat object '%s' to %s", target_name, output_path))
