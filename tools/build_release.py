#!/usr/bin/env python3
"""Package committed source and the reviewed original patch units; never reads firmware.

Each unit is recompiled with the supplied compiler whose identity is the one recorded for it in
resources/compiled.json, or, with --inherit, taken from an earlier toolkit whose manifest attests the
identical reviewed unit (same source digests, flags, compiler, symbols and bytes).
"""
import argparse,hashlib,json,re,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.check_release import SOURCE_FILES,MANIFEST,TAG,check_release,digest,need
from z8patch.compiled import compile_unit,compiler_identity

def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args])
def inherited(path,units):
    """Units of an earlier toolkit ZIP listed in the SHA256SUMS.txt beside it whose manifest records the
    identical reviewed unit; returns ({name: code}, provenance)."""
    raw=path.read_bytes();archive_sha256=digest(raw)
    sums=[line.split() for line in (path.parent/'SHA256SUMS.txt').read_text().splitlines() if line.strip()]
    need([archive_sha256,path.name] in sums,'Inherited toolkit is not the archive its SHA256SUMS.txt lists')
    with zipfile.ZipFile(path) as archive:
        prefix=archive.namelist()[0].split('/')[0]
        record=json.loads(archive.read(prefix+'/'+MANIFEST))
        need(record['schema']==2 and prefix=='NikonFirmwarePatch-'+record['tag'],'Inherited toolkit identity mismatch')
        code={}
        for name,unit in units.items():
            if record['compiled_units'].get(name)!=unit:continue
            data=archive.read(prefix+'/'+unit['resource'])
            need(record['files'][unit['resource']]=={'bytes':len(data),'sha256':digest(data)},'Inherited member digest mismatch: '+name)
            need(len(data)==unit['bytes'] and digest(data)==unit['sha256'],'Inherited unit differs from its review: '+name)
            code[name]=data
    return code,dict(tag=record['tag'],source_commit=record['source_commit'],archive_sha256=archive_sha256,units=sorted(code))
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--tag',required=True)
    ap.add_argument('--compiler',nargs='+',default=['clang'],help='Clang executables; each unit is compiled by the one with its reviewed identity')
    ap.add_argument('--inherit',type=Path,help='Earlier release toolkit ZIP, SHA256SUMS.txt beside it, attesting unchanged units')
    ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    need(re.fullmatch(TAG,a.tag),'Invalid release tag');need(not a.output.exists(),'Output directory already exists')
    need(not git('status','--porcelain','--untracked-files=normal').strip(),'Commit source changes before packaging')
    commit=git('rev-parse','HEAD').decode().strip()
    need(set(git('ls-tree','-r','--name-only','HEAD').decode().splitlines())==set(SOURCE_FILES),'Tracked source differs from release allowlist')
    subprocess.run([sys.executable,'-B',str(ROOT/'tools/check_repository.py'),'--all'],cwd=ROOT,check=True)
    files={n:git('show','HEAD:'+n) for n in SOURCE_FILES}
    need(all(raw==(ROOT/n).read_bytes() for n,raw in files.items()),'Working tree differs from committed source')
    units=json.loads(files['resources/compiled.json'])['units']
    kept,inherit=inherited(a.inherit,units) if a.inherit else ({},None)
    identities={compiler_identity(c):c for c in reversed(a.compiler)}
    for name,profile in units.items():
        if name in kept:
            files[profile['resource']]=kept[name];continue
        need(profile['compiler'] in identities,f"No supplied compiler is {profile['compiler']}, the reviewed compiler of {name}; supply it or --inherit a toolkit that attests the unchanged unit")
        code,detail=compile_unit(name,identities[profile['compiler']])
        need(detail==profile,'Compiled unit differs from reviewed bytes/symbols/compiler/source: '+name)
        files[profile['resource']]=code
    record=dict(schema=2,tag=a.tag,source_commit=commit,compiled_units=units,files={n:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for n,raw in sorted(files.items())})
    if inherit:record['inherited_units']=inherit
    files[MANIFEST]=(json.dumps(record,indent=2)+'\n').encode()
    a.output.mkdir(parents=True,exist_ok=False);prefix='NikonFirmwarePatch-'+a.tag
    target=a.output/(prefix+'-toolkit.zip')
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,raw in sorted(files.items()):
            e=zipfile.ZipInfo(prefix+'/'+name,date_time=(2026,1,1,0,0,0));e.create_system=3;e.external_attr=0o100644<<16;e.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(e,raw)
    verified=check_release(target,commit)
    with (a.output/'SHA256SUMS.txt').open('x') as f:f.write(verified['archive_sha256']+'  '+target.name+'\n')
    print(json.dumps(verified,indent=2))
if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError) as e:raise SystemExit('ERROR: '+str(e))
