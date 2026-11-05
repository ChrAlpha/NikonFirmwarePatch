"""Strict IRIDAS 3D .cube ingestion, runtime resampling and runtime .cube export.

Format reference: OpenColorIO src/OpenColorIO/fileformats/FileFormatIridasCube.cpp.
Cube rows are red-fastest; native samples are blue-fastest RGB uint16. Input
domains are baked into a normalized grid, and outputs clip to the SDR range.
"""
from dataclasses import dataclass
import hashlib
import math
import re
import shlex
import struct

GRID_SIZES = (17, 33, 65)

# The camera applies a look to the photo it has already rendered: 8-bit
# full-range BT.601 Y'CbCr of the JPEG in its selected colour space, after
# the active Picture Control. Cubes made for camera log or HDR encodings
# expect a different input and would be silently wrong there.
INPUT_BASIS = 'nikon-rendered-sdr'
FOREIGN_BASIS = re.compile(
    r'(?<![a-z0-9])(?:[vnsfcdl]-?log[0-9]?|log-?c[0-9]?|log3g10|hlg|pq|st[ ._-]?2084|'
    r'(?:rec|bt|itu)[ ._-]?2100|(?:rec|bt|itu)[ ._-]?2020|acescc|acescct|cineon)(?![a-z0-9])',
    re.IGNORECASE)
PANASONIC_STYLE = re.compile(r'^\s*#\s*LUMIXPHOTOSTYLE\b\s*(\S*)', re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Cube:
    title: str
    size: int
    minimum: tuple
    maximum: tuple
    values: tuple
    source_sha256: str
    comments: tuple = ()

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
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Cube input is not UTF-8 text') from exc
    header, values, comments = {}, [], []
    recognized = {'TITLE', 'LUT_3D_SIZE', 'DOMAIN_MIN', 'DOMAIN_MAX'}
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#'):
            comments.append(line.strip())
            continue
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
    if any(ord(c) < 32 for c in title):
        raise ValueError('TITLE contains control characters')
    return Cube(title, size, *limits, tuple(values), hashlib.sha256(raw).hexdigest(), tuple(comments))


def foreign_basis(cube, filename=''):
    """Return why a cube appears to expect input other than Nikon-rendered SDR, or ''."""
    reasons = []
    for comment in cube.comments:
        match = PANASONIC_STYLE.match(comment)
        if match:
            # Panasonic documents a missing or invalid tag as a V-Log base.
            style = match.group(1).upper() or 'missing (V-Log)'
            reasons.append(f'#LUMIXPHOTOSTYLE {style}: made for a Panasonic Photo Style base')
    for label, text in [('file name', filename), ('TITLE', cube.title), *(('comment', c) for c in cube.comments)]:
        match = FOREIGN_BASIS.search(text)
        if match:
            reasons.append(f'{label} mentions {match.group(0)}')
    if any(lo < 0 or hi > 1 for lo, hi in zip(cube.minimum, cube.maximum)):
        reasons.append('DOMAIN_MIN/MAX exceeds 0..1 (scene-referred or shaper input)')
    return '; '.join(dict.fromkeys(reasons))


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


def runtime_cube(title, grid, samples, comments=()):
    """Write the applied native grid as a red-fastest .cube with exact uint16 values."""
    if grid not in GRID_SIZES or len(samples) != grid**3*6:
        raise ValueError('Invalid runtime sample grid')
    if any(ord(c) < 32 for c in title+''.join(comments)):
        raise ValueError('Runtime cube text contains control characters')
    values = struct.unpack(f'<{grid**3*3}H', samples)
    # parse() reads TITLE with POSIX shell quoting: inside double quotes only
    # backslash and the quote itself are escapable, so this round-trips exactly.
    quoted = title.replace('\\', '\\\\').replace('"', '\\"')
    rows = [f'# {c}' for c in comments]
    rows += [f'TITLE "{quoted}"', f'LUT_3D_SIZE {grid}']
    for b in range(grid):
        for g in range(grid):
            for r in range(grid):
                at = ((r*grid+g)*grid+b)*3
                rows.append(' '.join(f'{values[at+c]/65535:.9f}' for c in range(3)))
    return ('\n'.join(rows)+'\n').encode()
