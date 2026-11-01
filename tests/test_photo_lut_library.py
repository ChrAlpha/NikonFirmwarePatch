"""Portable photo LUT library: identity, editing, export, malformed input and CLI contracts."""
import ctypes as C
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from z8photolut import library as lib
from z8photolut.cube import parse, runtime_cube

ROOT = Path(__file__).resolve().parents[1]


def cube(title, transform=lambda r, g, b: (r, g, b), size=3, comments=(), extra=''):
    rows = [*comments, f'TITLE "{title}"', f'LUT_3D_SIZE {size}']
    if extra:
        rows.append(extra)
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rows.append(' '.join(f'{v:.6f}' for v in transform(r/(size-1), g/(size-1), b/(size-1))))
    return ('\n'.join(rows)+'\n').encode()


WARM = cube('Warm', lambda r, g, b: (min(1, r*1.1), g, b*0.9))
COOL = cube('Cool', lambda r, g, b: (r*0.9, g, min(1, b*1.1)))
SWAP = cube('Swap', lambda r, g, b: (g, b, r))
IDENTITY = cube('Identity')


def look(raw, name=None, grid=17, source='look.cube', **options):
    return lib.look_from_cube(raw, source_name=source, name=name, grid=grid, **options)


class LibraryIdentityTests(unittest.TestCase):
    def test_rename_and_reorder_keep_transform_identity_and_change_library_identity(self):
        library = lib.Library((look(WARM), look(COOL), look(SWAP)))
        ids = [l.id for l in library.looks]
        renamed = library.rename('Warm', '暖色 "Winter" \\ 冬')
        moved = renamed.move('Swap', 1)
        self.assertEqual([l.id for l in moved.looks], [ids[2], ids[0], ids[1]])
        self.assertEqual(moved.looks[1].name, '暖色 "Winter" \\ 冬')
        self.assertEqual(len({library.id, renamed.id, moved.id}), 3)
        # The identity is the applied transform: same samples, same id, whatever the source text.
        reformatted = WARM.replace(b'TITLE "Warm"', b'# saved by another tool\nTITLE "Other"')
        self.assertEqual(look(reformatted, 'Other').id, ids[0])
        self.assertNotEqual(look(WARM, grid=33).id, ids[0])

    def test_default_follows_its_look_and_removed_default_becomes_off(self):
        library = lib.Library((look(WARM), look(COOL))).with_default('Cool')
        self.assertEqual(library.move('Cool', 1).default, 1)
        self.assertEqual(library.remove('Cool').default, 0)
        self.assertEqual(library.with_default('off').default, 0)

    def test_empty_library_round_trips_and_clears_every_look(self):
        library = lib.Library((look(WARM),)).remove(1)
        self.assertEqual(library.looks, ())
        raw = library.encode()
        self.assertEqual(lib.decode(raw).looks, ())
        self.assertEqual(lib.validate_runtime(raw)['count'], 0)
        self.assertEqual(library.add(look(COOL)).looks[0].name, 'Cool')

    def test_no_op_flag_is_exact_sample_identity(self):
        self.assertTrue(look(IDENTITY).identity)
        self.assertEqual(look(IDENTITY).samples, lib.identity_samples(17))
        self.assertFalse(look(WARM).identity)

    def test_names_keep_utf8_and_reject_only_unusable_text(self):
        for name in ['冬季 Winter', 'Winter "A" \\ test', 'é'*31, 'x'*63, '😀 look', 'a b']:
            with self.subTest(name=name):
                self.assertEqual(lib.check_name(name), name)
        for name in ['', ' lead', 'trail ', 'x'*64, 'é'*32, 'tab\tname', 'bell\x07', 'del\x7f', 'c1\x85',
                     'bad￾', 'bad￿', 'lone\ud800']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                lib.check_name(name)
        library = lib.Library((look(WARM),))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            library.add(look(COOL, 'WARM'))
        with self.assertRaisesRegex(ValueError, 'same transform'):
            library.add(look(WARM, 'Warm again'))


class ExportTests(unittest.TestCase):
    def test_source_export_is_exact_and_runtime_export_is_the_applied_grid(self):
        for name in ['Winter "A" \\ test', '冬季', '#not a comment', 'x'*63]:
            with self.subTest(name=name):
                item = look(SWAP, name)
                exported = runtime_cube(item.name, item.grid, item.samples, ('runtime grid',))
                parsed = parse(exported)
                self.assertEqual(parsed.title, name)
                again = look(exported, grid=item.grid)
                self.assertEqual((again.samples, again.id), (item.samples, item.id))
        library = lib.decode(lib.Library((look(SWAP, source='swap.cube'),)).encode())
        self.assertEqual(library.looks[0].source, SWAP)


class InputBasisTests(unittest.TestCase):
    def test_vendor_log_and_hdr_inputs_need_an_explicit_acknowledgement(self):
        cases = {
            'lumix.cube': cube('Classic', comments=('#LUMIXPHOTOSTYLE STD',)),
            'missing.cube': cube('Classic', comments=('#LUMIXPHOTOSTYLE',)),
            'vlog.cube': cube('V-Log to Rec709'),
            'nlog.cube': cube('Look', comments=('# for N-Log footage',)),
            'hlg.cube': cube('HLG grade'),
            'slog3.cube': cube('SLog3 film'),
            'domain.cube': cube('Shaper', extra='DOMAIN_MAX 1.5 1.5 1.5'),
        }
        for source, raw in cases.items():
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, '--accept-input-basis'):
                    look(raw, source=source)
                accepted = look(raw, source=source, accept_input_basis=True)
                self.assertTrue(accepted.basis_override)
                self.assertEqual(lib.decode(lib.Library((accepted,)).encode()).looks[0].basis_override,
                                 accepted.basis_override)
        with self.assertRaisesRegex(ValueError, 'vlt'):
            look(WARM, source='film.vlt')
        # Ordinary words are not mistaken for log encodings.
        for title in ['Analog Film', 'Dialog', 'Blog Look', 'Golden Hour', 'Catalog 2020s']:
            with self.subTest(title=title):
                self.assertFalse(look(cube(title)).basis_override)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.raw = lib.Library((look(WARM, source='warm.cube'), look(COOL, source='cool.cube')), 2).encode()
        self.size = lib.validate_runtime(self.raw)['bytes']

    def with_index(self, mutate):
        section = self.raw[self.size:-32]
        length = struct.unpack_from('<I', section, 12)[0]
        index = json.loads(section[16:16+length])
        mutate(index)
        encoded = json.dumps(index).encode()
        section = section[:12]+struct.pack('<I', len(encoded))+encoded+section[16+length:]
        import hashlib
        return self.raw[:self.size]+section+hashlib.sha256(section).digest()

    def test_malformed_files_fail_with_value_errors(self):
        corrupt = [b'', self.raw[:100], self.raw[:self.size], self.raw[:-1], self.raw+b'x',
                   self.raw[:self.size+20]+bytes([self.raw[self.size+20]^1])+self.raw[self.size+21:]]
        mutations = [
            lambda i: i.clear(), lambda i: i.update(looks={}), lambda i: i['looks'].pop(),
            lambda i: i['looks'][0].pop('source_bytes'), lambda i: i['looks'][0].update(extra=1),
            lambda i: i['looks'][0].update(source_bytes='12'), lambda i: i['looks'][0].update(source_bytes=-1),
            lambda i: i['looks'][0].update(source_bytes=10**9), lambda i: i['looks'][0].update(domain_min=None),
            lambda i: i['looks'][0].update(source_name='../x.cube'), lambda i: i['looks'][0].update(title=7),
            lambda i: i['looks'][0].update(input_basis='scene-linear'), lambda i: i['looks'].__setitem__(0, []),
        ]
        corrupt += [self.with_index(m) for m in mutations]
        for i, raw in enumerate(corrupt):
            with self.subTest(case=i), self.assertRaises(ValueError):
                lib.decode(raw)

    def test_runtime_changes_are_caught_without_rederiving_sources(self):
        raw = bytearray(self.raw)
        at = struct.unpack_from('<I', raw, 64)[0]+7
        raw[at] ^= 1
        struct.pack_into('<I', raw, 24, zlib.crc32(bytes(raw[64:self.size])))
        with self.assertRaisesRegex(ValueError, 'identity'):
            lib.decode(bytes(raw))


class WriterTests(unittest.TestCase):
    def test_writer_never_uses_hard_links_and_keeps_previous_file_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'Z8LUTLIB.BIN'
            with mock.patch.object(os, 'link', side_effect=OSError('FAT has no hard links')):
                lib.write(path, lib.Library((look(WARM),)), mode='create')
                with self.assertRaisesRegex(ValueError, 'exists'):
                    lib.write(path, lib.Library(()), mode='create')
                before = path.read_bytes()
                with mock.patch.object(os, 'replace', side_effect=OSError('card removed')):
                    with self.assertRaises(OSError):
                        lib.write(path, lib.Library((look(COOL),)), mode='replace')
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['Z8LUTLIB.BIN'])
                lib.write(path, lib.Library((look(COOL),)), mode='install')
                self.assertEqual(lib.read(path).looks[0].name, 'Cool')

    def test_failed_create_leaves_no_placeholder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'new.bin'
            with mock.patch.object(os, 'replace', side_effect=OSError('full')):
                with self.assertRaises(OSError):
                    lib.write(path, lib.Library(()), mode='create')
            self.assertEqual(list(Path(folder).iterdir()), [])


class NativeParityTests(unittest.TestCase):
    """The camera validator (compiled for the host) accepts exactly what the host writes."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='photo-lut-parity-')
        so = Path(cls.tmp.name)/'lut.so'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(ROOT/'z8photolut/src/lut.c'), '-o', str(so)], check=True)
        cls.native = C.CDLL(str(so))
        cls.native.z8_lut_validate.argtypes = [C.c_void_p, C.c_uint32]
        cls.native.z8_lut_validate_image.argtypes = [C.c_void_p, C.c_uint32]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def validate(self, raw):
        return self.native.z8_lut_validate(C.create_string_buffer(bytes(raw)), len(raw))

    def resealed(self, raw, mutate):
        """Apply a change, then make every CRC and the library digest consistent again."""
        raw = bytearray(raw[:lib.validate_runtime(raw)['bytes']])
        mutate(raw)
        count = struct.unpack_from('<I', raw, 16)[0]
        for i in range(count):
            at, size = struct.unpack_from('<2I', raw, 64+i*128)
            struct.pack_into('<I', raw, 64+i*128+16, zlib.crc32(raw[at:at+size]))
        struct.pack_into('<I', raw, 24, zlib.crc32(bytes(raw[64:])))
        import hashlib
        default = struct.unpack_from('<I', raw, 20)[0]
        raw[32:64] = hashlib.sha256(b'z8photolut/library/1\0'+struct.pack('<I', default)+bytes(raw[64:])).digest()
        return bytes(raw)

    def test_host_libraries_validate_natively(self):
        for looks in [(), (look(IDENTITY),), (look(WARM), look(COOL, grid=33), look(SWAP, '冬季 "A"', grid=65))]:
            raw = lib.Library(looks).runtime()
            with self.subTest(count=len(looks)):
                self.assertEqual(self.validate(raw), 0)

    def test_native_rejects_what_the_host_rejects(self):
        raw = lib.Library((look(WARM), look(IDENTITY))).runtime()
        first, identity = (struct.unpack_from('<I', raw, 64+i*128)[0] for i in (0, 1))
        cases = {
            'library digest': lambda r: None,                       # digest recomputed, then broken below
            'transform id': lambda r: r.__setitem__(slice(64+88, 64+89), bytes([r[64+88] ^ 1])),
            'samples under same id': lambda r: r.__setitem__(slice(first, first+1), bytes([r[first] ^ 1])),
            'no-op flag cleared': lambda r: struct.pack_into('<I', r, 64+128+12, 0),
            'no-op flag set': lambda r: struct.pack_into('<I', r, 64+12, 1),
            'duplicate name': lambda r: r.__setitem__(slice(64+128+24, 64+128+88), r[64+24:64+88]),
            'control in name': lambda r: r.__setitem__(slice(64+24, 64+25), b'\x07'),
            'invalid utf-8 name': lambda r: r.__setitem__(slice(64+24, 64+26), b'\xc0\xaf'),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                bad = self.resealed(raw, mutate)
                if label == 'library digest':
                    bad = bad[:40]+bytes([bad[40] ^ 1])+bad[41:]
                with self.assertRaises(ValueError):
                    lib.validate_runtime(bad)
                self.assertNotEqual(self.validate(bad), 0)
                # Boot trusts the package-verified image only for digests.
                image = self.native.z8_lut_validate_image(C.create_string_buffer(bad), len(bad))
                self.assertEqual(image == 0, label in ('library digest', 'transform id', 'samples under same id'))
        self.assertEqual(self.validate(self.resealed(raw, lambda r: None)), 0)
        self.assertNotEqual(identity, 0)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *arguments, ok=True):
        result = subprocess.run([sys.executable, str(ROOT/'photo_lut.py'), *map(str, arguments)],
                                capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertTrue(result.stderr.startswith('ERROR: '), result.stderr)
        return result

    def test_empty_create_name_recovery_management_export_and_card_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library, card = root/'looks.bin', root/'card'
            card.mkdir()
            (root/'long.cube').write_bytes(cube('A title far longer than sixty-three bytes of UTF-8 text, '
                                                'so it cannot be a camera name'))
            (root/'warm.cube').write_bytes(WARM)
            (root/'vlog.cube').write_bytes(cube('V-Log 709'))
            failed = self.run_cli('library', 'create', library, '--lut', root/'long.cube', ok=False)
            self.assertIn('library add', failed.stderr)
            self.assertFalse(library.exists())
            self.run_cli('library', 'create', library)
            self.run_cli('library', 'add', library, root/'long.cube', '--name', '长标题 Long')
            self.run_cli('library', 'add', library, root/'warm.cube', '--position', '1')
            before = library.read_bytes()
            self.assertIn('--accept-input-basis', self.run_cli('library', 'add', library, root/'vlog.cube', ok=False).stderr)
            self.assertEqual(library.read_bytes(), before)
            listing = json.loads(self.run_cli('library', 'list', library, '--json').stdout)
            self.assertEqual([l['name'] for l in listing['looks']], ['Warm', '长标题 Long'])
            identifier = listing['looks'][1]['id']
            self.run_cli('library', 'rename', library, identifier[:12], 'Long "冬"')
            self.run_cli('library', 'move', library, 'Long "冬"', '1')
            self.run_cli('library', 'default', library, '2')
            self.run_cli('library', 'verify', library)
            self.run_cli('library', 'export', library, 'Warm', root/'warm-source.cube', '--source')
            self.assertEqual((root/'warm-source.cube').read_bytes(), WARM)
            self.run_cli('library', 'export', library, 1, root/'runtime.cube', '--runtime')
            exported = parse((root/'runtime.cube').read_bytes())
            self.assertEqual(exported.title, 'Long "冬"')
            self.assertTrue(any('resampled' in c for c in exported.comments))
            self.run_cli('library', 'export', library, 1, root/'runtime.cube', '--runtime', ok=False)
            self.run_cli('library', 'card', library, card)
            self.assertEqual((card/'Z8LUTLIB.BIN').read_bytes(), library.read_bytes())
            self.run_cli('library', 'remove', library, 'Warm')
            self.run_cli('library', 'remove', library, 1)
            self.assertEqual(json.loads(self.run_cli('library', 'list', library, '--json').stdout)['looks'], [])
            self.run_cli('library', 'card', library, card)
            self.assertEqual(lib.read(card/'Z8LUTLIB.BIN').looks, ())


if __name__ == '__main__':
    unittest.main()
