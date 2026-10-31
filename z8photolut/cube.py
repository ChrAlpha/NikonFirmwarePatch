"""Strict IRIDAS 3D .cube ingestion and deterministic native bundle encoding.

Format reference: OpenColorIO src/OpenColorIO/fileformats/FileFormatIridasCube.cpp.
Cube rows are red-fastest; native samples are blue-fastest RGB uint16. Input
domains are baked into a normalized grid, and outputs clip to the SDR range.
"""
from dataclasses import dataclass
import hashlib
import math
import shlex
import struct
import zlib

MAGIC = b'Z8LUT002'
HEADER_BYTES, ENTRY_BYTES, MAX_LOOKS, MAX_BYTES = 64, 96, 8, 0x200000
GRID_SIZES = (17, 33, 65)


@dataclass(frozen=True)
class Cube:
    title: str
    size: int
    minimum: tuple
    maximum: tuple
    values: tuple
    source_sha256: str

    def sample(self, rgb):
        axes = []
        for c in range(3):
            t = min(1.0, max(0.0, (rgb[c]-self.minimum[c]) / (self.maximum[c]-self.minimum[c]))) * (self.size-1)
            low = min(self.size-2, int(t))
            axes.append((low, t-low))
        result = [0.0, 0.0, 0.0]
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    weight = math.prod(f if upper else 1-f for (_, f), upper in zip(axes, (r, g, b)))
                    at = ((axes[2][0]+b)*self.size + axes[1][0]+g)*self.size + axes[0][0]+r
                    for c, value in enumerate(self.values[at]):
                        result[c] += weight*value
        return tuple(result)


def parse(raw, name='Look'):
    if not isinstance(raw, bytes) or len(raw) > 32*1024*1024:
        raise ValueError('Cube input must be UTF-8 bytes, at most 32 MiB')
    text = raw.decode('utf-8-sig')
    header, values = {}, []
    recognized = {'TITLE', 'LUT_3D_SIZE', 'DOMAIN_MIN', 'DOMAIN_MAX'}
    for line_number, line in enumerate(text.splitlines(), 1):
        # A comment marker inside a quoted title remains part of the title.
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f'Cube line {line_number}: {exc}') from exc
        if not fields:
            continue
        key = fields[0]
        if key in recognized:
            if values or key in header:
                raise ValueError(f'Cube line {line_number}: repeated or late {key}')
            header[key] = fields[1:]
            continue
        if key[0].isalpha() or key[0] == '_':
            raise ValueError(f'Cube line {line_number}: unsupported directive {key}')
        if 'LUT_3D_SIZE' not in header or len(fields) != 3:
            raise ValueError(f'Cube line {line_number}: expected RGB data after LUT_3D_SIZE')
        try:
            value = tuple(float(v) for v in fields)
        except ValueError as exc:
            raise ValueError(f'Cube line {line_number}: invalid numeric value') from exc
        if not all(math.isfinite(v) and abs(v) <= 65536 for v in value):
            raise ValueError(f'Cube line {line_number}: nonfinite or excessive value')
        values.append(value)
        if len(values) > 65**3:
            raise ValueError('Cube contains too many samples')
    if len(header.get('LUT_3D_SIZE', [])) != 1:
        raise ValueError('Exactly one LUT_3D_SIZE is required')
    try:
        size = int(header['LUT_3D_SIZE'][0])
    except ValueError as exc:
        raise ValueError('Invalid LUT_3D_SIZE') from exc
    if not 2 <= size <= 65 or len(values) != size**3:
        raise ValueError('Cube grid must be 2..65 with exactly size^3 samples')
    limits = []
    for key, default in [('DOMAIN_MIN', ['0']*3), ('DOMAIN_MAX', ['1']*3)]:
        try:
            row = tuple(float(v) for v in header.get(key, default))
        except ValueError as exc:
            raise ValueError(f'Invalid {key}') from exc
        if len(row) != 3 or not all(math.isfinite(v) and abs(v) <= 65536 for v in row):
            raise ValueError(f'Invalid {key}')
        limits.append(row)
    if any(lo >= hi for lo, hi in zip(*limits)):
        raise ValueError('Each DOMAIN_MAX must exceed DOMAIN_MIN')
    if 'TITLE' in header and len(header['TITLE']) != 1:
        raise ValueError('TITLE must be one quoted string')
    title = header.get('TITLE', [name])[0]
    if not title or len(title.encode('utf-8')) > 63 or any(ord(c) < 32 for c in title):
        raise ValueError('Look title must be 1..63 UTF-8 bytes without control characters')
    return Cube(title, size, *limits, tuple(values), hashlib.sha256(raw).hexdigest())


def encode_samples(cube, size):
    if size not in GRID_SIZES:
        raise ValueError('Native grid must be 17, 33, or 65')
    output, identity = bytearray(), True
    for r in range(size):
        for g in range(size):
            for b in range(size):
                rgb = (r/(size-1), g/(size-1), b/(size-1))
                values = cube.sample(rgb)
                quantized = tuple(round(min(1, max(0, v))*65535) for v in values)
                expected = tuple(round(v*65535) for v in rgb)
                identity &= quantized == expected
                output.extend(struct.pack('<3H', *quantized))
    return bytes(output), identity


def bundle(cubes, *, grid=33, default=0):
    if not 1 <= len(cubes) <= MAX_LOOKS or not 0 <= default <= len(cubes):
        raise ValueError('Supply 1..8 looks; default is 0 (Off) or a look index')
    if len({c.title.casefold() for c in cubes}) != len(cubes):
        raise ValueError('Look titles must be distinct')
    payload = bytearray(HEADER_BYTES+MAX_LOOKS*ENTRY_BYTES)
    details = []
    for i, cube in enumerate(cubes):
        samples, identity = encode_samples(cube, grid)
        offset = (len(payload)+63)&~63
        payload.extend(bytes(offset-len(payload)))
        payload.extend(samples)
        if len(payload) > MAX_BYTES:
            raise ValueError('LUT bundle exceeds the 2 MiB firmware region; use a smaller grid or fewer looks')
        entry = struct.pack('<6I64s8s', offset, len(samples), grid, int(identity),
                            zlib.crc32(samples), 0, cube.title.encode().ljust(64, b'\0'), bytes(8))
        payload[HEADER_BYTES+i*ENTRY_BYTES:HEADER_BYTES+(i+1)*ENTRY_BYTES] = entry
        details.append(dict(title=cube.title, input_size=cube.size, native_size=grid,
                            identity=identity, source_sha256=cube.source_sha256,
                            samples_sha256=hashlib.sha256(samples).hexdigest()))
    struct.pack_into('<8s6I', payload, 0, MAGIC, 2, len(payload), len(cubes), default,
                     zlib.crc32(payload[HEADER_BYTES:]), 0)
    return bytes(payload), details


def validate_bundle(raw):
    if len(raw) < HEADER_BYTES+MAX_LOOKS*ENTRY_BYTES or len(raw) > MAX_BYTES:
        raise ValueError('Invalid LUT bundle size')
    magic, version, size, count, default, checksum, reserved = struct.unpack_from('<8s6I', raw)
    if (magic != MAGIC or version != 2 or size != len(raw) or not 1 <= count <= MAX_LOOKS
            or default > count or reserved or any(raw[32:HEADER_BYTES])
            or zlib.crc32(raw[HEADER_BYTES:]) != checksum):
        raise ValueError('Invalid LUT bundle header or checksum')
    end, titles = HEADER_BYTES+MAX_LOOKS*ENTRY_BYTES, []
    for i in range(MAX_LOOKS):
        entry = raw[HEADER_BYTES+i*ENTRY_BYTES:HEADER_BYTES+(i+1)*ENTRY_BYTES]
        if i >= count:
            if any(entry):
                raise ValueError('Nonzero unused look entry')
            continue
        at, length, grid, identity, checksum, reserved, title, tail = struct.unpack('<6I64s8s', entry)
        expected_at = (end+63)&~63
        if (grid not in GRID_SIZES or at != expected_at or length != grid**3*6
                or at+length > len(raw) or identity > 1 or reserved or any(tail)
                or any(raw[end:at]) or zlib.crc32(raw[at:at+length]) != checksum):
            raise ValueError('Invalid LUT entry bounds or checksum')
        name, separator, padding = title.partition(b'\0')
        if not name or not separator or any(padding):
            raise ValueError('Invalid LUT title')
        name = name.decode('utf-8')
        if any(ord(c) < 32 for c in name) or name.casefold() in titles:
            raise ValueError('Invalid or duplicate LUT title')
        titles.append(name.casefold())
        end = at+length
    if end != len(raw):
        raise ValueError('Trailing LUT bundle data')
    return dict(count=count, default=default, bytes=size)
