#!/usr/bin/env python3
"""Build a Z8 photo LUT firmware package from C3.11 and user-supplied .cube files."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from z8prores.package import Package, require, require_official_input, sha
from z8photolut.cube import parse, bundle, validate_bundle
from z8photolut.patcher import build


def main():
    p=argparse.ArgumentParser(description=__doc__,epilog='Camera menu: Photo shooting menu > Photo LUT > Off or a look. '
        'Selection belongs to the photo shooting bank. LUTs are compiled into this build. '
        'LUTs act on Nikon-rendered SDR image values; .cube does not identify scene-linear or log colorimetry. '
        'This development build requires physical-camera acceptance; QEMU timing is not camera performance.')
    p.add_argument('firmware',type=Path)
    p.add_argument('--lut',type=Path,action='append',required=True,help='3D .cube file; repeat for up to eight looks')
    p.add_argument('--grid',type=int,choices=(17,33,65),default=33)
    p.add_argument('--default-look',type=int,default=0,help='0: Off; 1..8: a supplied look')
    p.add_argument('--version',required=True,help='Distinct firmware identity NN.NN')
    p.add_argument('--tag',default='l0')
    p.add_argument('--compiler',default='clang-19')
    p.add_argument('--code-source',choices=('source','prebuilt'),default='source',
                   help='source requires Clang and AArch64 binutils; prebuilt requires the matching toolkit')
    p.add_argument('--output',type=Path,required=True,help='New output directory')
    a=p.parse_args()
    require(not a.output.exists(),'Output directory already exists')
    original=a.firmware.read_bytes()
    require_official_input(original)
    package=Package(original)
    looks=[parse(path.read_bytes(),path.stem) for path in a.lut]
    data,look_details=bundle(looks,grid=a.grid,default=a.default_look)
    validate_bundle(data)
    eg,details=build(package.eg,data,a.version,a.tag,a.compiler,a.code_source)
    encoded,checked=package.replace_eg(eg,'eg2070_'+a.version.replace('.','')+a.tag+'.bi')
    # Independent envelope read checks all module hashes/CRCs and native bytes.
    decoded=Package(encoded)
    require(decoded.eg==eg and decoded.modules==checked.modules,'Independent output readback failed')
    require(sha(a.firmware.read_bytes())==package.sha256,'Input changed during build')
    root=Path(__file__).resolve().parent
    release=root/'photo-lut-release.json'
    if release.is_file():
        provenance=json.loads(release.read_text())
        require(provenance['schema']==1,'Unsupported photo LUT toolkit manifest')
        for name,expected in provenance['files'].items():
            relative=Path(name)
            require(not relative.is_absolute() and '..' not in relative.parts,'Invalid toolkit member path')
            require(sha((root/relative).read_bytes())==expected,'Toolkit source or code changed: '+name)
        commit=provenance['source_commit'];dirty=False
    else:
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
        dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip())
    name='Z_8_'+a.version.replace('.','')+'.bin'
    manifest=dict(schema=1,status='DEVELOPMENT_BUILD',source_commit=commit,source_tree_clean=not dirty,
        input_sha256=package.sha256,output_filename=name,output_sha256=sha(encoded),
        main_payload_sha256=sha(eg[:-2]),main_payload_bytes=len(eg)-2,
        identity=f'{a.version}.{a.tag}',looks=look_details,default_look=a.default_look,
        details=details,modules=checked.modules)
    manifest['source_sha256']={path.relative_to(root).as_posix():sha(path.read_bytes())
        for path in [Path(__file__).resolve(),*sorted((root/'z8photolut').rglob('*'))]
        if path.is_file() and path.suffix in ('.py','.c','.h','.S','.ld')}
    a.output.mkdir(parents=True,exist_ok=False)
    for filename,raw in [(name,encoded),('main-payload.bi',eg[:-2]),('looks.bin',data)]:
        with (a.output/filename).open('xb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        require(sha((a.output/filename).read_bytes())==sha(raw),'Output file readback failed')
    with (a.output/'manifest.json').open('x') as f:
        json.dump(manifest,f,ensure_ascii=False,indent=2);f.write('\n')
    with (a.output/'SHA256SUMS.txt').open('x') as f:
        for filename in (name,'main-payload.bi','looks.bin','manifest.json'):
            f.write(sha((a.output/filename).read_bytes())+'  '+filename+'\n')
    print(json.dumps(dict(status=manifest['status'],output=str(a.output/name),sha256=manifest['output_sha256'],
                         main=str(a.output/'main-payload.bi'),looks=len(looks)),ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError as e:
        raise SystemExit('ERROR: '+str(e)+'\n'+str(e.stderr or ''))
    except (OSError,ValueError) as e:
        raise SystemExit('ERROR: '+str(e))
