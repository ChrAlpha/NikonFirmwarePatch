#!/usr/bin/env python3
"""Package exact committed photo LUT source and its reviewed native code, without Nikon firmware."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from z8photolut.patcher import compile_code
from z8prores.package import require,sha

SOURCES=(
    'LICENSE','NOTICE.md','requirements.txt','photo_lut.py','verify.py',
    'resources/input.json','resources/photo-lut.json',
    'z8photolut/__init__.py','z8photolut/cube.py','z8photolut/patcher.py',
    'z8photolut/src/firmware.c','z8photolut/src/firmware.h','z8photolut/src/hooks.S',
    'z8photolut/src/link.ld','z8photolut/src/lut.c','z8photolut/src/lut.h',
    'z8prores/__init__.py','z8prores/a64.py','z8prores/layout.py','z8prores/menu.py',
    'z8prores/package.py','z8prores/patcher.py',
)


def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--compiler',default='clang-19')
    p.add_argument('--tag',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,63}',a.tag),'Invalid toolkit tag')
    require(not a.output.exists(),'Output already exists')
    require(not git('status','--porcelain').strip(),'Commit source changes before packaging')
    commit=git('rev-parse','HEAD').decode().strip()
    files={name:git('show','HEAD:'+name) for name in SOURCES}
    require(all(raw==(ROOT/name).read_bytes() for name,raw in files.items()),'Working source differs from commit')
    code,symbols,native=compile_code(a.compiler)
    expected=json.loads(files['resources/photo-lut.json'])
    observed=dict(schema=1,native=native,symbols={k:hex(v) for k,v in symbols.items()})
    require(observed==expected,'Compiled code differs from the reviewed photo LUT profile')
    files['resources/photo-lut.bin']=code
    record=dict(schema=1,tag=a.tag,source_commit=commit,firmware_included=False,
                physical_camera_verified=False,files={name:sha(raw) for name,raw in sorted(files.items())})
    files['photo-lut-release.json']=(json.dumps(record,indent=2)+'\n').encode()
    prefix='NikonPhotoLUT-'+a.tag
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(a.output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,raw in sorted(files.items()):
            entry=zipfile.ZipInfo(prefix+'/'+name,date_time=(2026,1,1,0,0,0))
            entry.create_system=3;entry.external_attr=0o100644<<16;entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,raw)
    with zipfile.ZipFile(a.output) as archive:
        require(set(archive.namelist())=={prefix+'/'+n for n in files},'Archive member mismatch')
        require(all(archive.read(prefix+'/'+name)==raw for name,raw in files.items()),'Archive readback mismatch')
    print(json.dumps(dict(output=str(a.output),sha256=sha(a.output.read_bytes()),source_commit=commit,
                          source_files=len(SOURCES),firmware_included=False)))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError) as e:raise SystemExit('ERROR: '+str(e))
