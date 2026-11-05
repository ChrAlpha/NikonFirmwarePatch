#!/usr/bin/env python3
"""Manage portable Z8 photo LUT libraries (Z8LUTLIB.BIN) for the photo-lut feature group.

A library file holds up to eight looks. Its camera part is the runtime bundle
the firmware applies; its host part keeps each look's exact original .cube and,
for Log looks, the fit report. Copy it to a card root and import it once in the
camera; it is restored automatically after power cycles. Build firmware with
`patch.py <Z_8_0311.bin> --features photo-lut ...`; the firmware carries no looks.

Each look declares its input basis with --basis: nikon-sdr (default) for cubes
made for camera-rendered SDR, or the Log encoding and gamut the cube expects
(n-log/bt2020, s-log3/s-gamut3.cine, log3g10/redwidegamutrgb, f-log2/f-gamut-c,
arri-logc3/awg3, arri-logc4/awg4, v-log/v-gamut). Log looks are compiled into
the camera's native Log still-develop tables plus a residual grid; compiling
needs numpy, scipy and colour-science (pip install -r requirements-photo-lut-log.txt).
"""
import argparse
import json
import os
from pathlib import Path
import sys

from z8prores.package import require, sha
from z8photolut import library as lib
from z8photolut.basis import BASES, parse as parse_basis
from z8photolut.cube import runtime_cube

NOTES = ('nikon-sdr looks act on the photo the camera has already rendered: 8-bit full-range Y\'CbCr of the JPEG '
         'after the active Picture Control, in its selected colour space. Log looks render the still through the '
         'camera\'s Log develop with the look\'s fitted tables (JPEG and the NEF embedded previews), then the residual '
         'is applied to the JPEG. RAW (NEF) data is never changed. '
         'Camera menu: Photo shooting menu > Photo LUT > Off, Load LUTs from card, or a look. Import once: the library '
         'source and full transform identities for banks A-D are saved internally. Card libraries restore '
         'automatically after power cycles; keep Z8LUTLIB.BIN on the card. Off retains the imported library. '
         'An unavailable selected look blocks capture until it is restored or Off is selected. Saved selections use '
         'full transform identities, so renaming or reordering looks preserves them.')


def fit_options(a):
    return {k: v for k, v in dict(grey=a.grey, smoothness=a.smoothness).items() if v is not None}


def load_cube(path, a, name=None):
    if a.basis != 'nikon-sdr':
        print(f'Compiling {Path(path).name} for {a.basis} (about a minute)...', file=sys.stderr, flush=True)
    look = lib.look_from_cube(Path(path).read_bytes(), source_name=Path(path).name, name=name, grid=a.grid,
                              basis=a.basis, accept_input_basis=a.accept_input_basis, fit_options=fit_options(a))
    if look.fit:
        from z8photolut.fit import describe
        print('\n'.join([f'{look.name}:', *describe(look.fit)]), flush=True)
    return look


def label_notes(library):
    return [note for i, look in enumerate(library.looks, 1) if (note := lib.menu_font_note(look.name, i))]


def show(library, as_json):
    details = library.details()
    for i, look in enumerate(details['looks'], 1):
        look['menu_label_note'] = lib.menu_font_note(look['name'], i) or None
    if as_json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return
    print(f'Library {details["library_id"][:16]}  runtime {details["runtime_bytes"]}/{details["runtime_limit"]} bytes')
    for i, look in enumerate(details['looks'], 1):
        notes = [f'{look["input_basis"]} ({look["rendering"]})', f'grid {look["grid"]}', f'id {look["id"][:16]}',
                 f'source {look["source_name"]}']
        if look.get('fit'):
            r = look['fit']['report']
            notes.append('dE00 p50/p95 ISP {:.2f}/{:.2f}, final {:.2f}/{:.2f}'.format(*r['isp_de00'][:2], *r['final_de00'][:2]))
        if look['no_op']:
            notes.append('no-op')
        if look['input_basis_override']:
            notes.append('basis acknowledged: '+look['input_basis_override'])
        print(f'{i}. {look["name"]}  ('+'; '.join(notes)+')')
    for look in details['looks']:
        if look['menu_label_note']:
            print('NOTE: '+look['menu_label_note'], file=sys.stderr)


def write_new(path, raw):
    path = Path(path)
    with path.open('xb') as f:
        f.write(raw);f.flush();os.fsync(f.fileno())
    require(path.read_bytes() == raw, f'Readback of {path} failed')


def library_command(a):
    if a.action == 'create':
        library = lib.Library(tuple(load_cube(p, a) for p in a.lut or ()))
        lib.write(a.library, library, mode='create')
    elif a.action == 'verify':
        library = lib.read(a.library, deep=True)
        print(json.dumps(dict(status='valid', library_id=library.id.hex(), looks=len(library.looks),
                              runtime_bytes=len(library.runtime())), ensure_ascii=False))
        return
    elif a.action in ('list', 'show'):
        library = lib.read(a.library)
        if a.action == 'show':
            print(json.dumps(library.looks[library.find(a.look)].details(), ensure_ascii=False, indent=2))
        else:
            show(library, a.json)
        return
    elif a.action == 'export':
        library = lib.read(a.library)
        look = library.looks[library.find(a.look)]
        if a.source:
            raw = look.source
        else:
            if look.isp:
                raise ValueError(f'{look.name!r} is a Log look: its runtime grid is a residual after the camera\'s ISP '
                                 'tables, not a standalone transform; export --source instead')
            raw = runtime_cube(look.name, look.grid, look.samples, (
                f'Z8 photo LUT runtime grid (resampled; not the original source {look.source_name})',
                f'look id sha256 {look.id.hex()}', f'semantics {lib.SEMANTICS}'))
        write_new(a.output, raw)
        print(json.dumps(dict(output=str(a.output), kind='source' if a.source else 'runtime',
                              sha256=sha(raw), look_id=look.id.hex()), ensure_ascii=False))
        return
    elif a.action == 'card':
        library = lib.read(a.library)
        root = Path(a.card_root)
        require(root.is_dir(), f'{root} is not a mounted card root directory')
        target = root/lib.CARD_FILENAME
        lib.write(target, library, mode='install')
        print(json.dumps(dict(card_file=str(target), library_id=library.id.hex(), looks=len(library.looks),
                              menu_label_notes=label_notes(library),
                              next_step='In the camera: Photo LUT > Load LUTs from card'), ensure_ascii=False))
        return
    else:
        library = lib.read(a.library)
        if a.action == 'add':
            library = library.add(load_cube(a.cube, a, a.name), a.position)
        elif a.action == 'rename':
            library = library.rename(a.look, a.name)
        elif a.action == 'move':
            library = library.move(a.look, a.position)
        elif a.action == 'remove':
            library = library.remove(a.look)
        lib.write(a.library, library)
    show(lib.read(a.library), False)


def parser():
    p = argparse.ArgumentParser(description=__doc__, epilog=NOTES, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest='command', required=True)
    library = commands.add_parser('library', help='Create, inspect and edit a portable look library')
    actions = library.add_subparsers(dest='action', required=True)
    accept = dict(action='store_true', help='Accept a nikon-sdr cube whose name/comments suggest log, HDR or another '
                  'vendor base, confirming it was made for camera-rendered SDR')
    grid = dict(type=int, choices=lib.GRID_SIZES, default=33,
                help='Runtime grid: the full look, or a Log look\'s residual (default 33; 65 fits one Log look)')

    def compile_options(c):
        c.add_argument('--grid', **grid);c.add_argument('--accept-input-basis', **accept)
        c.add_argument('--basis', default='nikon-sdr', type=lambda t: parse_basis(t).key,
                       help='Input basis the cube expects: ' + ', '.join(b.key for b in BASES) + ' (default nikon-sdr)')
        c.add_argument('--grey', type=float, help='Log looks: curve units of scene 18%% grey (default 400, the stock '
                       'N-Log 0.18 point); raise to render brighter')
        c.add_argument('--smoothness', type=float, help='Log looks: Cpxs inter-node smoothness weight (default 3)')
    c = actions.add_parser('create', help='Create a new library, empty or from up to 8 .cube files')
    c.add_argument('library', type=Path);c.add_argument('--lut', action='append', type=Path,
                                                        help='Add a .cube named by its TITLE (use add --name to choose)')
    compile_options(c)
    c = actions.add_parser('add', help='Add a .cube as a new look')
    c.add_argument('library', type=Path);c.add_argument('cube', type=Path)
    c.add_argument('--name', help=f'Display name, 1..{lib.NAME_BYTES} UTF-8 bytes (default: cube TITLE or file name)')
    c.add_argument('--position', type=int, help='1-based position (default: last)')
    compile_options(c)
    for action, text in [('list', 'List looks in camera order'),
                         ('verify', 'Check every byte and re-derive every look from its source (slow)')]:
        c = actions.add_parser(action, help=text);c.add_argument('library', type=Path)
        if action == 'list':
            c.add_argument('--json', action='store_true')
    look = dict(help='Look position (1..8), name, or id prefix (8+ hex digits)')
    c = actions.add_parser('show', help='Show one look');c.add_argument('library', type=Path);c.add_argument('look', **look)
    c = actions.add_parser('rename', help='Rename a look (identity unchanged)')
    c.add_argument('library', type=Path);c.add_argument('look', **look);c.add_argument('name')
    c = actions.add_parser('move', help='Move a look to a position (identity unchanged)')
    c.add_argument('library', type=Path);c.add_argument('look', **look);c.add_argument('position', type=int)
    c = actions.add_parser('remove', help='Remove a look (an empty library clears the camera looks)');c.add_argument('library', type=Path);c.add_argument('look', **look)
    c = actions.add_parser('export', help='Write a look as a new .cube file')
    c.add_argument('library', type=Path);c.add_argument('look', **look);c.add_argument('output', type=Path)
    kind = c.add_mutually_exclusive_group(required=True)
    kind.add_argument('--source', action='store_true', help='Exact original .cube bytes')
    kind.add_argument('--runtime', action='store_true', help='The resampled grid the camera applies')
    c = actions.add_parser('card', help=f'Copy the library to a mounted card root as {lib.CARD_FILENAME}')
    c.add_argument('library', type=Path);c.add_argument('card_root', type=Path)
    library.set_defaults(run=library_command)
    return p


def main():
    a = parser().parse_args()
    a.run(a)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as e:
        raise SystemExit('ERROR: '+str(e))
