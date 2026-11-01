"""Shared photo LUT test fixtures: runtime bundles from literal cubes, bank records."""
import hashlib
import struct

from z8photolut.library import Library, look_from_cube

LEASE_BYTES = 256
JPEG_STORAGE = 8256*5504*2+1920*1080*2+640*480*2+192*120*2+0x3600


def runtime_bundle(cubes, grid=17, default=0):
    """Runtime bytes for (raw .cube bytes) in order, named by their TITLE."""
    looks = tuple(look_from_cube(raw, source_name=f'look{i}.cube', grid=grid) for i, raw in enumerate(cubes))
    return Library(looks, default).runtime()


def snapshot_bytes(grid):
    """Bytes a transform reservation adds for the latched private look grid."""
    return (grid**3*6+63)&~63


STATE = 0xa101f0000
BUILTIN = 0x409a0000
BANK_INDEX = 0xa03d48164
RECORD_MAGIC = 0x4b42385a
RESTORE_DONE = 2


def bundle_ids(m, bundle=BUILTIN):
    """Full look identities of a bundle in emulator memory, in order."""
    count = struct.unpack('<I', m.mem_read(bundle+16, 4))[0]
    return [bytes(m.mem_read(bundle+64+i*128+88, 32)) for i in range(count)]


def record_bytes(banks, library=0):
    """A sealed bank record for banks A..D: None = Off, bytes = look id, 'unresolved';
    library 0 built-in, 1 card."""
    body = struct.pack('<IHH2I', RECORD_MAGIC, 1, 0xc0, library, 0)
    for bank in banks:
        kind = 0 if bank is None else 2 if bank == 'unresolved' else 1
        body += struct.pack('<B3x', kind)+(bank if kind == 1 else bytes(32))
    return body+hashlib.sha256(body).digest()


def desire(m, layout, look, bank=0, bundle=BUILTIN, ready=True, restore=RESTORE_DONE):
    """Make built-in look `look` (1-based; 0 = Off) the selected bank's desired look.

    Placeholder bundles without identities get distinct ones, so each row is
    named by its own full identity as in real libraries."""
    ids = bundle_ids(m, bundle)
    for i, value in enumerate(ids):
        if not any(value):
            ids[i] = bytes([i+1])*32
            m.mem_write(bundle+64+i*128+88, ids[i])
    banks = [None]*4
    banks[bank] = ids[look-1] if look else None
    persist = STATE+layout['persist_offset']
    m.mem_write(BANK_INDEX, struct.pack('<I', bank))
    m.mem_write(persist, record_bytes(banks))
    m.mem_write(persist+layout['persist_ready_offset'], struct.pack('<I', int(ready)))
    m.mem_write(persist+layout['persist_authoritative_offset'], struct.pack('<I', int(ready)))
    m.mem_write(persist+layout['persist_restore_offset'], struct.pack('<I', restore))
