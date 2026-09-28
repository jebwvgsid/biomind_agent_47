"""Explicit CSV/TSV input descriptor parsing; no BioMind imports or execution."""
from pathlib import Path
import csv
import io
import json
import hashlib

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

