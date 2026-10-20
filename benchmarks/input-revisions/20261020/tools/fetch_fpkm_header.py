from pathlib import Path
import urllib.request,json,hashlib
R=Path(__file__).resolve().parents[1];url='https://zenodo.org/record/4994698/files/maize_503genotypes_raw_expression_values_FPKM.txt';row={'url':url,'range':'bytes=0-65535'}
try:
 with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BioMind-input-validation/1.0','Range':row['range']}),timeout=40) as f:
  b=f.read(65536);row.update(status=f.status,content_range=f.headers.get('Content-Range'),bytes=len(b),sha256=hashlib.sha256(b).hexdigest());(R/'sources/FPKM.header.prefix').write_bytes(b)
except Exception as e:row['error']=str(e)
(R/'evidence/fpkm-header.json').write_text(json.dumps(row,indent=2));print(json.dumps(row))
