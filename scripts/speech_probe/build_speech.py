"""Build a local CPU speech worker from pinned wheels and verified local assets."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    for name in ('output', 'work', 'resources'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    output, work = args.output.resolve(), args.work.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    lock = Path(__file__).with_name('speech-requirements-lock.txt')
    expected = {line.split('==')[0].lower().replace('_','-'):line.split('==')[1].split()[0]
                for line in lock.read_text().splitlines() if '==' in line}
    actual = {d.metadata['Name'].lower().replace('_','-'):d.version for d in metadata.distributions()}
    if expected != actual:
        raise RuntimeError('Speech build environment must match the full lock')
    resource_lock = Path(__file__).with_name('speech-resources-lock.json')
    source = args.resources.resolve(strict=True)
    assets = json.loads(resource_lock.read_text())
    for row in assets['files']:
        path = (source/row['path']).resolve(strict=True)
        if not path.is_relative_to(source) or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise RuntimeError('Speech resource mismatch')
    command = [sys.executable, '-m', 'PyInstaller', '--onedir', '--console', '--noupx',
               '--name', 'Qiban.Speech', '--distpath', str(output), '--workpath', str(work/'build'),
               '--specpath', str(work), '--paths', str(ROOT/'src'), '--collect-all', 'faster_whisper', '--collect-all', 'ctranslate2',
               '--collect-all', 'av', '--collect-all', 'onnxruntime', '--collect-all', 'sherpa_onnx',
               '--add-data', str(ROOT/'src/companion_core/speech_decode.cjs')+';.',
               '--add-data', str(ROOT/'src/companion_core/speech_encode.cjs')+';companion_core',
               str(ROOT/'src/companion_core/speech_worker.py')]
    with (work/'build.log').open('w',encoding='utf8') as log:
        subprocess.run(command,check=True,stdout=log,stderr=subprocess.STDOUT,cwd=work)
    bundle = output/'Qiban.Speech'
    for row in assets['files']:
        target = bundle/'resources'/row['path']
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/row['path'],target)
    shutil.copyfile(resource_lock,bundle/'resources/speech-resources-lock.json')
    licenses=bundle/'licenses'
    licenses.mkdir()
    packages=[]
    supplements = {r['name']:r for r in json.loads(Path(__file__).with_name('licenses').joinpath('sources.json').read_text())}
    for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
        copied=[]
        for file in dist.files or []:
            if any(word in file.name.lower() for word in ('license','licence','copying','notice')) and dist.locate_file(file).is_file():
                target=licenses/dist.metadata['Name']/str(file).replace('../','_parent_/')
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(dist.locate_file(file),target)
                copied.append(target.relative_to(bundle).as_posix())
        if not copied and dist.metadata['Name'].lower() in supplements:
            row=supplements[dist.metadata['Name'].lower()]
            original=Path(__file__).with_name('licenses')/row['path']
            if row['version']!=dist.version or hashlib.sha256(original.read_bytes()).hexdigest()!=row['sha256']:
                raise RuntimeError('Supplemental license mismatch')
            target=licenses/dist.metadata['Name']/row['path']
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original,target)
            copied.append(target.relative_to(bundle).as_posix())
        if not copied:
            raise RuntimeError('Missing license text: '+dist.metadata['Name'])
        packages.append({'name':dist.metadata['Name'],'version':dist.version,'license_files':copied})
    shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt',licenses/'CPython-LICENSE.txt')
    shutil.copyfile(ROOT/'LICENSE',licenses/'Qiban-LICENSE.txt')
    shutil.copyfile(lock,bundle/lock.name)
    shutil.copyfile(Path(__file__).with_name('licenses')/'sources.json',licenses/'supplemental-sources.json')
    (bundle/'build-packages.json').write_text(json.dumps(packages,indent=2),encoding='utf8')
    files=[{'path':p.relative_to(bundle).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
           for p in sorted(bundle.rglob('*')) if p.is_file()]
    (bundle/'runtime-manifest.json').write_text(json.dumps({'format':1,'entrypoint':'Qiban.Speech.exe','files':files},indent=2),encoding='utf8')
    print(json.dumps({'bundle':str(bundle),'files':len(files),'bytes':sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file())}))


if __name__=='__main__':
    main()
