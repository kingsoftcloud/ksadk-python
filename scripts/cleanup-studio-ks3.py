"""Inventory/delete only Studio prerelease objects in its fixed product bucket."""
from pathlib import Path
import argparse, base64, datetime, hashlib, hmac, json, os, re, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=['inventory', 'cleanup'])
parser.add_argument('--plan', default='plan/ks3-inventory.json')
parser.add_argument('--sha256')
args = parser.parse_args()
assert os.environ.get('GITHUB_ACTIONS') == 'true'
bucket = 'agentkit'
origin = 'https://agentkit.ks3-cn-beijing.ksyuncs.com'
access = os.environ['STUDIO_DOWNLOAD_PROD_KS3_ACCESS_KEY']
secret = os.environ['STUDIO_DOWNLOAD_PROD_KS3_SECRET_KEY']
out = Path('.studio-cleanup-evidence'); out.mkdir(mode=0o700, exist_ok=True)
allowed = re.compile(r'(?:studio/(?:(?:feeds/(?:mac-arm64|win-x64)/[a-z0-9._-]+)|[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?/(?:(?:full/)?AgentKit-Studio-mac-arm64\.(?:dmg|zip|zip\.blockmap)|AgentKit-Studio-win-x64\.exe(?:\.blockmap)?))|dsh-desk/(?:bin|feeds)/(?:mac-arm64|win-x64)/[a-zA-Z0-9._-]+)')

def request(method, key='', query=None, subresource=''):
    date = datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    path = '/' + urllib.parse.quote(key, safe='/')
    canonical = '/' + bucket + path + ('?' + subresource if subresource else '')
    signed = base64.b64encode(hmac.new(secret.encode(), (method+'\n\n\n'+date+'\n'+canonical).encode(), hashlib.sha1).digest()).decode()
    query_text = urllib.parse.urlencode(query or {})
    if subresource: query_text = subresource + ('&' + query_text if query_text else '')
    url = origin + path + ('?' + query_text if query_text else '')
    req = urllib.request.Request(url, method=method, headers={'Date':date,'Authorization':'KSS '+access+':'+signed})
    try:
        with urllib.request.urlopen(req, timeout=90) as r: return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, e.read(), dict(e.headers)

def listing():
    results=[]
    for prefix in ['studio/', 'dsh-desk/']:
        marker=''
        while True:
            query={'prefix':prefix,'max-keys':1000}
            if marker: query['marker']=marker
            status, body, _=request('GET',query=query)
            assert status==200, 'KS3 listing HTTP '+str(status)
            root=ET.fromstring(body)
            def text(node,name): return node.findtext('{*}'+name)
            rows=root.findall('{*}Contents')
            for row in rows:
                key=text(row,'Key')
                results.append({'key':key,'size':int(text(row,'Size')),'etag':text(row,'ETag'),'lastModified':text(row,'LastModified'),'eligible':bool(allowed.fullmatch(key))})
            if text(root,'IsTruncated')!='true': break
            assert rows
            marker=text(root,'NextMarker') or text(rows[-1],'Key')
    return sorted(results,key=lambda r:r['key'])

if args.mode=='inventory':
    status, body, _=request('GET',subresource='versioning')
    versioning=ET.fromstring(body).findtext('{*}Status') if status==200 else 'unverified-http-'+str(status)
    report={'schemaVersion':1,'bucket':bucket,'origin':origin,'prefixes':['studio/','dsh-desk/'],'objects':listing(),'versioning':versioning,'readOnly':True,'noGitHubMainOrVisibilityChange':True}
    data=(json.dumps(report,indent=2)+'\n').encode();(out/'ks3-inventory.json').write_bytes(data)
    print(json.dumps({'objects':len(report['objects']),'bytes':sum(i['size'] for i in report['objects']),'ineligible':sum(not i['eligible'] for i in report['objects']),'versioning':versioning,'manifestSha256':hashlib.sha256(data).hexdigest()}))
else:
    data=Path(args.plan).read_bytes(); assert args.sha256 and hashlib.sha256(data).hexdigest()==args.sha256
    plan=json.loads(data); assert plan['bucket']==bucket and plan['origin']==origin
    # KS3 explicitly does not implement S3 Bucket Versioning APIs (official
    # compatibility table documents/959). This fixed KS3 endpoint returned
    # 404 for versioning while both scoped bucket listings succeeded.
    assert plan['versioning'] in [None,'Suspended','unverified-http-404']
    # The sole non-file entry in the reviewed inventory is the zero-byte
    # directory marker at the root of this exact owned product namespace.
    assert all((i['eligible'] and allowed.fullmatch(i['key'])) or (i['key']=='studio/' and i['size']==0) for i in plan['objects'])
    assert listing()==plan['objects'], 'Objects changed after inventory; no delete authorized'
    rows=sorted(plan['objects'],key=lambda r:('/feeds/' not in r['key'],r['key']))
    journal=[]
    try:
        for row in rows:
            status,_,headers=request('HEAD',row['key'])
            normalized={k.lower():v for k,v in headers.items()}
            assert status==200 and normalized.get('etag')==row['etag'] and int(normalized['content-length'])==row['size'], 'Object changed before deletion'
            status,_,_=request('DELETE',row['key']); assert status in [200,204], 'KS3 deletion HTTP '+str(status)
            status,_,_=request('HEAD',row['key']); assert status==404,'Deleted object remains accessible'
            journal.append({'key':row['key'],'bytes':row['size'],'deleted':True,'authenticatedHead':status})
            (out/'ks3-deletions.json').write_text(json.dumps(journal,indent=2)+'\n')
            print(json.dumps({'deleted':len(journal),'total':len(rows)}),flush=True)
        assert listing()==[], 'Studio objects remain'
        (out/'ks3-cleanup-result.json').write_text(json.dumps({'passed':True,'bucket':bucket,'objectsDeleted':len(journal),'remaining':0,'published':False},indent=2)+'\n')
    finally:
        (out/'ks3-deletions.json').write_text(json.dumps(journal,indent=2)+'\n')
