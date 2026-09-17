"""Export explicit source roots; never open config, hidden files or credentials."""
from pathlib import Path
import hashlib,json,shutil,sys
source=Path(sys.argv[1]).resolve();target=Path(sys.argv[2]).resolve()
if not str(target).startswith('/tmp/'):raise SystemExit('candidate must be under /tmp')
extensions={'.py','.md','.json','.yaml','.yml','.txt','.tsv','.toml','.ini','.fa','.fasta','.fq','.fastq','.vcf','.gff','.gff3','.r','.R','.sh','.csv','.sam','.dict','.fai','.ann','.amb','.pac','.bwt','.sa'}
roots=['agent_core','prompts','skills','execution','tools','apps','workflows','integrations','tests','memory']
paths=[p for name in roots if (source/name).is_dir() for p in (source/name).rglob('*')]
paths += [source/name for name in ['bio_agents.py','package_manager.py','supervisor.py','memory_manager.py','text_rag.py','requirements-cli.txt']]
manifest={}
for path in sorted(paths):
 rel=path.relative_to(source)
 if path.is_symlink() or not path.is_file() or path.suffix not in extensions or any(part.startswith('.') or part=='__pycache__' for part in rel.parts) or path.name=='startup.py':continue
 dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
 manifest[str(rel)]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'mode':path.stat().st_mode&0o777}
(target/'source_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True))
print(json.dumps({'candidate':str(target),'files':len(manifest),'manifest_sha256':hashlib.sha256((target/'source_manifest.json').read_bytes()).hexdigest()}))
