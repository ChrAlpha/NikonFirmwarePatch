#!/usr/bin/env python3
"""Compile candidate patch units for maintainer review; never reads firmware.

Writes a new directory only. Review the generated compiled.json and rebuild the
combination recipes before installing any changed metadata in resources/. With --units, only the
named units are compiled; the other units keep their reviewed entries from resources/compiled.json.
"""
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from z8patch.compiled import SPECS,compile_unit


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--compiler',default='clang')
    ap.add_argument('--units',nargs='+',choices=list(SPECS),help='Compile only these units')
    a=ap.parse_args()
    if a.output.exists():raise ValueError('Output already exists')
    a.output.mkdir(parents=True)
    reviewed=json.loads((ROOT/'resources/compiled.json').read_text())['units'] if a.units else {}
    units={name:reviewed[name] for name in SPECS if a.units and name not in a.units}
    for name in a.units or SPECS:
        code,meta=compile_unit(name,a.compiler)
        (a.output/Path(meta['resource']).name).write_bytes(code)
        units[name]=meta
        print(name,len(code),meta['sha256'],flush=True)
    with (a.output/'compiled.json').open('x') as f:
        json.dump(dict(schema=1,units={name:units[name] for name in SPECS}),f,indent=2);f.write('\n')


if __name__=='__main__':main()
