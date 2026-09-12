"""Explicitly authorized PyPI download of exactly two pure Python CLI wheels."""
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import urllib.request
import urllib.parse
import zipfile
import sys
import datetime

root=Path(sys.argv[1]).resolve();root.mkdir(exist_ok=False)
requester=urllib.request.build_opener(urllib.request.ProxyHandler({}))
packages=[('prompt_toolkit','3.0.52','prompt_toolkit-3.0.52-py3-none-any.whl'),
          ('wcwidth','0.2.13','wcwidth-0.2.13-py2.py3-none-any.whl')]
rows=[]
for name,version,filename in packages:
    metadata_url=f'https://pypi.org/pypi/{name}/{version}/json'
    with requester.open(metadata_url,timeout=30) as response:metadata=json.load(response)
    choices=[v for v in metadata['urls'] if v['filename']==filename and v['packagetype']=='bdist_wheel' and not v['yanked']]
    assert len(choices)==1,choices
    item=choices[0];url=item['url'];parts=urllib.parse.urlsplit(url)
    assert parts.scheme=='https' and parts.hostname=='files.pythonhosted.org'
    with requester.open(url,timeout=30) as response:body=response.read(2_000_001)
    assert len(body)<=2_000_000 and len(body)==item['size']
    digest=hashlib.sha256(body).hexdigest();assert digest==item['digests']['sha256']
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names=archive.namelist()
        assert all(not PurePosixPath(p).is_absolute() and '..' not in PurePosixPath(p).parts for p in names)
        assert not any('.data/scripts/' in p or p.endswith(('.so','.dll','.exe')) for p in names)
        info=next(p for p in names if p.endswith('.dist-info/METADATA'))
        wheel=next(p for p in names if p.endswith('.dist-info/WHEEL'))
        text=archive.read(info).decode();wheel_text=archive.read(wheel).decode()
        assert 'Root-Is-Purelib: true' in wheel_text
        requires=[s.split(': ',1)[1] for s in text.splitlines() if s.startswith('Requires-Dist: ')]
        assert requires==['wcwidth'] if name=='prompt_toolkit' else (len(requires)==1 and requires[0].startswith('backports.functools-lru-cache') and 'python_version < \"3.2\"' in requires[0]),requires
        rec=next(p for p in names if p.endswith('.dist-info/RECORD'))
        import base64
        for p,h,size in csv.reader(io.StringIO(archive.read(rec).decode())):
            if not h:assert p==rec;continue
            algo,expected=h.split('=',1);assert algo=='sha256'
            value=archive.read(p);actual=base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b'=').decode()
            assert actual==expected and len(value)==int(size),p
    target=root/filename;target.write_bytes(body)
    rows.append({'name':name,'version':version,'filename':filename,'path':str(target),'metadata_url':metadata_url,
                 'download_url':url,'sha256':digest,'pypi_sha256':item['digests']['sha256'],'bytes':len(body),
                 'requires_dist':requires,'wheel_metadata':wheel_text,'wheel_record_verified':True,
                 'downloaded_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
(root/'provenance.json').write_text(json.dumps(rows,indent=2));print(json.dumps(rows,indent=2))
