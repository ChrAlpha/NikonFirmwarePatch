"""Portable photo LUT library: the runtime bundle the camera applies plus exact sources.

One file serves the host and the camera. Its prefix is the Z8LUT003 runtime
bundle that firmware validates and applies; the camera reads only that prefix.
A host-only source section follows with each look's original .cube bytes, so
an exported source is byte-identical and every runtime grid can be re-derived.

Look identity is the SHA-256 of the transform the camera applies (semantics
version, grid and quantized samples), so it survives renaming and reordering.
Library identity covers the complete runtime bundle: order, names and default.
"""
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib

from .cube import GRID_SIZES, INPUT_BASIS, encode_samples, foreign_basis, parse

MAGIC, VERSION = b'Z8LUT003', 3
HEADER, ENTRY, MAX_LOOKS, MAX_BYTES = 64, 128, 8, 0x200000
HEADER_FORMAT, ENTRY_FORMAT = '<8s6I32s', '<6I64s32s8s'
NAME_BYTES = 63
SOURCE_MAGIC, SOURCE_VERSION = b'Z8LUTSRC', 1
CARD_FILENAME = 'Z8LUTLIB.BIN'
# Exactly what a look identity covers besides its samples. Changing any of
# these rules must change this string and therefore every identity.
SEMANTICS = ('input=nikon-rendered-sdr 8-bit full-range BT.601 YCbCr;'
             'model=RGB tetrahedral uint16 grid;output=8-bit clipped;strength=1')


def look_id(grid, samples):
    return hashlib.sha256(b'z8photolut/look/1\0'+SEMANTICS.encode()+b'\0'+struct.pack('<I', grid)+samples).digest()


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


@dataclass(frozen=True)
class Look:
    name: str
    grid: int
    samples: bytes
    identity: bool
    source: bytes
    source_name: str
    title: str
    input_size: int
    domain_min: tuple
    domain_max: tuple
    basis_override: str = ''

    @property
    def id(self):
        return look_id(self.grid, self.samples)

    def details(self):
        return dict(name=self.name, id=self.id.hex(), grid=self.grid, no_op=self.identity,
                    input_basis=INPUT_BASIS, input_basis_override=self.basis_override or None,
                    source_name=self.source_name, source_title=self.title,
                    source_sha256=hashlib.sha256(self.source).hexdigest(), source_bytes=len(self.source),
                    input_size=self.input_size, domain_min=list(self.domain_min), domain_max=list(self.domain_max),
                    resampling='trilinear to native grid', runtime_interpolation='tetrahedral',
                    output_range='clipped SDR RGB 0..1', semantics=SEMANTICS,
                    samples_sha256=hashlib.sha256(self.samples).hexdigest())


def look_from_cube(raw, *, source_name, name=None, grid=33, accept_input_basis=False):
    """Validate one .cube for the camera; failures name the fix."""
    if source_name.lower().endswith('.vlt'):
        raise ValueError(f'{source_name}: Panasonic VARICAM .vlt is not supported; export a 3D .cube made for '
                         'Nikon-rendered SDR photos')
    stem = Path(source_name).stem
    cube = parse(raw, stem)
    reason = foreign_basis(cube, source_name)
    if reason and not accept_input_basis:
        raise ValueError(f'{source_name}: {reason}. The camera applies looks to its rendered SDR JPEG values '
                         '(after Picture Control, in the selected colour space). If this LUT was made for that '
                         'input, add it again with --accept-input-basis')
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
        payload = bytearray(HEADER+MAX_LOOKS*ENTRY)
        for i, look in enumerate(self.looks):
            offset = (len(payload)+63)&~63
            payload.extend(bytes(offset-len(payload)))
            payload.extend(look.samples)
            if len(payload) > MAX_BYTES:
                raise ValueError('Looks exceed the 2 MiB camera library; use a smaller grid or fewer looks')
            struct.pack_into(ENTRY_FORMAT, payload, HEADER+i*ENTRY, offset, len(look.samples), look.grid,
                             int(look.identity), zlib.crc32(look.samples), 0, look.name.encode('utf-8'),
                             look.id, bytes(8))
        body = bytes(payload[HEADER:])
        identifier = hashlib.sha256(b'z8photolut/library/1\0'+struct.pack('<I', self.default)+body).digest()
        struct.pack_into(HEADER_FORMAT, payload, 0, MAGIC, VERSION, len(payload), len(self.looks), self.default,
                         zlib.crc32(body), 0, identifier)
        return bytes(payload)

    @property
    def id(self):
        return self.runtime()[32:64]

    # ---- whole file -----------------------------------------------------
    def encode(self):
        runtime = self.runtime()
        meta = [dict(source_name=l.source_name, source_sha256=hashlib.sha256(l.source).hexdigest(),
                     source_bytes=len(l.source), title=l.title, input_size=l.input_size,
                     domain_min=list(l.domain_min), domain_max=list(l.domain_max),
                     input_basis=INPUT_BASIS, input_basis_override=l.basis_override) for l in self.looks]
        index = json.dumps(dict(looks=meta), ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
        section = SOURCE_MAGIC+struct.pack('<II', SOURCE_VERSION, len(index))+index+b''.join(l.source for l in self.looks)
        return runtime+section+hashlib.sha256(section).digest()

    def details(self):
        runtime = self.runtime()
        return dict(format='Z8LUT003', library_id=self.id.hex(), runtime_bytes=len(runtime),
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


def validate_runtime(raw):
    """Independent structural check of the bundle the firmware applies."""
    if len(raw) < HEADER+MAX_LOOKS*ENTRY:
        raise ValueError('Truncated LUT library header')
    magic, version, size, count, default, checksum, reserved, identifier = struct.unpack_from(HEADER_FORMAT, raw)
    if magic != MAGIC or version != VERSION:
        raise ValueError('Not a Z8LUT003 photo LUT library')
    if size > len(raw) or size > MAX_BYTES or size < HEADER+MAX_LOOKS*ENTRY:
        raise ValueError('Invalid or truncated LUT library size')
    raw = raw[:size]
    if (count > MAX_LOOKS or default > count or reserved or zlib.crc32(raw[HEADER:]) != checksum):
        raise ValueError('Invalid LUT library header or checksum')
    expected = hashlib.sha256(b'z8photolut/library/1\0'+struct.pack('<I', default)+raw[HEADER:]).digest()
    if identifier != expected:
        raise ValueError('LUT library identity does not match its contents')
    end, names, ids, entries = HEADER+MAX_LOOKS*ENTRY, set(), set(), []
    for i in range(MAX_LOOKS):
        entry = raw[HEADER+i*ENTRY:HEADER+(i+1)*ENTRY]
        if i >= count:
            if any(entry):
                raise ValueError('Nonzero unused look entry')
            continue
        at, length, grid, identity, crc, flags, name, digest, tail = struct.unpack(ENTRY_FORMAT, entry)
        if (grid not in GRID_SIZES or at != (end+63)&~63 or length != grid**3*6 or at+length > size
                or identity > 1 or flags or any(tail) or any(raw[end:at]) or zlib.crc32(raw[at:at+length]) != crc):
            raise ValueError(f'Invalid look {i+1} bounds or checksum')
        text, separator, padding = name.partition(b'\0')
        if not separator or any(padding):
            raise ValueError(f'Invalid look {i+1} name')
        try:
            text = check_name(text.decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise ValueError(f'Look {i+1} name is not UTF-8') from exc
        samples = raw[at:at+length]
        if digest != look_id(grid, samples) or text.casefold() in names or digest in ids:
            raise ValueError(f'Look {i+1} identity is wrong or duplicated')
        if bool(identity) != (samples == identity_samples(grid)):
            raise ValueError(f'Look {i+1} no-op flag does not match its samples')
        names.add(text.casefold());ids.add(digest)
        entries.append(dict(name=text, grid=grid, samples=samples, identity=bool(identity), id=digest))
        end = at+length
    if end != size:
        raise ValueError('Trailing data inside the LUT library bundle')
    return dict(count=count, default=default, bytes=size, id=identifier, looks=entries)


SOURCE_FIELDS = dict(source_name=str, source_sha256=str, source_bytes=int, title=str, input_size=int,
                     domain_min=list, domain_max=list, input_basis=str, input_basis_override=str)


def source_index(raw, count):
    """Check the host source index shape and types before anything is read from it."""
    try:
        index = json.loads(raw.decode('ascii'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('Library source index is not valid JSON') from exc
    if not isinstance(index, dict) or set(index) != {'looks'} or not isinstance(index['looks'], list):
        raise ValueError('Library source index has an unexpected structure')
    if len(index['looks']) != count:
        raise ValueError('Library source index does not match its looks')
    for number, info in enumerate(index['looks'], 1):
        if not isinstance(info, dict) or set(info) != set(SOURCE_FIELDS):
            raise ValueError(f'Library source record {number} has unexpected fields')
        for key, kind in SOURCE_FIELDS.items():
            if type(info[key]) is not kind:
                raise ValueError(f'Library source record {number}: {key} must be {kind.__name__}')
        if (info['source_bytes'] < 0 or info['input_basis'] != INPUT_BASIS or not info['source_name']
                or len(info['source_sha256']) != 64 or Path(info['source_name']).name != info['source_name']):
            raise ValueError(f'Library source record {number} is invalid')
    return index['looks']


def decode(raw, *, deep=False):
    """Read a complete library file.

    Always checked: runtime structure, CRCs, every applied transform's
    identity, the library identity, the source index and every source hash,
    and canonical encoding. `deep` also re-derives each runtime grid from its
    exact source cube (the slow, deliberate `library verify`)."""
    runtime = validate_runtime(raw)
    size = runtime['bytes']
    section, digest = raw[size:-32], raw[-32:]
    if len(raw) < size+48 or not section.startswith(SOURCE_MAGIC) or hashlib.sha256(section).digest() != digest:
        raise ValueError('Library source section is missing or damaged (a camera-only bundle cannot be edited)')
    version, length = struct.unpack_from('<II', section, 8)
    if version != SOURCE_VERSION or 16+length > len(section):
        raise ValueError('Unsupported library source section')
    meta = source_index(section[16:16+length], runtime['count'])
    at, looks = 16+length, []
    for entry, info in zip(runtime['looks'], meta):
        if at+info['source_bytes'] > len(section):
            raise ValueError(f'Source of {entry["name"]!r} is truncated')
        source = section[at:at+info['source_bytes']]
        at += info['source_bytes']
        if hashlib.sha256(source).hexdigest() != info['source_sha256']:
            raise ValueError(f'Source of {entry["name"]!r} is damaged')
        if deep:
            look = look_from_cube(source, source_name=info['source_name'], name=entry['name'], grid=entry['grid'],
                                  accept_input_basis=bool(info['input_basis_override']))
            if look.samples != entry['samples'] or look.basis_override != info['input_basis_override']:
                raise ValueError(f'Runtime grid of {entry["name"]!r} does not match its source')
        else:
            look = Look(entry['name'], entry['grid'], entry['samples'], entry['identity'], source,
                        info['source_name'], info['title'], info['input_size'], tuple(info['domain_min']),
                        tuple(info['domain_max']), info['input_basis_override'])
        looks.append(look)
    if at != len(section):
        raise ValueError('Trailing data in library source section')
    library = Library(tuple(looks), runtime['default'])
    if library.encode() != raw:
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
