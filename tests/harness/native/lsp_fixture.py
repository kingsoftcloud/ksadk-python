"""Independent stdio peer for transport/lifecycle checks, not a type checker."""

import hashlib
import sys
from pathlib import Path

from ksadk.harness.diagnostic_adapters.contracts import DiagnosticResource, LanguageServerProfile

SOURCE = r"""
import json, os, subprocess, sys, time
from pathlib import Path
from urllib.parse import unquote, urlparse
mode, trace, startup_delay = sys.argv[1:]
time.sleep(float(startup_delay))
documents = {}
Path(trace+'.pid').write_text(str(os.getpid()))
def emit(value):
    data = json.dumps({'jsonrpc':'2.0', **value}).encode()
    sys.stdout.buffer.write(('Content-Length: %d\r\n\r\n' % len(data)).encode()+data)
    sys.stdout.buffer.flush()
def issues(text):
    return ([{'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':3}},
              'severity':1,'message':'Incompatible assignment','code':'fixture_type'}]
            if 'BAD' in text else [])
while True:
    header=[]
    while True:
        line=sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        if line==b'\r\n': break
        header.append(line)
    length=int(next(s.split(b':',1)[1] for s in header if s.startswith(b'Content-Length:')))
    request=json.loads(sys.stdin.buffer.read(length))
    with open(trace,'a') as output: output.write(json.dumps(request)+'\n')
    method=request.get('method'); params=request.get('params',{})
    if method=='initialize':
        emit({'id':request['id'],'result':{'capabilities':{
            'textDocumentSync':2, **({'diagnosticProvider':{}} if mode=='pull' else {})}}})
    elif method in ('textDocument/didOpen','textDocument/didChange'):
        doc=params['textDocument']; uri=doc['uri']
        text=doc.get('text', params.get('contentChanges',[{}])[0].get('text',''))
        documents[uri]=text
        if 'HANG' in text:
            child=subprocess.Popen([sys.executable,'-c',
                "import time;from pathlib import Path;time.sleep(1);Path("+
                repr(trace+'.late')+").write_text('late')"])
            Path(trace+'.child').write_text(str(child.pid))
        elif mode!='pull':
            emit({'method':'textDocument/publishDiagnostics','params':{
                'uri':uri,'version':doc['version']-1,'diagnostics':issues('BAD')}})
            emit({'method':'textDocument/publishDiagnostics','params':{
                'uri':uri,'version':doc['version'],'diagnostics':issues(text)}})
    elif method=='textDocument/diagnostic':
        uri=params['textDocument']['uri']
        emit({'id':request['id'],'result':{'kind':'full','items':issues(documents[uri]),
             'relatedDocuments':{'file:///outside-workspace.py':{'kind':'full','items':issues('BAD')}}}})
    elif method=='shutdown': emit({'id':request['id'],'result':None})
"""


def profile(tmp_path, *, mode="push", timeout=2.0, languages=None, startup_delay=0.0):
    source = tmp_path / "language_server.py"
    source.write_text(SOURCE)
    trace = tmp_path / "lsp-trace.jsonl"
    executable = Path(sys.executable).resolve()
    resources = tuple(
        DiagnosticResource(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (executable, source)
    )
    return LanguageServerProfile(
        server_id="fixture-lsp",
        version="1",
        argv=(str(executable), str(source), mode, str(trace), str(startup_delay)),
        languages=languages or {".py": "python", ".ts": "typescript"},
        resources=resources,
        timeout_seconds=timeout,
    ), trace
