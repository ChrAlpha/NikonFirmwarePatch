"""Shared photo LUT test fixtures: the reviewed native unit, card libraries from literal cubes, bank records."""
import hashlib
import struct

from z8patch.compiled import load
from z8photolut import library as lib
from z8photolut.library import Library, look_from_cube
from z8photolut.patcher import (CODE, CODE_END, DEVELOP, DEVELOP_BYTES, LANE_RECORDS, STATE as STATE_IMAGE, STATE_VA,
                                runtime_layout)

LEASE_BYTES, LEASE_MUTABLE = 320, 256   # firmware.c JpegLease: four immutable lines, then one CPU line
JPEG_STORAGE = 8256*5504*2+1920*1080*2+640*480*2+192*120*2+0x3600


def native_code():
    """The photo-lut unit as the composer installs it (resources/photo-lut.bin, checked against its
    reviewed sources), its linked symbol addresses and its runtime layout."""
    code, profile = load('photo-lut')
    symbols = {name: int(value['va'], 16) for name, value in profile['symbols'].items() if not name.startswith('$')}
    return code, symbols, dict(runtime_layout=runtime_layout(code, symbols), sha256=profile['sha256'],
                               flags=profile['flags'], compiler=profile['compiler'])


def runtime_bundle(cubes, grid=17, default=0):
    """Runtime bytes for (raw .cube bytes) in order, named by their TITLE."""
    looks = tuple(look_from_cube(raw, source_name=f'look{i}.cube', grid=grid) for i, raw in enumerate(cubes))
    return Library(looks, default).runtime()


# Stock factory N-Log M2 row and the identity Cpxs row; the curve is an N-Log-like monotone shape.
NLOG_M2 = [118, 439, -45, -59, -352, 411, 245, -212, -33]


def isp_tables(m1=None, curve_power=0.6):
    """Valid still-develop tables in their firmware formats (no compiler needed)."""
    curve = [round(32*(128+(1023-128)*(i/228)**curve_power)) for i in range(229)]
    return dict(rgb=curve*3, nlg=[(g+4) >> 3 for g in curve], m2=list(NLOG_M2),
                m1=list(m1 or [1024, 0, 0, 0, 1024, 0, 0, 0, 1024]), cpxs=[256, 0, 0, 256, 0, 0]*32)


FIT_RECORD = dict(compiler='z8photolut.fit/1', grey=400.0, smoothness=3.0, samples=500, iterations=1, seed=1,
                  report=dict(isp_de00=[1.0, 2.0, 3.0], final_de00=[0.5, 1.0, 2.0], slope=[1.0, 2.0], reachable=0.9,
                              cpxs_min_det=1.0, m1_condition=1.0, m2_condition=2.0, m1_target_q10=[1024, 0, 0, 0, 1024, 0, 0, 0, 1024]))


def isp_look(name, basis='s-log3/s-gamut3.cine', residual=None, grid=17, m1=None, source=b'TITLE "Log"\n'):
    """An isp+residual look from fixed tables: residual is the raw .cube resampled as the residual grid
    (None: the identity grid). Firmware tests use it without fitting anything."""
    samples = look_from_cube(residual, source_name='residual.cube', grid=grid).samples if residual else lib.identity_samples(grid)
    return lib.Look(name, grid, samples, samples == lib.identity_samples(grid), source, 'log.cube', 'Log', 2,
                    (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), '', basis, lib.pack_isp(isp_tables(m1)), FIT_RECORD)


def snapshot_bytes(grid):
    """Bytes a transform reservation adds for the latched private look grid."""
    return (grid**3*6+63)&~63


CODE_BYTES = CODE_END-CODE
STATE = STATE_VA
PRIVATE_BYTES = DEVELOP+DEVELOP_BYTES-STATE_IMAGE      # the zero state page, then the develop pages
LANE_BYTES = 0x80                                       # firmware.h Z8DevelopLane
# A published card library in the general heap; the firmware itself carries no looks.
LIBRARY = 0xa1e000000
BANK_INDEX = 0xa03d48164
RECORD_MAGIC = 0x4b42385a
RESTORE_DONE = 2
LIBRARY_NONE, LIBRARY_CARD = 0, 1
# Native continuations the hooks branch to (hooks.S original_<address>).
RESUME = dict(queue_resume=0x40cd7d58, queue_skip=0x40cd7f24, frame_resume=0x40e991b4, frame_success=0x40e991ec,
              applied_resume=0x402d14b4, applied_failure=0x402d14d0, native_y_setter=0x402ab038)


def bundle_ids(m, bundle=LIBRARY):
    """Full look identities of a bundle in emulator memory, in order."""
    count = struct.unpack('<I', m.mem_read(bundle+16, 4))[0]
    return [bytes(m.mem_read(bundle+64+i*128+88, 32)) for i in range(count)]


def record_bytes(banks, library=LIBRARY_NONE):
    """A sealed bank record for banks A..D: None = Off, bytes = look id, 'unresolved';
    library 0 none, 1 card."""
    body = struct.pack('<IHH2I', RECORD_MAGIC, 1, 0xc0, library, 0)
    for bank in banks:
        kind = 0 if bank is None else 2 if bank == 'unresolved' else 1
        body += struct.pack('<B3x', kind)+(bank if kind == 1 else bytes(32))
    return body+hashlib.sha256(body).digest()


def publish(m, layout, bundle=LIBRARY):
    """Publish a card library already in emulator memory, as the card worker does."""
    m.mem_write(STATE+layout['card_offset'], struct.pack('<Q', bundle))


def desire(m, layout, look, bank=0, bundle=LIBRARY, ready=True, restore=RESTORE_DONE):
    """Publish the card library at `bundle` and make its look `look` (1-based; 0 = Off) the
    selected bank's desired look.

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
    publish(m, layout, bundle)
    m.mem_write(BANK_INDEX, struct.pack('<I', bank))
    m.mem_write(persist, record_bytes(banks, LIBRARY_CARD))
    m.mem_write(persist+layout['persist_ready_offset'], struct.pack('<I', int(ready)))
    m.mem_write(persist+layout['persist_authoritative_offset'], struct.pack('<I', int(ready)))
    m.mem_write(persist+layout['persist_restore_offset'], struct.pack('<I', restore))


def lane_record(m, lane, look_id=None, choice=1, streak=1):
    """Make develop lane `lane` (0 still, 1 live) report its last M1/M2 build as served with look_id
    (None: native), as the build wrappers do."""
    at = LANE_RECORDS+lane*LANE_BYTES
    if look_id is None:
        m.mem_write(at, struct.pack('<4I', 0, 0, 0, 0)+bytes(32))
    else:
        m.mem_write(at, struct.pack('<4I', 0, choice, streak, 0)+look_id)
