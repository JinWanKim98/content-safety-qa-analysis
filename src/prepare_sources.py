"""Acquire the exact Google DICES-350 revision without overwriting saved sources."""
import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.github.com/repos/google-research-datasets/dices-dataset/commits/main'


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def download(url, path):
    if path.exists():
        raise FileExistsError('Preserving existing source: '+str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name+'.part')
    if partial.exists():
        raise FileExistsError('Inspect the previous partial download first: '+str(partial))
    subprocess.run(['curl','--fail','--location','--retry','2','--max-time','180',
                    '--silent','--show-error','--output',str(partial),url],check=True)
    if partial.stat().st_size == 0:
        raise ValueError('Empty download: '+url)
    partial.rename(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-existing', action='store_true')
    args = parser.parse_args()
    manifest = ROOT/'references/source_manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text())
        for row in data['files']:
            assert file_hash(ROOT/row['path']) == row['sha256'],row['path']
        print('All preserved source files match the acquisition manifest.')
        return
    if args.verify_existing:
        raise FileNotFoundError('No acquisition manifest yet; acquire first.')
    meta = ROOT/'references/google_dices/commit.json'
    if meta.exists():
        commit = json.loads(meta.read_text())
    else:
        download(API,meta)
        commit = json.loads(meta.read_text())
    revision = commit['sha']
    assert re.fullmatch('[0-9a-f]{40}',revision)
    base = 'https://raw.githubusercontent.com/google-research-datasets/dices-dataset/'+revision+'/'
    declared = [
        ('README.md','references/google_dices/README.md'),
        ('350/README.md','references/google_dices/350_README.md'),
        ('350/diverse_safety_adversarial_dialog_350.csv','data/raw/dices350/diverse_safety_adversarial_dialog_350.csv')]
    files = [{'path':str(meta.relative_to(ROOT)),'url':API,'sha256':file_hash(meta),'bytes':meta.stat().st_size}]
    for source, local in declared:
        path = ROOT/local
        if not path.exists():
            download(base+source,path)
        files.append({'path':local,'url':base+source,'sha256':file_hash(path),'bytes':path.stat().st_size})
    root_readme=(ROOT/'references/google_dices/README.md').read_text()
    assert 'Creative Commons Attribution 4.0' in root_readme
    data={'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'repository':'google-research-datasets/dices-dataset',
          'revision':revision,'revision_date':commit['commit']['committer']['date'],'files':files,
          'license':'CC BY 4.0, explicitly stated in the pinned repository README',
          'license_statement':'references/google_dices/README.md#license',
          'csv_scanned':False,'tls_verification_disabled':False}
    manifest.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'revision':revision,'files':[{k:r[k] for k in ['path','bytes','sha256']} for r in files]},indent=2))


if __name__=='__main__':
    main()
