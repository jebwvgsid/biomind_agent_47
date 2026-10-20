from pathlib import Path
import zipfile,xml.etree.ElementTree as E,posixpath
ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
def sheets(path):
 with zipfile.ZipFile(path) as z:return [x.get('name') for x in E.fromstring(z.read('xl/workbook.xml')).findall('.//m:sheet',ns)]
def rows(path,name):
 with zipfile.ZipFile(path) as z:
  wb=E.fromstring(z.read('xl/workbook.xml')); rel={x.get('Id'):x.get('Target') for x in E.fromstring(z.read('xl/_rels/workbook.xml.rels'))}; s=next(x for x in wb.findall('.//m:sheet',ns) if x.get('name')==name); t=rel[s.get('{'+ns['r']+'}id')];t=t.lstrip('/') if t.startswith('/') else posixpath.normpath('xl/'+t)
  st=[''.join(x.itertext()) for x in E.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si',ns)] if 'xl/sharedStrings.xml' in z.namelist() else []
  out=[]
  for row in E.fromstring(z.read(t)).findall('.//m:row',ns):
   d={}
   for c in row.findall('m:c',ns):
    v=c.find('m:v',ns);v=v.text if v is not None else ''
    if c.get('t')=='s':v=st[int(v)]
    elif c.get('t')=='inlineStr':v=''.join(c.find('m:is',ns).itertext())
    d[''.join(x for x in c.get('r') if x.isalpha())]=v
   out.append(d)
  return out
if __name__=='__main__':
 import sys,json
 for p in map(Path,sys.argv[1:]):
  print(p.name)
  for s in sheets(p):print(s,len(rows(p,s)),json.dumps(rows(p,s)[:5],ensure_ascii=False))
