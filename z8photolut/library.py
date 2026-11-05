"""Portable photo LUT library: the runtime bundle the camera applies plus exact sources.

One file serves the host and the camera. Its prefix is the Z8LUT004 runtime
bundle that firmware validates and applies; the camera reads only that prefix.
A host-only source section follows with each look's original .cube bytes and,
for Log looks, the compiler parameters and fit report, so an exported source is
byte-identical and every runtime grid can be re-derived or re-measured.

A look declares its input basis (z8photolut/basis.py) and so its rendering:
`cpu-3d` (nikon-sdr: the camera applies the full grid to its rendered JPEG) or
`isp+residual` (a Log basis: the still develop runs the native Log chain with
the look's fitted ISP tables, then the camera applies a near-identity residual
grid). Look identity is the SHA-256 of the transform the camera applies
(semantics, basis, ISP tables, grid and samples), so it survives renaming and
reordering; nikon-sdr identities equal their Z8LUT003 identities. Library
identity covers the complete runtime bundle: order, names and default.

Z8LUT003 libraries (nikon-sdr only) are read and rewritten as Z8LUT004.

Runtime bundle (little-endian; firmware lut.h mirrors every offset):
  header  64 B   magic Z8LUT004, u32 version 4, bytes, count, default, crc32 of
                 bytes 64.., reserved 0, library id sha256
  entries 8x128  u32 offset, bytes, grid, identity, crc32; u16 rendering, basis;
                 name[64] UTF-8 NUL-padded; id[32]; u32 isp_bytes, reserved 0
  looks          64-aligned spans: [ISP block (isp+residual only)] grid samples
ISP block (ISP_BYTES), each table in its firmware format, zero padding between:
  0x000 RGB curves u16[3][229], 1/32 10-bit code, non-decreasing, <= 32736
  0x560 NLG u16[229] = (green + 4) >> 3
  0x730 M2 s16[9], 1/512 (curve codes -> internal Y'UV)
  0x750 M1 base s16[9], Q10 (composed natively with the body matrix and gain)
  0x770 Cpxs s16[32][6] Pa..Pf, Q8, firmware ring-slot order
Grid samples: uint16 RGB, blue fastest, applied by tetrahedral interpolation to
8-bit RGB from full-range BT.601 Y'CbCr.
"""
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib

from .basis import BY_CODE, BY_KEY, NIKON_SDR, parse as parse_basis
from .cube import GRID_SIZES, basis_problem, encode_samples, foreign_basis, parse

MAGIC, VERSION = b'Z8LUT004', 4
LEGACY_MAGIC, LEGACY_VERSION = b'Z8LUT003', 3
HEADER, ENTRY, MAX_LOOKS, MAX_BYTES = 64, 128, 8, 0x200000
HEADER_FORMAT, ENTRY_FORMAT, LEGACY_ENTRY_FORMAT = '<8s6I32s', '<5I2H64s32s2I', '<6I64s32s8s'
NAME_BYTES = 63
SOURCE_MAGIC, SOURCE_VERSION, LEGACY_SOURCE_VERSION = b'Z8LUTSRC', 2, 1
CARD_FILENAME = 'Z8LUTLIB.BIN'
RENDERINGS = ('cpu-3d', 'isp+residual')
# ISP block: (name, offset, count, struct code); the block is ISP_BYTES with zero padding.
ISP_BYTES, CURVE_NODES, CURVE_LIMIT = 0x900, 229, 32736
ISP_TABLES = (('rgb', 0x000, 3*229, 'H'), ('nlg', 0x560, 229, 'H'), ('m2', 0x730, 9, 'h'),
              ('m1', 0x750, 9, 'h'), ('cpxs', 0x770, 32*6, 'h'))
# Exactly what a look identity covers besides its tables and samples. Changing
# any of these rules must change these strings and therefore every identity.
SEMANTICS = ('input=nikon-rendered-sdr 8-bit full-range BT.601 YCbCr;'
             'model=RGB tetrahedral uint16 grid;output=8-bit clipped;strength=1')
ISP_SEMANTICS = ('input=camera scene-linear through the native Log still develop;'
                 'isp=M1 base Q10,RGB 3x229 u16/32,NLG,M2 s16/512,Cpxs 32x6 Q8 slot order;'
                 'residual=RGB tetrahedral uint16 grid on 8-bit full-range BT.601 YCbCr;output=8-bit clipped;strength=1')


def look_id(grid, samples):
    """Identity of a cpu-3d (nikon-sdr) look; unchanged from Z8LUT003."""
    return hashlib.sha256(b'z8photolut/look/1\0'+SEMANTICS.encode()+b'\0'+struct.pack('<I', grid)+samples).digest()


def isp_look_id(basis, grid, isp, samples):
    """Identity of an isp+residual look: basis, ISP tables and residual grid."""
    return hashlib.sha256(b'z8photolut/look/2\0'+ISP_SEMANTICS.encode()+b'\0'+basis.encode()+b'\0'
                          + struct.pack('<I', grid)+isp+samples).digest()


def fit_id(isp):
    """Identity of a look's ISP tables alone (XMP FitSHA256)."""
    return hashlib.sha256(b'z8photolut/fit/1\0'+isp).digest()


def pack_isp(tables):
    """The ISP block from integer tables: rgb (3x229), nlg (229), m2 (9), m1 (9), cpxs (32x6, slot order)."""
    raw = bytearray(ISP_BYTES)
    for name, at, count, code in ISP_TABLES:
        values = [int(v) for row in tables[name] for v in (row if isinstance(row, (list, tuple)) else [row])]
        if len(values) != count:
            raise ValueError(f'ISP table {name} needs {count} values')
        struct.pack_into(f'<{count}{code}', raw, at, *values)
    unpack_isp(bytes(raw))
    return bytes(raw)


def unpack_isp(raw):
    """Check an ISP block exactly as the firmware does; return its tables as flat tuples."""
    if len(raw) != ISP_BYTES:
        raise ValueError('ISP block has the wrong size')
    tables, end = {}, 0
    for name, at, count, code in ISP_TABLES:
        if any(raw[end:at]):
            raise ValueError('ISP block padding is not zero')
        tables[name] = struct.unpack_from(f'<{count}{code}', raw, at)
        end = at+count*2
    if any(raw[end:]):
        raise ValueError('ISP block padding is not zero')
    rgb = tables['rgb']
    for c in range(3):
        curve = rgb[c*CURVE_NODES:(c+1)*CURVE_NODES]
        if max(curve) > CURVE_LIMIT or any(b < a for a, b in zip(curve, curve[1:])):
            raise ValueError('ISP RGB curve must be non-decreasing and at most 32736')
    if tables['nlg'] != tuple((g+4) >> 3 for g in rgb[CURVE_NODES:2*CURVE_NODES]):
        raise ValueError('ISP NLG table does not follow the green curve')
    return tables


FONT_FACTS = Path(__file__).resolve().parents[1]/'resources/photo-lut-fonts.json'
FONT_NAMES = {16: 'Latin', 2: 'Simplified Chinese', 0: 'Traditional Chinese', 13: 'Japanese', 22: 'Korean'}


def _font_ranges():
    facts = json.loads(FONT_FACTS.read_text())
    return [(int(font), facts['fonts'][str(font)]) for font in facts['label_fonts']]


def menu_font_note(name, position=1):
    """'' if the camera can draw this menu label, else a recoverable hint.

    The firmware draws a look label ("N. Name") in the UI language's menu font
    or, failing that, the first of the Latin, Simplified Chinese, Traditional
    Chinese, Japanese and Korean menu fonts that has every character. Nikon's
    fonts hold only the characters the camera's own menus use, and one label
    uses one font. The name itself is always kept."""
    label = f'{position}. {name}'
    needed = {ord(c) for c in label if ord(c) >= 0x20}
    best = None
    for font, spans in _font_ranges():
        missing = sorted(c for c in needed if not any(a <= c < b for a, b in spans))
        if not missing:
            return ''
        if best is None or len(missing) < len(best[1]):
            best = (font, missing)
    drawable = {c for font, spans in _font_ranges() for c in needed if any(a <= c < b for a, b in spans)}
    absent = sorted(needed-drawable)
    if absent:
        problem = 'the fallback menu fonts lack ' + ', '.join(chr(c) for c in absent)
    else:
        problem = (f'no single fallback menu font has all of its characters (closest: {FONT_NAMES[best[0]]}, '
                   f'missing ' + ', '.join(chr(c) for c in best[1]) + ')')
    return (f'menu label {name!r}: {problem}, so it may show boxes in the camera menu unless the UI language font '
            'draws it. The name is kept in the library, exports and photo metadata; rename the look for a '
            'readable menu label')


def identity_samples(grid):
    """The exact no-op grid (round half up), identical to the firmware check."""
    node = [(2*k*65535+grid-1)//(2*(grid-1)) for k in range(grid)]
    return b''.join(struct.pack('<3H', node[r], node[g], node[b])
                    for r in range(grid) for g in range(grid) for b in range(grid))


def check_name(name):
    """Display names: 1..63 UTF-8 bytes of XML 1.0 text without control characters.

    The same rule serves the bundle, the camera menu (UTF-16), runtime .cube
    TITLE export and XMP provenance, so a valid name is valid everywhere.
    """
    if not isinstance(name, str):
        raise ValueError('Look name must be text')
    try:
        size = len(name.encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise ValueError(f'Look name is not valid Unicode text: {name!r}') from exc
    if not 1 <= size <= NAME_BYTES:
        raise ValueError(f'Look name must be 1..{NAME_BYTES} UTF-8 bytes ({size} given): {name!r}')
    if any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or ord(c) in (0xfffe, 0xffff) for c in name):
        raise ValueError(f'Look name contains a control or non-XML character: {name!r}')
    if name != name.strip(' '):
        raise ValueError(f'Look name has leading or trailing spaces: {name!r}')
    return name


def _fit():
    """The Log look compiler, imported only when a Log look is compiled or deeply verified."""
    try:
        from . import fit
    except ImportError as exc:
        raise ValueError('Compiling or verifying a Log look needs numpy, scipy and colour-science: '
                         'pip install -r requirements-photo-lut-log.txt') from exc
    return fit


@dataclass(frozen=True)
class Look:
    name: str
    grid: int
    samples: bytes                      # cpu-3d: the full look; isp+residual: the residual grid
    identity: bool                      # the grid is the exact no-op grid
    source: bytes
    source_name: str
    title: str
    input_size: int
    domain_min: tuple
    domain_max: tuple
    basis_override: str = ''            # nikon-sdr only: acknowledged foreign-looking text
    basis: str = NIKON_SDR.key
    isp: bytes = b''                    # isp+residual only: the ISP block
    fit: dict = field(default=None, compare=False)   # isp+residual only: compiler parameters and report

    @property
    def rendering(self):
        return RENDERINGS[1] if self.isp else RENDERINGS[0]

    @property
    def id(self):
        return isp_look_id(self.basis, self.grid, self.isp, self.samples) if self.isp else look_id(self.grid, self.samples)

    def details(self):
        details = dict(name=self.name, id=self.id.hex(), grid=self.grid, no_op=self.identity and not self.isp,
                       input_basis=self.basis, rendering=self.rendering,
                       input_basis_override=self.basis_override or None,
                       source_name=self.source_name, source_title=self.title,
                       source_sha256=hashlib.sha256(self.source).hexdigest(), source_bytes=len(self.source),
                       input_size=self.input_size, domain_min=list(self.domain_min), domain_max=list(self.domain_max),
                       resampling='trilinear to native grid', runtime_interpolation='tetrahedral',
                       output_range='clipped SDR RGB 0..1', semantics=ISP_SEMANTICS if self.isp else SEMANTICS,
                       samples_sha256=hashlib.sha256(self.samples).hexdigest())
        if self.isp:
            details.update(resampling='residual by inversion of the quantised ISP model', residual_no_op=self.identity,
                           fit_id=fit_id(self.isp).hex(), fit=self.fit)
        return details


def look_from_cube(raw, *, source_name, name=None, grid=33, basis=NIKON_SDR.key, accept_input_basis=False,
                   fit_options=None):
    """Validate one .cube for the camera; failures name the fix.

    A nikon-sdr cube is resampled to the runtime grid. A cube for a declared Log
    basis is compiled (z8photolut/fit.py, needs numpy, scipy and colour-science)
    into ISP tables plus a residual on that grid; fit_options are passed to it."""
    if source_name.lower().endswith('.vlt'):
        raise ValueError(f'{source_name}: Panasonic VARICAM .vlt is not supported; export a 3D .cube')
    declared = parse_basis(basis)
    stem = Path(source_name).stem
    cube = parse(raw, stem)
    if declared.log:
        if accept_input_basis:
            raise ValueError('--accept-input-basis acknowledges a nikon-sdr cube; a Log cube declares --basis instead')
        problem = basis_problem(cube, source_name, declared)
        if problem:
            raise ValueError(f'{source_name}: {problem}, but it was declared for {declared.label}. Declare the '
                             'basis the cube was made for (photo_lut.py library add --basis)')
        reason = ''
    else:
        reason = foreign_basis(cube, source_name)
        if reason and not accept_input_basis:
            raise ValueError(f'{source_name}: {reason}. Without --basis a look acts on the camera-rendered SDR JPEG '
                             'values (after Picture Control, in the selected colour space). Declare the Log basis it '
                             'was made for with --basis (' + ', '.join(b.key for b in BY_KEY.values() if b.log) +
                             '), or, if it was made for camera-rendered SDR, add it again with --accept-input-basis')
    if name is None:
        name = cube.title
        try:
            check_name(name)
        except ValueError as exc:
            raise ValueError(f'{source_name}: title {name!r} is not a usable look name ({exc}); add it with '
                             f'`photo_lut.py library add LIBRARY {source_name} --name NAME`') from exc
    check_name(name)
    if grid not in GRID_SIZES:
        raise ValueError('Native grid must be 17, 33 or 65')
    if declared.log:
        compiled = _fit().compile_look(cube, declared, grid=grid, **(fit_options or {}))
        return Look(name, grid, compiled.samples, compiled.samples == identity_samples(grid), raw, Path(source_name).name,
                    cube.title, cube.size, cube.minimum, cube.maximum, '', declared.key, compiled.isp, compiled.fit)
    samples, _ = encode_samples(cube, grid)
    return Look(name, grid, samples, samples == identity_samples(grid), raw, Path(source_name).name, cube.title, cube.size,
                cube.minimum, cube.maximum, reason if accept_input_basis else '')


@dataclass(frozen=True)
class Library:
    looks: tuple
    default: int = 0
    source_section: dict = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        # An empty library is valid: loaded from a card it clears every look.
        if not 0 <= len(self.looks) <= MAX_LOOKS:
            raise ValueError(f'A library holds at most {MAX_LOOKS} looks')
        if not 0 <= self.default <= len(self.looks):
            raise ValueError('Default must be Off or an existing look')
        names = [check_name(l.name).casefold() for l in self.looks]
        if len(set(names)) != len(names):
            raise ValueError('Look names must be distinct (case-insensitive)')
        ids = [l.id for l in self.looks]
        if len(set(ids)) != len(ids):
            raise ValueError('The same transform is already in this library')

    # ---- runtime bundle -------------------------------------------------
    def runtime(self):
        return _runtime(self.looks, self.default)

    @property
    def id(self):
        return self.runtime()[32:64]

    # ---- whole file -----------------------------------------------------
    def encode(self):
        meta = [dict(source_name=l.source_name, source_sha256=hashlib.sha256(l.source).hexdigest(),
                     source_bytes=len(l.source), title=l.title, input_size=l.input_size,
                     domain_min=list(l.domain_min), domain_max=list(l.domain_max),
                     input_basis=l.basis, input_basis_override=l.basis_override, fit=l.fit) for l in self.looks]
        return _file(self.runtime(), SOURCE_VERSION, meta, self.looks)

    def details(self):
        runtime = self.runtime()
        return dict(format='Z8LUT004', library_id=self.id.hex(), runtime_bytes=len(runtime),
                    runtime_limit=MAX_BYTES, default=self.default,
                    default_name='Off' if not self.default else self.looks[self.default-1].name,
                    looks=[l.details() for l in self.looks])

    # ---- operations (each returns a new validated library) --------------
    def find(self, reference):
        reference = str(reference)
        if reference.isdigit() and 1 <= int(reference) <= len(self.looks):
            return int(reference)-1
        matches = [i for i, l in enumerate(self.looks) if l.name.casefold() == reference.casefold()]
        if not matches and len(reference) >= 8 and all(c in '0123456789abcdef' for c in reference.lower()):
            matches = [i for i, l in enumerate(self.looks) if l.id.hex().startswith(reference.lower())]
        if len(matches) != 1:
            raise ValueError(f'No unique look {reference!r}; use a position 1..{len(self.looks)}, a name or an id prefix')
        return matches[0]

    def _default_after(self, looks):
        if not self.default:
            return 0
        kept = self.looks[self.default-1].id
        return next((i+1 for i, l in enumerate(looks) if l.id == kept), 0)

    def add(self, look, position=None):
        looks = list(self.looks)
        if len(looks) >= MAX_LOOKS:
            raise ValueError(f'The library already holds {MAX_LOOKS} looks; remove one first')
        position = len(looks)+1 if position is None else position
        if not 1 <= position <= len(looks)+1:
            raise ValueError(f'Position must be 1..{len(looks)+1}')
        looks.insert(position-1, look)
        return Library(tuple(looks), self._default_after(looks))

    def rename(self, reference, name):
        i = self.find(reference)
        looks = list(self.looks)
        looks[i] = replace(looks[i], name=check_name(name))
        return Library(tuple(looks), self.default)

    def move(self, reference, position):
        i = self.find(reference)
        if not 1 <= position <= len(self.looks):
            raise ValueError(f'Position must be 1..{len(self.looks)}')
        looks = list(self.looks)
        looks.insert(position-1, looks.pop(i))
        return Library(tuple(looks), self._default_after(looks))

    def remove(self, reference):
        i = self.find(reference)
        looks = self.looks[:i]+self.looks[i+1:]
        return Library(looks, self._default_after(looks))

    def with_default(self, reference):
        return Library(self.looks, 0 if str(reference).casefold() == 'off' else self.find(reference)+1)


def _runtime(looks, default, legacy=False):
    """The camera bundle: Z8LUT004, or for a nikon-sdr-only check of an imported file, Z8LUT003."""
    payload = bytearray(HEADER+MAX_LOOKS*ENTRY)
    for i, look in enumerate(looks):
        offset = (len(payload)+63)&~63
        payload.extend(bytes(offset-len(payload)))
        span = look.isp+look.samples
        payload.extend(span)
        if len(payload) > MAX_BYTES:
            raise ValueError('Looks exceed the 2 MiB camera library; use a smaller grid or fewer looks')
        if legacy:
            struct.pack_into(LEGACY_ENTRY_FORMAT, payload, HEADER+i*ENTRY, offset, len(span), look.grid,
                             int(look.identity), zlib.crc32(span), 0, look.name.encode('utf-8'), look.id, bytes(8))
        else:
            struct.pack_into(ENTRY_FORMAT, payload, HEADER+i*ENTRY, offset, len(span), look.grid, int(look.identity),
                             zlib.crc32(span), RENDERINGS.index(look.rendering), BY_KEY[look.basis].code,
                             look.name.encode('utf-8'), look.id, len(look.isp), 0)
    body = bytes(payload[HEADER:])
    domain = b'z8photolut/library/1\0' if legacy else b'z8photolut/library/2\0'
    identifier = hashlib.sha256(domain+struct.pack('<I', default)+body).digest()
    struct.pack_into(HEADER_FORMAT, payload, 0, LEGACY_MAGIC if legacy else MAGIC, LEGACY_VERSION if legacy else VERSION,
                     len(payload), len(looks), default, zlib.crc32(body), 0, identifier)
    return bytes(payload)


def _file(runtime, version, meta, looks):
    index = json.dumps(dict(looks=meta), ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    section = SOURCE_MAGIC+struct.pack('<II', version, len(index))+index+b''.join(l.source for l in looks)
    return runtime+section+hashlib.sha256(section).digest()


def validate_runtime(raw):
    """Independent structural check of the bundle the firmware applies (Z8LUT004, or an importable Z8LUT003)."""
    if len(raw) < HEADER+MAX_LOOKS*ENTRY:
        raise ValueError('Truncated LUT library header')
    magic, version, size, count, default, checksum, reserved, identifier = struct.unpack_from(HEADER_FORMAT, raw)
    legacy = (magic, version) == (LEGACY_MAGIC, LEGACY_VERSION)
    if (magic, version) != (MAGIC, VERSION) and not legacy:
        raise ValueError('Not a Z8LUT004 (or importable Z8LUT003) photo LUT library')
    if size > len(raw) or size > MAX_BYTES or size < HEADER+MAX_LOOKS*ENTRY:
        raise ValueError('Invalid or truncated LUT library size')
    raw = raw[:size]
    if (count > MAX_LOOKS or default > count or reserved or zlib.crc32(raw[HEADER:]) != checksum):
        raise ValueError('Invalid LUT library header or checksum')
    domain = b'z8photolut/library/1\0' if legacy else b'z8photolut/library/2\0'
    if identifier != hashlib.sha256(domain+struct.pack('<I', default)+raw[HEADER:]).digest():
        raise ValueError('LUT library identity does not match its contents')
    end, names, ids, entries = HEADER+MAX_LOOKS*ENTRY, set(), set(), []
    for i in range(MAX_LOOKS):
        entry = raw[HEADER+i*ENTRY:HEADER+(i+1)*ENTRY]
        if i >= count:
            if any(entry):
                raise ValueError('Nonzero unused look entry')
            continue
        if legacy:
            at, length, grid, identity, crc, flags, name, digest, tail = struct.unpack(LEGACY_ENTRY_FORMAT, entry)
            rendering, code, isp_bytes, extra = 0, 0, 0, flags or any(tail)
        else:
            at, length, grid, identity, crc, rendering, code, name, digest, isp_bytes, extra = struct.unpack(ENTRY_FORMAT, entry)
        basis = BY_CODE.get(code)
        if (grid not in GRID_SIZES or at != (end+63)&~63 or at+length > size or identity > 1 or extra
                or basis is None or rendering != int(basis.log) or isp_bytes != (ISP_BYTES if basis.log else 0)
                or length != isp_bytes+grid**3*6 or any(raw[end:at]) or zlib.crc32(raw[at:at+length]) != crc):
            raise ValueError(f'Invalid look {i+1} bounds, kind or checksum')
        text, separator, padding = name.partition(b'\0')
        if not separator or any(padding):
            raise ValueError(f'Invalid look {i+1} name')
        try:
            text = check_name(text.decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise ValueError(f'Look {i+1} name is not UTF-8') from exc
        isp, samples = raw[at:at+isp_bytes], raw[at+isp_bytes:at+length]
        if isp:
            try:
                unpack_isp(isp)
            except ValueError as exc:
                raise ValueError(f'Look {i+1}: {exc}') from exc
        expected = isp_look_id(basis.key, grid, isp, samples) if isp else look_id(grid, samples)
        if digest != expected or text.casefold() in names or digest in ids:
            raise ValueError(f'Look {i+1} identity is wrong or duplicated')
        if bool(identity) != (samples == identity_samples(grid)):
            raise ValueError(f'Look {i+1} no-op flag does not match its samples')
        names.add(text.casefold());ids.add(digest)
        entries.append(dict(name=text, grid=grid, samples=samples, identity=bool(identity), id=digest,
                            basis=basis.key, isp=isp))
        end = at+length
    if end != size:
        raise ValueError('Trailing data inside the LUT library bundle')
    return dict(version=version, count=count, default=default, bytes=size, id=identifier, looks=entries)


SOURCE_FIELDS = dict(source_name=str, source_sha256=str, source_bytes=int, title=str, input_size=int,
                     domain_min=list, domain_max=list, input_basis=str, input_basis_override=str, fit=(dict, type(None)))
LEGACY_INPUT_BASIS = 'nikon-rendered-sdr'
# Host record of a compiled Log look (z8photolut/fit.py writes exactly these).
FIT_FIELDS = dict(compiler=str, grey=float, smoothness=float, samples=int, iterations=int, seed=int, report=dict)
REPORT_FIELDS = dict(isp_de00=list, final_de00=list, slope=list, reachable=float, cpxs_min_det=float,
                     m1_condition=float, m2_condition=float, m1_target_q10=list)


def _fields(record, fields, what):
    if not isinstance(record, dict) or set(record) != set(fields):
        raise ValueError(f'{what} has unexpected fields')
    for key, kind in fields.items():
        if type(record[key]) not in (kind if isinstance(kind, tuple) else (kind,)):
            raise ValueError(f'{what}: {key} has the wrong type')


def source_index(raw, count, version=SOURCE_VERSION):
    """Check the host source index shape and types before anything is read from it."""
    try:
        index = json.loads(raw.decode('ascii'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('Library source index is not valid JSON') from exc
    if not isinstance(index, dict) or set(index) != {'looks'} or not isinstance(index['looks'], list):
        raise ValueError('Library source index has an unexpected structure')
    if len(index['looks']) != count:
        raise ValueError('Library source index does not match its looks')
    fields = SOURCE_FIELDS if version == SOURCE_VERSION else {k: v for k, v in SOURCE_FIELDS.items() if k != 'fit'}
    for number, info in enumerate(index['looks'], 1):
        _fields(info, fields, f'Library source record {number}')
        if version == SOURCE_VERSION:
            known = info['input_basis'] in BY_KEY
            log = known and BY_KEY[info['input_basis']].log
            if log:
                _fields(info['fit'], FIT_FIELDS, f'Library source record {number} fit')
                _fields(info['fit']['report'], REPORT_FIELDS, f'Library source record {number} fit report')
            if not known or (info['fit'] is not None) != log or (log and info['input_basis_override']):
                raise ValueError(f'Library source record {number} has an invalid basis or fit')
        elif info['input_basis'] != LEGACY_INPUT_BASIS:
            raise ValueError(f'Library source record {number} is invalid')
        if (info['source_bytes'] < 0 or not info['source_name'] or len(info['source_sha256']) != 64
                or Path(info['source_name']).name != info['source_name']):
            raise ValueError(f'Library source record {number} is invalid')
    return index['looks']


def decode(raw, *, deep=False):
    """Read a complete library file (Z8LUT004, or Z8LUT003 imported as nikon-sdr looks).

    Always checked: runtime structure, CRCs, every applied transform's
    identity, the library identity, the source index and every source hash,
    and canonical encoding. `deep` also re-derives each nikon-sdr runtime grid
    from its exact source cube and re-measures each Log look's fit report from
    its stored tables against its source (the slow, deliberate `library verify`)."""
    runtime = validate_runtime(raw)
    size, legacy = runtime['bytes'], runtime['version'] == LEGACY_VERSION
    section, digest = raw[size:-32], raw[-32:]
    if len(raw) < size+48 or not section.startswith(SOURCE_MAGIC) or hashlib.sha256(section).digest() != digest:
        raise ValueError('Library source section is missing or damaged (a camera-only bundle cannot be edited)')
    version, length = struct.unpack_from('<II', section, 8)
    if version != (LEGACY_SOURCE_VERSION if legacy else SOURCE_VERSION) or 16+length > len(section):
        raise ValueError('Unsupported library source section')
    meta = source_index(section[16:16+length], runtime['count'], version)
    at, looks = 16+length, []
    for entry, info in zip(runtime['looks'], meta):
        if at+info['source_bytes'] > len(section):
            raise ValueError(f'Source of {entry["name"]!r} is truncated')
        source = section[at:at+info['source_bytes']]
        at += info['source_bytes']
        if hashlib.sha256(source).hexdigest() != info['source_sha256']:
            raise ValueError(f'Source of {entry["name"]!r} is damaged')
        if not legacy and info['input_basis'] != entry['basis']:
            raise ValueError(f'Source record of {entry["name"]!r} declares another basis')
        look = Look(entry['name'], entry['grid'], entry['samples'], entry['identity'], source,
                    info['source_name'], info['title'], info['input_size'], tuple(info['domain_min']),
                    tuple(info['domain_max']), info['input_basis_override'], entry['basis'], entry['isp'],
                    info.get('fit'))
        if deep and look.isp:
            _fit().verify_look(look)
        elif deep:
            again = look_from_cube(source, source_name=info['source_name'], name=entry['name'], grid=entry['grid'],
                                   accept_input_basis=bool(info['input_basis_override']))
            if again.samples != entry['samples'] or again.basis_override != info['input_basis_override']:
                raise ValueError(f'Runtime grid of {entry["name"]!r} does not match its source')
        looks.append(look)
    if at != len(section):
        raise ValueError('Trailing data in library source section')
    library = Library(tuple(looks), runtime['default'])
    if legacy:
        meta = [{k: v for k, v in m.items() if k != 'fit'} for m in meta]
        canonical = _file(_runtime(library.looks, library.default, legacy=True), LEGACY_SOURCE_VERSION, meta, library.looks)
    else:
        canonical = library.encode()
    if canonical != raw:
        raise ValueError('Library is not in canonical form')
    return library


def read(path, *, deep=False):
    path = Path(path)
    if path.stat().st_size > 512*1024*1024:
        raise ValueError(f'{path} is too large to be a photo LUT library')
    return decode(path.read_bytes(), deep=deep)


def write(path, library, *, mode='replace'):
    """Write a library so that the previous file survives any failure.

    mode: 'create' (the path must not exist), 'replace' (it must exist) or
    'install' (either; used for a card root). Only exclusive create and
    rename are used, which FAT and exFAT card filesystems provide."""
    if mode not in ('create', 'replace', 'install'):
        raise ValueError('Unknown write mode')
    path = Path(path)
    raw = library.encode()
    decode(raw)
    if mode == 'create' and path.exists():
        raise ValueError(f'{path} already exists')
    if mode == 'replace' and not path.is_file():
        raise ValueError(f'{path} does not exist; create it first')
    folder = path.resolve().parent
    handle, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=folder)
    reserved = False
    try:
        mask = os.umask(0);os.umask(mask)
        try:
            os.chmod(temporary, path.stat().st_mode & 0o777 if path.is_file() else 0o666 & ~mask)
        except OSError:
            pass  # FAT/exFAT have no POSIX modes.
        with os.fdopen(handle, 'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        if Path(temporary).read_bytes() != raw:
            raise OSError(f'Readback of {temporary} failed')
        if mode == 'create':
            # Reserve the name exclusively, then rename the complete file over
            # the reservation: never replaces a file someone else created.
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
            reserved = True
        os.replace(temporary, path)
        reserved = False
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        if reserved and path.exists() and not path.stat().st_size:
            path.unlink()
        raise
    _sync_directory(folder)
    if path.read_bytes() != raw:
        raise OSError(f'Readback of {path} failed')
    return raw


def _sync_directory(folder):
    if not hasattr(os, 'O_DIRECTORY'):
        return
    try:
        directory = os.open(folder, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass  # Some card filesystems do not sync directories; the data file is synced.
    finally:
        os.close(directory)
