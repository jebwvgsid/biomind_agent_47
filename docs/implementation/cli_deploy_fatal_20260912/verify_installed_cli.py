"""Fresh -I -S target process: actual package paths/versions and wheel byte parity."""
import base64
import csv
import hashlib
import importlib
import importlib.metadata
import io
import json
from pathlib import Path
import sys
import sysconfig
import zipfile

assert sys.executable=='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
assert sys.flags.isolated and sys.flags.no_site
sys.dont_write_bytecode=True
site=Path(sysconfig.get_path('purelib'));sys.path.insert(0,str(site))
wheel_root,result=map(Path,sys.argv[1:]);assert not result.exists()
rows=[]
for name,version,filename,expected in [
 ('prompt_toolkit','3.0.52','prompt_toolkit-3.0.52-py3-none-any.whl','9aac639a3bbd33284347de5ad8d68ecc044b91a762dc39b7c21095fcd6a19955'),
 ('wcwidth','0.2.13','wcwidth-0.2.13-py2.py3-none-any.whl','3da69048e4540d84af32131829ff948f1e022c1c6bdb8d6102117aac784f6859')]:
    module=importlib.import_module(name);path=Path(module.__file__).resolve()
    assert path.is_relative_to(site/name)
    dist=importlib.metadata.distribution(name);assert dist.version==version
    assert getattr(module,'__version__')==version
    wheel=wheel_root/filename;assert hashlib.sha256(wheel.read_bytes()).hexdigest()==expected
    checked=[]
    with zipfile.ZipFile(wheel) as z:
        record=next(n for n in z.namelist() if n.endswith('.dist-info/RECORD'))
        for n,h,size in csv.reader(io.StringIO(z.read(record).decode())):
            if not h:continue
            assert '..' not in Path(n).parts and not Path(n).is_absolute()
            actual=site/n;body=actual.read_bytes()
            assert body==z.read(n),n
            checked.append({'path':n,'sha256':hashlib.sha256(body).hexdigest()})
    rows.append({'package':name,'version':version,'module_file':str(path),'distribution_path':str(dist.locate_file('')),
                 'wheel_sha256':expected,'wheel_files_verified':checked})
receipt={'executable':sys.executable,'version':sys.version,'packages':rows,'cache_on_path':any('/tmp/biomind-cli-deps' in p for p in sys.path),
         'status':'DEPLOYED','scope':'CLI packages only; no BioMind startup or old process inspected'}
assert not receipt['cache_on_path'];result.write_text(json.dumps(receipt,indent=2));print(json.dumps({k:v for k,v in receipt.items() if k!='packages'}))
