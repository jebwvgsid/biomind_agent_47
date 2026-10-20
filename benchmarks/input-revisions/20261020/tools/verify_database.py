from pathlib import Path
import urllib.request,urllib.parse,json,time,hashlib,xml.etree.ElementTree as E,csv
R=Path(__file__).resolve().parents[1];O=R/'evidence/database-live';O.mkdir(exist_ok=True);q=json.loads((R/'inputs/database/query_input.json').read_text());log=[];base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
def get(name,url):
 for attempt in range(3):
  time.sleep(.4+attempt)
  row={'name':name,'url':url,'attempt':attempt+1,'time':time.strftime('%FT%T%z')}
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BioMind-input-validation/1.0','Accept':'application/json' if name.endswith('json') else '*/*'}),timeout=30) as f:
    b=f.read(10000001);assert len(b)<=10000000;row.update(status=f.status,bytes=len(b),sha256=hashlib.sha256(b).hexdigest());(O/name).write_bytes(b)
   log.append(row);(O/'query_log.json').write_text(json.dumps(log,indent=2));return b
  except Exception as e:
   row['error']=str(e);log.append(row);(O/'query_log.json').write_text(json.dumps(log,indent=2))
 raise RuntimeError(row)
def url(action,**kw):return base+action+'.fcgi?'+urllib.parse.urlencode(kw)
s=json.loads(get('gds_search.json',url('esearch',db='gds',term=q['geo_query'],retmode='json',retmax=20,sort='relevance')));ids=s['esearchresult']['idlist'];assert ids
su=json.loads(get('gds_summary.json',url('esummary',db='gds',id=','.join(ids),retmode='json')))['result'];assert all(i in su for i in ids)
# Validate one real linked record per target DB; inspect all returned GEO IDs if necessary.
status={'geo':{'total_count':int(s['esearchresult']['count']),'page_size':len(ids),'accessions':[su[i].get('accession') for i in ids]}}
for db in ['sra','pubmed']:
 links={}
 for i in ids:
  x=E.fromstring(get(f'{db}_link_{i}.xml',url('elink',dbfrom='gds',db=db,id=i,retmode='xml')));v=[n.text for n in x.findall('.//LinkSetDb/Link/Id')];links[i]=v
  if v:
   take=v[:min(len(v),q[db+'_fetch_limit_per_geo_uid'])];b=get(db+'_fetch.xml',url('efetch',db=db,id=','.join(take),retmode='xml'));root=E.fromstring(b);assert not root.findall('.//ERROR');status[db]={'status':'PASS','geo_uid':i,'linked_count':len(v),'fetched_ids':take,'xml_root':root.tag};break
 else:status[db]={'status':'NO_LINKS','inspected':len(ids)}
 (O/(db+'_mapping.json')).write_text(json.dumps(links,indent=2))
e=json.loads(get('ensembl.json',q['ensembl_endpoint']));assert e['id']==q['gene_stable_id'] and e['species']==q['species'];status['ensembl']={'status':'PASS','id':e['id'],'species':e['species'],'assembly':e['assembly_name']};status['scope']='Live first GEO search page and real linked fetch for SRA/PubMed plus IFNAR1; not full benchmark evaluation';status['status']='PASS';(O/'verification.json').write_text(json.dumps(status,indent=2));print(json.dumps(status,indent=2))
