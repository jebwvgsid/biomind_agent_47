"""Standalone task-directory delivery writer. Standard library, no scientific execution.

Copied into a task directory for report/status generation and a future reproduction
script's read-only gate. This file never imports the application or writes Commit.
"""
from pathlib import Path
import argparse
import csv
import io
import json
import shlex
import hashlib
import os
import stat


def parse_input_manifest(data, descriptor):
    """One explicit table parser shared by live admission and portable delivery.

    This assigns no permissions and infers no biological relationship.
    """
    if len(data)>1024*1024:raise ValueError('input manifest exceeds the existing bounded text-file read limit')
    path = Path(descriptor['path'])
    if not path.is_absolute():
        raise ValueError('manifest path must be absolute')
    fmt = descriptor.get('format')
    if fmt not in {'txt','tsv','csv'}:
        raise ValueError('format must explicitly be txt, tsv or csv')
    delimiter = descriptor.get('delimiter', {'csv':',','tsv':'\t'}.get(fmt))
    if not isinstance(delimiter,str) or len(delimiter)!=1 or delimiter in '\r\n':
        raise ValueError('TXT requires an explicit single-character delimiter')
    columns = descriptor.get('columns')
    if not isinstance(columns,dict) or not isinstance(columns.get('sample'),str) or not isinstance(columns.get('files'),dict) or not columns['files']:
        raise ValueError('columns.sample and columns.files (role to column) are required')
    labels = {k:v for k,v in columns.items() if k!='files'}
    if any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in {**labels,**columns['files']}.items()):
        raise ValueError('column names and roles must be nonempty strings')
    identity = descriptor.get('row_identity', ['sample'])
    if not isinstance(identity,list) or not identity or len(set(identity))!=len(identity) or 'sample' not in identity or any(k not in labels for k in identity):
        raise ValueError('row_identity must name distinct declared identity columns including sample')
    text = data.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = reader.fieldnames or []
    if len(header)!=len(set(header)):
        raise ValueError('duplicate column names at line 1')
    missing = sorted(set([*labels.values(),*columns['files'].values()])-set(header))
    if missing:
        raise ValueError('missing columns at line 1: '+','.join(missing))
    rows, seen, samples, files = [], set(), [], []
    for raw in reader:
        line = reader.line_num
        if None in raw:
            raise ValueError('extra fields at line '+str(line))
        values = {role:str(raw.get(column) or '').strip() for role,column in labels.items()}
        selected = {role:str(raw.get(column) or '').strip() for role,column in columns['files'].items()}
        if any(not value for value in [*values.values(),*selected.values()]):
            raise ValueError('missing/empty selected value at line '+str(line))
        key = json.dumps([values[k] for k in identity],ensure_ascii=False,separators=(',',':'))
        if key in seen:
            raise ValueError('duplicate row identity at line '+str(line)+': '+key)
        seen.add(key)
        if values['sample'] not in samples:
            samples.append(values['sample'])
        resolved={}
        for role,value in selected.items():
            p=Path(value)
            if '\x00' in value or '..' in p.parts:
                raise ValueError('invalid/traversing member path at line '+str(line))
            if not p.is_absolute(): p=path.parent/p
            resolved[role]=str(p)
            if str(p) not in files:files.append(str(p))
        rows.append({'row_id':key,'sample':values['sample'],'identity':{k:values[k] for k in identity},'files':resolved,'line':line})
    if not rows:
        raise ValueError('input manifest has no rows')
    normalized={**descriptor,'path':str(path),'format':fmt,'delimiter':delimiter,'columns':columns,'row_identity':identity}
    digest=hashlib.sha256(data).hexdigest()
    version=hashlib.sha256(json.dumps({'descriptor':normalized,'sha256':digest},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'path':str(path),'sha256':digest,'version':version,'descriptor':normalized,
        'row_identity':identity,'samples':samples,'rows':rows,'files':files}


def _rows(path, columns):
    path = Path(path)
    if path.stat().st_size > 4*1024*1024: raise ValueError('delivery table exceeds bounded read')
    reader = csv.DictReader(io.StringIO(path.read_text()), delimiter='\t')
    if not set(columns) <= set(reader.fieldnames or []): raise ValueError('missing columns: '+','.join(columns))
    return list(reader)


def _portable_fingerprint(path):
    """Same bounded hash strength as runtime; no-follow every path component."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute non-traversing evidence path required')
    parent=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
                raise ValueError('regular single-link file required')
            full=before.st_size<=4*1024*1024
            data=stream.read(4*1024*1024 if full else 65536)
            after=os.fstat(stream.fileno())
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                raise ValueError('evidence changed while reading')
            return {'path':str(path),'size':before.st_size,'mtime_ns':before.st_mtime_ns,
                'sha256' if full else 'sample_sha256':hashlib.sha256(data).hexdigest(),
                'hash_coverage_bytes':len(data),'hash_coverage':'full' if full else 'prefix'},data
    finally:
        os.close(parent)


def _delivery_input_view(manifest):
    descriptor=manifest['input_manifest'];snapshot=manifest['input_manifest_snapshot']
    fp,data=_portable_fingerprint(descriptor['path'])
    if fp['hash_coverage']!='full':raise ValueError('manifest needs a complete bounded read')
    view=parse_input_manifest(data,descriptor)
    if any(view[k]!=snapshot.get(k) for k in ('path','sha256','version','descriptor')):
        raise ValueError('INPUT_MANIFEST_CHANGED')
    expected=snapshot.get('files') or {}
    if set(expected)!={view['path'],*view['files']}:
        raise ValueError('INPUT_MANIFEST_MEMBER_SET_CHANGED')
    for path in expected:
        actual=fp if path==view['path'] else _portable_fingerprint(path)[0]
        if actual!=expected[path]:raise ValueError('INPUT_MEMBER_CHANGED: '+path)
    return view


def _render_manifest_delivery(root,manifest,execution_status='observed'):
    """Generic reporting consumes the original list and actual result table."""
    root=Path(root);view=_delivery_input_view(manifest)
    result=manifest['result_manifest'];path=Path(result['path'])
    fp,data=_portable_fingerprint(path)
    if fp['hash_coverage']!='full':raise ValueError('result manifest needs a complete bounded read')
    reader=csv.DictReader(io.StringIO(data.decode('utf-8')),delimiter=',' if result.get('format')=='csv' else '\t')
    wanted={r['row_id'] for r in view['rows']};seen={};issues=[]
    output_root=Path(result.get('output_root') or path.parent).absolute()
    for row in reader:
        if None in row:raise ValueError('malformed result manifest row '+str(reader.line_num))
        key=row.get('row_id') or json.dumps([row.get(k,'') for k in view['row_identity']],ensure_ascii=False,separators=(',',':'))
        if key not in wanted:issues.append('unexpected row '+key)
        signature=(row.get('status'),row.get('return_code'),row.get('path'))
        if key in seen and seen[key]!=signature:issues.append('conflicting row '+key)
        seen[key]=signature
        if row.get('status')!='success' or row.get('return_code')!='0':issues.append('failed or unknown row '+key)
        member=Path(row.get('path') or '')
        if not member.is_absolute():member=output_root/member
        if not member.is_relative_to(output_root) or not member.resolve().is_relative_to(output_root.resolve()):
            raise ValueError('result member outside declared output root')
        member_fp,_=_portable_fingerprint(member)
        if not member_fp['size']:issues.append('empty output for '+key)
    if set(seen)!=wanted:issues.append('result coverage incomplete')
    if execution_status in {'failed','blocked'}:issues.append('recorded execution status: '+execution_status)
    status='success' if not issues else execution_status if execution_status in {'failed','blocked'} else 'blocked'
    root.mkdir(parents=True,exist_ok=True)
    (root/'report.md').write_text('# Analysis delivery\n\n'+str(manifest.get('method') or 'Recorded workflow')+'\n\n'
        +'Input manifest: `'+view['path']+'`\n\nSHA256: `'+view['sha256']+'`\n\n'
        +f"Samples: {len(view['samples'])}; input rows: {len(view['rows'])}; result rows: {len(seen)}.\n\n"
        +'Computation status: '+status+'\n\n'
        +'This writer checks recorded status, paths and coverage; it does not execute science, certify biological conclusions or write framework Commit.\n\n'
        +'\n'.join('- '+issue for issue in issues)+'\n')
    (root/'stage_status.tsv').write_text('stage\texecution_status\tdetail\nresults\t'+status+'\tinput-list row coverage and saved outputs\n')
    return {'computation_status':status,'issues':issues,'written':['report.md','stage_status.tsv'],
        'scientific_execution_count':0,'samples':len(view['samples']),'input_rows':len(view['rows']),
        'input_manifest_version':view['version']}


def usable_gate(root, manifest):
    root = Path(root);samples = manifest['samples'];resolution = manifest['resolution_bp']
    rows = _rows(root/manifest['balance_table'], ['sample','matrix','resolution_bp','usable_for_3d_genome_analysis'])
    selected = [r for r in rows if str(r['resolution_bp'])==str(resolution)]
    if len(selected)!=len(samples) or {r['sample'] for r in selected} != set(samples):
        raise ValueError('working resolution/sample missing or duplicate')
    for row in selected:
        if row['usable_for_3d_genome_analysis'].lower()!='yes' or row['matrix']!=manifest['inputs'][row['sample']]:
            raise ValueError('working resolution not usable or input mismatch: '+row['sample'])
    resolution_file = root/'resolution_preflight.jsonl'
    resolution_ok = False
    if resolution_file.is_file():
        if resolution_file.stat().st_size > 1024*1024: raise ValueError('resolution observation too large')
        coverage=set()
        for line in resolution_file.read_text().splitlines():
            try: row=json.loads(line)
            except ValueError: continue
            if row.get('ok') is True and str(row.get('resolution_bp'))==str(resolution):
                checks=row.get('inputs') or []
                if any(r.get('resource_handle') not in samples or r.get('path')!=manifest['inputs'].get(r.get('resource_handle')) or r.get('present') is not True or r.get('target_group') not in {'/resolutions/'+str(resolution),'/'} or (r.get('target_group')=='/' and str(r.get('root_resolution'))!=str(resolution)) for r in checks):
                    raise ValueError('negative/conflicting resolution batch')
                coverage.update(r['resource_handle'] for r in checks)
            elif 'inputs' in row:
                raise ValueError('negative or mismatched resolution batch')
        resolution_ok=coverage==set(samples)
    if not resolution_ok:
        resolution_ok=any(r.get('kind')=='resolution' and r.get('present') and
            str(r.get('resolution_bp'))==str(resolution) and set(r.get('samples') or [])==set(samples)
            for r in manifest.get('accepted_facts') or [])
    if not resolution_ok: raise ValueError('resolution observation missing or negative')
    return {'samples':samples,'resolution_bp':resolution,'usable':True}


def render_delivery(root, manifest, *, execution_status='observed'):
    """Consume saved small tables; failed/missing stages never become success."""
    if manifest.get('input_manifest'):
        return _render_manifest_delivery(root,manifest,execution_status)
    root=Path(root);stages=[];issues=[]
    try:usable_gate(root,manifest);preflight=True
    except (OSError,ValueError) as exc:preflight=False;issues.append(str(exc))
    stages.append(('preflight','success' if preflight else 'blocked','; '.join(issues) or 'declared samples and working resolution usable'))
    samples=manifest['samples'];results=[];stats=[]
    try:
        results=_rows(root/manifest['run_summary'],['sample','resolution_bp','status','return_code'])
        success=(len(results)==len(samples) and {r['sample'] for r in results}==set(samples) and
            all(r['status']=='success' and r['return_code']=='0' and str(r['resolution_bp'])==str(manifest['resolution_bp']) for r in results))
        if not success:issues.append('sample-level result failed or incomplete')
        stats=_rows(root/manifest['statistics'],['sample'])
        if len(stats)!=len(samples) or {r['sample'] for r in stats}!=set(samples):success=False;issues.append('statistics sample set incomplete')
    except (OSError,ValueError) as exc:success=False;issues.append(str(exc))
    status='success' if success and preflight and execution_status not in {'failed','blocked'} else 'blocked' if not preflight or execution_status=='blocked' else 'failed' if execution_status=='failed' or results else 'pending'
    stages.append(('tad_calling',status,'recorded per-sample results; no computation performed by this writer'))
    report=['# Analysis delivery','',f"Resolution: {manifest['resolution_bp']} bp",'',
        'Method: '+str(manifest.get('method') or 'See the declared workflow.'),'',
        'Inputs:',*[f"- {s}: `{manifest['inputs'][s]}`" for s in samples],'',
        'Computation status: '+status,'',
        'System acceptance: consult BioMind’s authoritative per-step Commit records. This script does not certify or write framework acceptance.','',
        'Existing registered runtime: '+str(manifest.get('launcher') or 'recorded configuration'),'',
        'This report consumes saved observations and tables; it does not run scientific analysis or independently validate biological conclusions.','']
    if stats:
        columns=list(stats[0]);report+=['| '+' | '.join(columns)+' |','| '+' | '.join('---' for _ in columns)+' |']
        report += ['| '+' | '.join(str(row.get(k,'')).replace('|','\\|') for k in columns)+' |' for row in stats]
    if issues:report+=['','Unresolved observations:',*['- '+v for v in issues]]
    report+=['','Reproduction scope: the generated script requires a new output directory, performs both preflights and the explicit usable gate, then analysis and report/status generation. It is never the existing-result review entry.']
    root.mkdir(parents=True,exist_ok=True)
    (root/'report.md').write_text('\n'.join(report)+'\n')
    stages.extend([('write_report','success','non-scientific document written'),('write_stage_status','success','computation status only; no framework Commit')])
    (root/'stage_status.tsv').write_text('stage\texecution_status\tdetail\n'+''.join('\t'.join(row)+'\n' for row in stages))
    return {'computation_status':status,'issues':issues,'written':['report.md','stage_status.tsv'],'scientific_execution_count':0}


def reproduction_script(manifest):
    """Generate, never run, an explicit future workflow using its frozen launcher."""
    if manifest.get('input_manifest'):
        reproduction=manifest.get('reproduction') or {}
        argv=reproduction.get('argv')
        if not isinstance(argv,list) or not argv or any(not isinstance(v,str) or not v for v in argv):
            raise ValueError('REPRODUCTION_ENTRY_UNDECLARED: an actual helper argv is required')
        manifest_flag=reproduction.get('manifest_flag','--input-manifest')
        output_flag=reproduction.get('output_flag','--out-dir')
        if any(not isinstance(v,str) or not v.startswith('--') or any(c.isspace() for c in v) for v in (manifest_flag,output_flag)):
            raise ValueError('invalid explicit helper argument flags')
        return '\n'.join(['#!/usr/bin/env bash','set -euo pipefail',
            '# Future execution only; never used to certify current outputs.',
            'RUN_OUTPUT="${1:?provide a NEW output directory}"',
            'if [[ -e "$RUN_OUTPUT" ]]; then echo "Output already exists" >&2; exit 2; fi',
            'DELIVERY_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"',
            shlex.join(shlex.split(manifest.get('launcher') or 'python3'))+' "$DELIVERY_DIR/deliver_existing.py" verify-inputs --root "$RUN_OUTPUT" --manifest "$DELIVERY_DIR/delivery_manifest.json"',
            shlex.join(argv)+' '+shlex.quote(manifest_flag)+' "$DELIVERY_DIR/input_manifest.json" '+shlex.quote(output_flag)+' "$RUN_OUTPUT"',''])
    launch=' '.join(shlex.quote(s) for s in shlex.split(manifest['launcher']))
    q=shlex.quote
    scripts=manifest['scripts'];samples=manifest['samples'];resolution=str(manifest['resolution_bp'])
    lines=['#!/usr/bin/env bash','set -euo pipefail',
        '# Future reproduction only. Never use this entry to accept existing results.',
        'RUN_OUTPUT="${1:?provide a NEW output directory}"',
        'if [[ -e "$RUN_OUTPUT" ]]; then echo "Output already exists; choose a new directory" >&2; exit 2; fi',
        'mkdir -p "$RUN_OUTPUT"',
        'DELIVERY_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"',
        'MANIFEST="$DELIVERY_DIR/delivery_manifest.json"',
        'DELIVERY_HELPER="$DELIVERY_DIR/deliver_existing.py"',
        'on_error() { result_code=$?; '+launch+' "$DELIVERY_HELPER" render --root "$RUN_OUTPUT" --manifest "$MANIFEST" --status failed --fresh-resolution; exit "$result_code"; }',
        'trap on_error ERR',
        "printf 'sample\\tmatrix\\n' > \"$RUN_OUTPUT/samples.tsv\""]
    lines += ['printf '+q('%s\t%s\n')+' '+q(s)+' '+q(manifest['inputs'][s])+' >> "$RUN_OUTPUT/samples.tsv"' for s in samples]
    lines.append(': > "$RUN_OUTPUT/resolution_preflight.jsonl"')
    for offset in range(0,len(samples),64):
        lines.append(launch+' '+q(scripts['resolution'])+' '+ ' '.join('--input '+q(s)+' '+q(manifest['inputs'][s]) for s in samples[offset:offset+64])+' --resolution-bp '+q(resolution)+' --observation '+q('reproduction-resolution')+' >> "$RUN_OUTPUT/resolution_preflight.jsonl"')
    lines.append(launch+' '+q(scripts['balance'])+' '+' '.join('--sample '+q(s)+' '+q(manifest['inputs'][s]) for s in samples)+' --out-dir "$RUN_OUTPUT"')
    # set -e plus this standalone command fences all following scientific work.
    lines.append(launch+' "$DELIVERY_HELPER" gate --root "$RUN_OUTPUT" --manifest "$MANIFEST" --fresh-resolution')
    lines.append(launch+' '+q(scripts['analysis'])+' --sample-sheet "$RUN_OUTPUT/samples.tsv" --resolution-bp '+q(resolution)+' --out-root "$RUN_OUTPUT" --correct-for-multiple-testing '+q(manifest['multiple_testing'])+' --no-auto-balance --plot-lengths')
    lines.append(launch+' "$DELIVERY_HELPER" render --root "$RUN_OUTPUT" --manifest "$MANIFEST" --status observed --fresh-resolution')
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['gate','render','verify-inputs']);parser.add_argument('--root',required=True);parser.add_argument('--manifest',required=True);parser.add_argument('--status',default='observed');parser.add_argument('--fresh-resolution',action='store_true')
    args=parser.parse_args(argv);manifest=json.loads(Path(args.manifest).read_text())
    if args.fresh_resolution:manifest['accepted_facts']=[]
    try:
        if args.action=='verify-inputs':
            view=_delivery_input_view(manifest)
            result={'status':'unchanged','input_manifest_version':view['version'],'rows':len(view['rows'])}
        else:
            result=usable_gate(args.root,manifest) if args.action=='gate' else render_delivery(args.root,manifest,execution_status=args.status)
    except (OSError,ValueError) as exc:
        print(json.dumps({'status':'blocked','error':str(exc)}));return 2
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
