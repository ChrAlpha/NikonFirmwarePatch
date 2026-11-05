#!/usr/bin/env python3
"""Manage portable Z8 photo LUT libraries (Z8LUTLIB.BIN) for the photo-lut feature group.

A library file holds up to eight looks. Its camera part is the runtime bundle
the firmware applies; its host part keeps each look's exact original .cube.
Copy it to a card root and import it once in the camera; it is restored
automatically after power cycles. Build firmware with
`patch.py <Z_8_0311.bin> --features photo-lut ...`; the firmware carries no looks.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from z8prores.package import require, sha
from z8photolut import library as lib
from z8photolut.cube import INPUT_BASIS, runtime_cube

NOTES = ('Looks act on the photo the camera has already rendered: 8-bit full-range Y\'CbCr of the JPEG after the '
         'active Picture Control, in its selected colour space. RAW (NEF) data is never changed. '
         'Camera menu: Photo shooting menu > Photo LUT > Off, Load LUTs from card, or a look. Import once: the library '
         'source and full transform identities for banks A-D are saved internally. Card libraries restore '
         'automatically after power cycles; keep Z8LUTLIB.BIN on the card. Off retains the imported library. '
         'An unavailable selected look blocks capture until it is restored or Off is selected. Saved selections use '
         'full transform identities, so renaming or reordering looks preserves them.')


def load_cubes(paths, grid, accept):
    return [lib.look_from_cube(Path(p).read_bytes(), source_name=Path(p).name, grid=grid,
                               accept_input_basis=accept) for p in paths]


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
    print(f'Input basis: {INPUT_BASIS} (camera-rendered SDR JPEG values)')
    for i, look in enumerate(details['looks'], 1):
        notes = [f'grid {look["grid"]}', f'id {look["id"][:16]}', f'source {look["source_name"]}']
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
        library = lib.Library(tuple(load_cubes(a.lut or (), a.grid, a.accept_input_basis)))
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
            look = lib.look_from_cube(Path(a.cube).read_bytes(), source_name=Path(a.cube).name, name=a.name,
                                      grid=a.grid, accept_input_basis=a.accept_input_basis)
            library = library.add(look, a.position)
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
    basis = dict(action='store_true', help='Accept a cube whose name/comments suggest log, HDR or another vendor '
                 'base, confirming it was made for camera-rendered SDR')
    grid = dict(type=int, choices=lib.GRID_SIZES, default=33, help='Runtime grid (default 33)')
    c = actions.add_parser('create', help='Create a new library, empty or from up to 8 .cube files')
    c.add_argument('library', type=Path);c.add_argument('--lut', action='append', type=Path,
                                                        help='Add a .cube named by its TITLE (use add --name to choose)')
    c.add_argument('--grid', **grid);c.add_argument('--accept-input-basis', **basis)
    c = actions.add_parser('add', help='Add a .cube as a new look')
    c.add_argument('library', type=Path);c.add_argument('cube', type=Path)
    c.add_argument('--name', help=f'Display name, 1..{lib.NAME_BYTES} UTF-8 bytes (default: cube TITLE or file name)')
    c.add_argument('--position', type=int, help='1-based position (default: last)')
    c.add_argument('--grid', **grid);c.add_argument('--accept-input-basis', **basis)
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
