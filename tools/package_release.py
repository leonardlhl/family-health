"""Package only program source. Never includes medical data, credentials or runtime output."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['START.cmd','VIEW.cmd','requirements.txt','server.py','package.json','.gitignore','AGENTS.md','README.md','CLOUD-SETUP.md','PRODUCT.md','DESIGN.md','CONTRACT.md']
SOURCE_DIRS = ['web','healthlib','tools','tests','supabase','.github']

def source_files():
    files = []
    for name in ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for name in SOURCE_DIRS:
        folder = ROOT / name
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {'.pyc','.log'}:
                files.append(path)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Source package cannot contain linked or external files.')
    return sorted(files)

def package(destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = source_files()
    manifest = {'product':'Family Health','version':'1.3.0','contains_patient_data':False,'files':{str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, 'FamilyHealth/' + str(path.relative_to(ROOT)).replace('\\','/'))
        archive.writestr('FamilyHealth/release.json', json.dumps(manifest,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():
            raise ValueError('Package integrity check failed.')
    return {'path':str(destination),'files':len(files),'bytes':destination.stat().st_size}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(package(args.output),ensure_ascii=False))
