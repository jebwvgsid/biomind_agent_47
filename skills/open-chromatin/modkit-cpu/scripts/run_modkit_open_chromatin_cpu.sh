#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' \
'Run modkit open-chromatin prediction on CPU with mandatory modBAM preflight checks.' \
'' \
'Required environment variables:' \
'  MODKIT=/path/to/modkit' \
'  MODEL_DIR=/path/to/model_directory' \
'  BAM=/path/to/sample.sorted.bam' \
'  OUTDIR=/path/to/output_directory' \
'  SAMPLE=sample_name' \
'' \
'Choose exactly one execution scope:' \
'  REGION=contig:start-end              Small-region CPU test or one contig if supported' \
'  INCLUDE_BED=/path/to/regions.bed     Selected regions' \
'  CHROMOSOMES="chr1 chr2"              Run contig-by-contig' \
'  ALLOW_WHOLE_GENOME=1                 Whole-genome CPU run; use only for small genomes' \
'' \
'Optional environment variables:' \
'  STEP_SIZE=25                         Distance between prediction centers' \
'  THRESHOLD=0.8                        Probability cutoff for thresholded bedGraph/BED' \
'  MIN_COVERAGE=                        Pass --min-coverage only when set' \
'  FORCE=0                              Set to 1 to pass --force when supported' \
'  RUN_LABEL=                           Output label for INCLUDE_BED or whole-genome mode' \
'' \
'Examples:' \
'  MODKIT=/opt/modkit MODEL_DIR=/models/open_chromatin BAM=sample.sorted.bam \' \
'  OUTDIR=modkit_open SAMPLE=sample REGION=chr1:100000-200000 \' \
'  bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh' \
'' \
'  MODKIT=/opt/modkit MODEL_DIR=/models/open_chromatin BAM=sample.sorted.bam \' \
'  OUTDIR=modkit_open SAMPLE=sample INCLUDE_BED=peaks_to_test.bed \' \
'  bash skills/open-chromatin/modkit-cpu/scripts/run_modkit_open_chromatin_cpu.sh'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

sanitize_label() {
    local label="$1"
    label="${label//[^A-Za-z0-9._-]/_}"
    label="${label##_}"
    label="${label%%_}"
    printf '%s' "${label:-run}"
}

has_help_option() {
    local option="$1"
    grep -q -- "${option}" "${PREDICT_HELP}"
}

join_by_tab() {
    local first=1
    local item
    for item in "$@"; do
        if [[ "${first}" -eq 1 ]]; then
            first=0
        else
            printf '\t'
        fi
        printf '%s' "${item}"
    done
    printf '\n'
}

check_required_variables() {
    : "${MODKIT:?Need MODKIT=/path/to/modkit}"
    : "${MODEL_DIR:?Need MODEL_DIR=/path/to/model_directory}"
    : "${BAM:?Need BAM=/path/to/sample.sorted.bam}"
    : "${OUTDIR:?Need OUTDIR=/path/to/output_directory}"
    : "${SAMPLE:?Need SAMPLE=sample_name}"
}

check_files() {
    test -x "${MODKIT}" || fail "MODKIT is not executable: ${MODKIT}"
    test -d "${MODEL_DIR}" || fail "MODEL_DIR not found: ${MODEL_DIR}"
    test -s "${BAM}" || fail "BAM not found or empty: ${BAM}"
}

check_bam_integrity_and_index() {
    command -v samtools >/dev/null 2>&1 || fail "samtools not found on PATH"
    samtools quickcheck -v "${BAM}"

    if [[ ! -s "${BAM}.bai" && ! -s "${BAM}.csi" ]]; then
        fail "BAM index not found. Please run: samtools index ${BAM}"
    fi
}

check_modified_base_tags() {
    mkdir -p "${OUTDIR}/logs"

    "${MODKIT}" modbam check-tags "${BAM}" --head 1000 \
        > "${OUTDIR}/logs/${SAMPLE}.modbam_check_tags.txt"

    if [[ ! -s "${OUTDIR}/logs/${SAMPLE}.modbam_check_tags.txt" ]]; then
        fail "empty modBAM tag report: ${OUTDIR}/logs/${SAMPLE}.modbam_check_tags.txt"
    fi
}

check_6ma_tag() {
    if ! grep -q "A+a" "${OUTDIR}/logs/${SAMPLE}.modbam_check_tags.txt"; then
        fail "No A+a 6mA tag found in modBAM. This BAM is not suitable for modkit open-chromatin prediction. Return to Dorado basecalling / modified-base calling with a 6mA model."
    fi
}

inspect_predict_help() {
    PREDICT_HELP="${OUTDIR}/logs/${SAMPLE}.open_chromatin_predict_help.txt"
    export PREDICT_HELP

    "${MODKIT}" open-chromatin predict --help > "${PREDICT_HELP}" 2>&1 || {
        while IFS= read -r line; do
            printf '%s\n' "${line}" >&2
        done < "${PREDICT_HELP}"
        fail "failed to inspect modkit open-chromatin predict --help"
    }

    grep -E -- "--device|--region|--include-bed|--threshold|--step-size|--min-coverage" "${PREDICT_HELP}" || true
}

build_common_args() {
    DEVICE_ARGS=()
    MIN_COVERAGE_ARGS=()
    FORCE_ARGS=()
    LOG_FLAG=()

    if has_help_option "--device"; then
        DEVICE_ARGS=(--device cpu)
    fi

    if [[ -n "${MIN_COVERAGE:-}" ]]; then
        has_help_option "--min-coverage" || fail "MIN_COVERAGE was set but installed modkit help does not show --min-coverage"
        MIN_COVERAGE_ARGS=(--min-coverage "${MIN_COVERAGE}")
    fi

    if [[ "${FORCE:-0}" == "1" ]]; then
        has_help_option "--force" || fail "FORCE=1 was set but installed modkit help does not show --force"
        FORCE_ARGS=(--force)
    fi

    if has_help_option "--log-filepath"; then
        LOG_FLAG=(--log-filepath)
    elif has_help_option "--log"; then
        LOG_FLAG=(--log)
    else
        LOG_FLAG=()
    fi
}

choose_execution_scope() {
    local n_scope=0
    [[ -n "${REGION:-}" ]] && n_scope=$((n_scope + 1))
    [[ -n "${INCLUDE_BED:-}" ]] && n_scope=$((n_scope + 1))
    [[ -n "${CHROMOSOMES:-}" ]] && n_scope=$((n_scope + 1))
    [[ "${ALLOW_WHOLE_GENOME:-0}" == "1" ]] && n_scope=$((n_scope + 1))

    if [[ "${n_scope}" -eq 0 ]]; then
        fail "No execution scope set. Start CPU prediction with REGION or INCLUDE_BED. Use ALLOW_WHOLE_GENOME=1 only for small genomes or explicit whole-genome requests."
    fi

    if [[ "${n_scope}" -gt 1 ]]; then
        fail "Set exactly one scope: REGION, INCLUDE_BED, CHROMOSOMES, or ALLOW_WHOLE_GENOME=1"
    fi

    if [[ -n "${REGION:-}" ]]; then
        has_help_option "--region" || fail "REGION was set but installed modkit help does not show --region. Use INCLUDE_BED instead."
        SCOPE_TYPES=("region")
        SCOPE_VALUES=("${REGION}")
        SCOPE_LABELS=("")
    elif [[ -n "${INCLUDE_BED:-}" ]]; then
        has_help_option "--include-bed" || fail "INCLUDE_BED was set but installed modkit help does not show --include-bed"
        test -s "${INCLUDE_BED}" || fail "INCLUDE_BED not found or empty: ${INCLUDE_BED}"
        local include_label="${RUN_LABEL:-$(basename "${INCLUDE_BED}")}"
        SCOPE_TYPES=("include-bed")
        SCOPE_VALUES=("${INCLUDE_BED}")
        SCOPE_LABELS=("${include_label}")
    elif [[ -n "${CHROMOSOMES:-}" ]]; then
        has_help_option "--region" || fail "CHROMOSOMES was set but installed modkit help does not show --region"
        local chroms="${CHROMOSOMES//,/ }"
        SCOPE_TYPES=()
        SCOPE_VALUES=()
        SCOPE_LABELS=()
        for chrom in ${chroms}; do
            if [[ -n "${chrom}" ]]; then
                SCOPE_TYPES+=("region")
                SCOPE_VALUES+=("${chrom}")
                SCOPE_LABELS+=("")
            fi
        done
        [[ "${#SCOPE_TYPES[@]}" -gt 0 ]] || fail "CHROMOSOMES did not contain any contig names"
    else
        echo "WARNING: Whole-genome CPU prediction requested. This can be very slow on large genomes." >&2
        SCOPE_TYPES=("whole-genome")
        SCOPE_VALUES=("${RUN_LABEL:-whole_genome}")
        SCOPE_LABELS=("${RUN_LABEL:-whole_genome}")
    fi
}

predict_one_scope() {
    local scope_type="$1"
    local scope_value="$2"
    local requested_label="${3:-}"
    local label=""
    local scope_args=()

    case "${scope_type}" in
        region)
            label="$(sanitize_label "${requested_label:-${scope_value}}")"
            scope_args=(--region "${scope_value}")
            ;;
        include-bed)
            label="$(sanitize_label "${requested_label:-$(basename "${scope_value}")}")"
            scope_args=(--include-bed "${scope_value}")
            ;;
        whole-genome)
            label="$(sanitize_label "${requested_label:-${scope_value}}")"
            scope_args=()
            ;;
        *)
            fail "internal error: unknown scope ${scope_type}"
            ;;
    esac

    local raw="${OUTDIR}/bedgraph/${SAMPLE}.${label}.open_chromatin.raw.bedgraph"
    local log="${OUTDIR}/logs/${SAMPLE}.${label}.open_chromatin.cpu.log"
    local thresholded="${OUTDIR}/bedgraph/${SAMPLE}.${label}.open_chromatin.threshold${THRESHOLD}.bedgraph"
    local merged="${OUTDIR}/bed/${SAMPLE}.${label}.open_chromatin.threshold${THRESHOLD}.merged.bed"
    local cmd=(
        "${MODKIT}" open-chromatin predict "${BAM}"
        --model "${MODEL_DIR}"
        --step-size "${STEP_SIZE}"
        "${DEVICE_ARGS[@]}"
        "${MIN_COVERAGE_ARGS[@]}"
        "${FORCE_ARGS[@]}"
        "${scope_args[@]}"
    )

    if [[ "${#LOG_FLAG[@]}" -gt 0 ]]; then
        cmd+=("${LOG_FLAG[@]}" "${log}")
    fi

    cmd+=(-o "${raw}")

    echo "Running CPU open-chromatin prediction for ${scope_type}: ${scope_value}"
    printf 'Command:'
    printf ' %q' "${cmd[@]}"
    printf '\n'

    "${cmd[@]}"

    test -s "${raw}" || fail "empty output bedGraph: ${raw}"

    awk 'BEGIN{OFS="\t"} /^[#]/ {next} NF >= 4 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && ($4 + 0) >= thr {print $1,$2,$3,$4}' \
        thr="${THRESHOLD}" "${raw}" > "${thresholded}"

    if [[ -s "${thresholded}" ]]; then
        cut -f1-3 "${thresholded}" \
            | sort -k1,1 -k2,2n \
            | bedtools merge -i - \
            > "${merged}"
    else
        : > "${merged}"
    fi

    join_by_tab "${scope_type}" "${scope_value}" "${raw}" "${thresholded}" "${merged}" >> "${OUTDIR}/logs/${SAMPLE}.open_chromatin_outputs.tsv"

    echo "Done:"
    echo "  Raw bedGraph: ${raw}"
    echo "  Thresholded bedGraph: ${thresholded}"
    echo "  Merged BED: ${merged}"
}

STEP_SIZE="${STEP_SIZE:-25}"
THRESHOLD="${THRESHOLD:-0.8}"

check_required_variables
check_files
check_bam_integrity_and_index
check_modified_base_tags
check_6ma_tag
inspect_predict_help
build_common_args
choose_execution_scope

command -v bedtools >/dev/null 2>&1 || fail "bedtools not found on PATH"
mkdir -p "${OUTDIR}/bedgraph" "${OUTDIR}/bed"

join_by_tab "scope_type" "scope_value" "raw_bedgraph" "thresholded_bedgraph" "merged_bed" > "${OUTDIR}/logs/${SAMPLE}.open_chromatin_outputs.tsv"

for scope_i in "${!SCOPE_TYPES[@]}"; do
    predict_one_scope "${SCOPE_TYPES[scope_i]}" "${SCOPE_VALUES[scope_i]}" "${SCOPE_LABELS[scope_i]}"
done

echo "All requested CPU open-chromatin predictions completed."
echo "Output manifest: ${OUTDIR}/logs/${SAMPLE}.open_chromatin_outputs.tsv"
