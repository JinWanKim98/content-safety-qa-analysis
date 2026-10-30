"""Current input verification for local rebuilds, independent of old completion reports."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def verify_sources():
    path=ROOT/'references/source_manifest.json'
    if not path.exists():path=ROOT/'references/02_source_manifest.json'
    m=json.loads(path.read_text());files=m.get('files',m.get('records',[]))
    for f in files:
        p=ROOT/f['path']
        if not p.is_file():raise FileNotFoundError('Missing pinned source: '+str(p)+'. See REPRODUCING.md.')
        if sha(p)!=f['sha256']:raise ValueError('Pinned source checksum differs: '+str(p))
    return m
def snapshot(paths=(),model=True):
    m=verify_sources();out={ROOT/f['path']:f['sha256'] for f in m.get('files',m.get('records',[]))}
    if model:
        candidates=[ROOT/'data/processed/project01.sqlite',ROOT/'data/processed/project02.sqlite',ROOT/'models/payment_financials.sqlite']
        receipt=ROOT/'reports/05_model_validation.json'
        if receipt.exists():
            r=json.loads(receipt.read_text());p=ROOT/r['model_path']
            if 'model_sha256' in r and sha(p)!=r['model_sha256']:raise ValueError('Model differs from its build receipt')
            candidates=[p]
        for p in candidates:
            if p.exists():out[p]=sha(p)
    for p in [ROOT/'analysis.sql',*sorted((ROOT/'docs').glob('*.json')),*[ROOT/p for p in paths]]:
        out[p]=sha(p)
    return out
def check_snapshot(protected):
    for p,h in protected.items():
        if sha(p)!=h:raise ValueError('Input changed during execution: '+str(p))
