"""Pure ANN marginal-table operations; no model, workflow or execution authority.

Units: VCF data records (not ALT alleles), ANN entries, and whole SO field
values (an '&' compound is not split). This is a new-Plan operation contract;
it does not reinterpret an already-approved different schema.
"""
from collections import Counter
import argparse
import json
from pathlib import Path

HEADERS = ('metric\tvalue', 'annotation_impact\tcount', 'sequence_ontology_effect\tcount')
COUNTS = ('variants_total', 'variants_with_ann', 'ann_records_total')
FLAGS = ('header_has_snpeff_version', 'header_has_ann_info')


def summarize(lines):
    metrics = dict.fromkeys(COUNTS, 0)
    metrics.update(dict.fromkeys(FLAGS, False))
    impact, effect = Counter(), Counter()
    for line in lines:
        if line.startswith('#'):
            metrics['header_has_snpeff_version'] |= line.startswith('##SnpEffVersion=')
            metrics['header_has_ann_info'] |= line.startswith('##INFO=<ID=ANN,')
            continue
        if not line.strip():
            continue
        fields = line.rstrip('\r\n').split('\t')
        if len(fields) < 8:
            raise ValueError('VCF_RECORD_COLUMNS_INVALID')
        metrics['variants_total'] += 1
        annotations = [item[4:] for item in fields[7].split(';') if item.startswith('ANN=')]
        if len(annotations) > 1:
            raise ValueError('VCF_ANN_DUPLICATE_FIELD')
        entries = [item for item in (annotations[0].split(',') if annotations else []) if item and item != '.']
        metrics['variants_with_ann'] += bool(entries)
        for entry in entries:
            parts = entry.split('|')
            if len(parts) < 3 or not parts[1] or not parts[2]:
                raise ValueError('ANN_ENTRY_FIELDS_INVALID')
            metrics['ann_records_total'] += 1
            effect[parts[1]] += 1
            impact[parts[2]] += 1
    rows = [HEADERS[0], *[f'{k}\t{str(v).lower()}' for k, v in metrics.items()], '', HEADERS[1]]
    rows.extend(f'{k}\t{v}' for k, v in sorted(impact.items()))
    rows.extend(['', HEADERS[2]])
    rows.extend(f'{k}\t{v}' for k, v in sorted(effect.items()))
    return '\n'.join(rows) + '\n'


def validate(text):
    sections = {}; current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line in HEADERS:
            if line in sections:
                raise ValueError('SUMMARY_DUPLICATE_SECTION')
            current = sections[line] = {}
            continue
        if current is None:
            raise ValueError('SUMMARY_HEADER_REQUIRED')
        cells = line.split('\t')
        if len(cells) != 2 or not cells[0] or cells[0] in current:
            raise ValueError('SUMMARY_ROW_INVALID')
        current[cells[0]] = cells[1]
    if tuple(sections) != HEADERS:
        raise ValueError('SUMMARY_SECTIONS_INVALID')
    metrics = sections[HEADERS[0]]
    if set(metrics) != set(COUNTS + FLAGS):
        raise ValueError('SUMMARY_METRICS_INVALID')
    def number(raw):
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError('SUMMARY_COUNT_INVALID')
        return int(raw)
    counts = {k: number(metrics[k]) for k in COUNTS}
    if any(metrics[k] not in {'true', 'false'} for k in FLAGS):
        raise ValueError('SUMMARY_BOOLEAN_INVALID')
    if not (counts['variants_with_ann'] <= counts['variants_total']
            and counts['variants_with_ann'] <= counts['ann_records_total']):
        raise ValueError('SUMMARY_VARIANT_RELATION_INVALID')
    for header in HEADERS[1:]:
        if sum(number(v) for v in sections[header].values()) != counts['ann_records_total']:
            raise ValueError('SUMMARY_ANN_MARGINAL_TOTAL_INVALID')
    return {'schema': 'snpeff-ann-marginals-v1', 'status': 'valid', 'counts': counts,
            'units': {'variants': 'VCF_data_records', 'impact': 'ANN_entries',
                      'sequence_ontology_effect': 'whole_ANN_effect_field_not_split'},
            'header_checks': {k: metrics[k] == 'true' for k in FLAGS},
            'coverage': 'summary_internal_consistency_only', 'independent_vcf_rescan': 'NOT_RUN'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('summarize', 'validate'))
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    with args.input.open(encoding='utf-8') as stream:
        print(summarize(stream) if args.operation == 'summarize' else json.dumps(validate(stream.read()), ensure_ascii=False), end='\n')
