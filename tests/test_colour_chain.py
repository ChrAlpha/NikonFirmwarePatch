#!/usr/bin/env python3
"""Execute the shared colour-chain hooks in Unicorn; no firmware or compiler (needs unicorn).

Base hooks (every composition) serve provider lane 0 and the Log types; with photo-lut the sites enter the
lane hooks, which serve lane 1 and otherwise continue in the base hooks."""
import itertools
from pathlib import Path
import struct
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
from unicorn.arm64_const import UC_ARM64_REG_NZCV, UC_ARM64_REG_SP
import unicorn.arm64_const as regs
from z8patch.compiled import load
from z8colour.patcher import CODE, CONFIG, LOG_TYPE, SITES, lane
from z8log.patcher import colour_tables

REG = [getattr(regs, f'UC_ARM64_REG_X{i}') for i in range(31)]
PROVIDER = 0xA10200000
# Build contexts of lane 0 (still develop), lane 1 (live view) and any other build.
CTX = dict(still=(0xA10300000, 0xA10300400), live=(0xA10300200, 0xA10300600), other=(0xA10300800, 0xA10300A00))
STOCK_RECORD = 0xA10300C00          # [ctx+0x98] for the Cpxs site: tone byte at +0x31
TABLES = dict(still=dict(rgb=0x40B70000, nlg=0x40B71000, m2=0x40B72000, m1=0x40B73000, cpxs=0x40B74000),
              live=dict(rgb=0x40B75000, nlg=0x40B76000, m2=0x40B77000, m1=0x40B78000, cpxs=0x40B79000))
LANES = ('still', 'live')
CONTINUE = {at: at+4 for at, _, _ in SITES}
STOCK_X8, STOCK_X9_M2, STOCK_X14 = 0x441A1010, 0x441A56A8, 0x441A0A4A
# RGB and NLG continuations set the flags again (4111A514 cmp) or call (4111A6E0 bl) before any use.
FLAGS_KEPT = ('colour_m2_hook', 'colour_m1_hook', 'colour_cpxs_hook')


class Machine:
    def __init__(self, code, entries, log, provider):
        self.entries = entries
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.uc.mem_map(0x40980000, 0x10000)
        self.uc.mem_write(CODE, code)
        config = struct.pack('<Q10QQ', LOG_TYPE if log else 0, *(colour_tables() if log else [0]*10), PROVIDER if provider else 0)
        self.uc.mem_write(CONFIG, config)
        self.uc.mem_map(0x41116000, 0x5000)               # continuations: never executed past the first instruction
        self.uc.mem_map(0xA03D5C000, 0x1000)
        self.uc.mem_map(PROVIDER, 0x1000)
        for i, name in enumerate(LANES):
            self.uc.mem_write(PROVIDER+0x38*i, struct.pack('<2Q', *CTX[name]))
        self.uc.mem_map(0xA10300000, 0x1000)
        self.uc.mem_map(0xA20000000, 0x10000)             # stack

    def run(self, entry, site, values, log_type, provider_tables=True):
        self.uc.mem_write(LOG_TYPE, bytes([log_type]))
        for i, name in enumerate(LANES):
            t = TABLES[name]
            self.uc.mem_write(PROVIDER+0x38*i+0x10, struct.pack('<5Q', t['rgb'], t['nlg'], t['m2'], t['m1'], t['cpxs'])
                              if provider_tables else bytes(40))
        start = {i: 0x1111_0000_0000 + i for i in range(31)}
        start.update(values)
        for i, v in start.items():
            self.uc.reg_write(REG[i], v)
        self.uc.reg_write(UC_ARM64_REG_SP, 0xA20008000)
        self.uc.reg_write(UC_ARM64_REG_NZCV, 0x60000000)
        self.uc.emu_start(self.entries[entry], CONTINUE[site])
        out = {i: self.uc.reg_read(REG[i]) for i in range(31)}
        return start, out, self.uc.reg_read(UC_ARM64_REG_SP), self.uc.reg_read(UC_ARM64_REG_NZCV)


def log_choice(log, log_type, native):
    return log and native and log_type in (1, 2)


def served(provider, ctx, tables_ok, entry):
    """The lane whose tables the build gets, or None: lane 0 from every hook, lane 1 from the lane hooks only."""
    if not (provider and tables_ok) or ctx == 'other':
        return None
    return ctx if ctx == 'still' or entry.startswith('colour_lane_') else None


class ColourChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, profile = load('colour', 'prebuilt')
        cls.code = code
        cls.entries = {k: int(v['va'], 16) for k, v in profile['symbols'].items()}
        cls.tables = colour_tables()

    def check(self, entry, site, values, log, provider, ctx, tables_ok, log_type, outputs, expected):
        m = Machine(self.code, self.entries, log, provider)
        start, out, sp, nzcv = m.run(entry, site, values, log_type, tables_ok)
        self.assertEqual(sp, 0xA20008000)
        if entry.replace('lane_', '') in FLAGS_KEPT:
            self.assertEqual(nzcv, 0x60000000)
        for i in range(31):
            if i in outputs:
                self.assertEqual(out[i], expected[i], (entry, i, log, provider, ctx, tables_ok, log_type))
            elif i not in (9, 10) or entry.replace('lane_', '') != 'colour_nlg_hook':
                self.assertEqual(out[i], start[i], (entry, f'x{i} clobbered'))

    def cases(self, base):
        """(entry, log, provider, ctx, tables_ok, log_type); lane hooks exist only with a provider (photo-lut)."""
        for log, provider, ctx, tables_ok, t in itertools.product((False, True), (False, True), ('still', 'live', 'other'),
                                                                 (False, True), (0, 1, 2, 3, 0xFF)):
            yield base, log, provider, ctx, tables_ok, t
            if provider:
                yield lane(base), log, provider, ctx, tables_ok, t

    def test_rgb(self):
        for entry, log, provider, ctx, tables_ok, t in self.cases('colour_rgb_hook'):
            for tone in (0, 1, 2):
                lane_name = served(provider, ctx, tables_ok, entry)
                x8 = TABLES[lane_name]['rgb'] if lane_name else self.tables[t-1] if log_choice(log, t, tone == 1) else STOCK_X8
                self.check(entry, 0x4111A508, {8: 0xFFFF0000_00000000 | STOCK_X8, 9: tone, 19: CTX[ctx][1]},
                           log, provider, ctx, tables_ok, t, {8}, {8: x8})

    def test_nlg(self):
        for entry, log, provider, ctx, tables_ok, t in self.cases('colour_nlg_hook'):
            for tone in (0, 1, 2, 3):
                lane_name = served(provider, ctx, tables_ok, entry)
                stock = tone*0x1CA + 0x441A2282
                x1 = TABLES[lane_name]['nlg'] if lane_name else self.tables[2+t-1] if log_choice(log, t, tone == 1) else stock
                self.check(entry, 0x4111A6D4, {2: 0x1CA, 9: tone, 10: 0x441A2282, 19: CTX[ctx][1]},
                           log, provider, ctx, tables_ok, t, {1}, {1: x1})

    def test_m2(self):
        for entry, log, provider, ctx, tables_ok, t in self.cases('colour_m2_hook'):
            for row in (0, 1, 2):
                lane_name = served(provider, ctx, tables_ok, entry) if row == 1 else None
                base = TABLES[lane_name]['m2']-0x12 if lane_name else self.tables[4+t-1]-0x12 if log_choice(log, t, row == 1) else STOCK_X9_M2
                self.check(entry, 0x41116CB4, {8: row, 9: STOCK_X9_M2, 11: 0x12, 19: CTX[ctx][0]},
                           log, provider, ctx, tables_ok, t, {9, 11}, {9: base, 11: row*0x12+base})

    def test_m1(self):
        for entry, log, provider, ctx, tables_ok, t in self.cases('colour_m1_hook'):
            for selector in (0, 1, 2):
                lane_name = served(provider, ctx, tables_ok, entry) if selector == 1 else None
                x14 = TABLES[lane_name]['m1']-0x24 if lane_name else self.tables[6+t-1]-0x24 if log_choice(log, t, selector == 1) else STOCK_X14
                self.check(entry, 0x41116010, {11: selector, 13: 2, 14: 0xFFFF0000_00000000 | STOCK_X14, 19: CTX[ctx][0]},
                           log, provider, ctx, tables_ok, t, {14}, {14: x14})

    def test_cpxs(self):
        for entry, log, provider, ctx, tables_ok, t in self.cases('colour_cpxs_hook'):
            for tone in (0, 1, 2):
                context = CTX[ctx][1]
                m = Machine(self.code, self.entries, log, provider)
                m.uc.mem_write(STOCK_RECORD, bytes(0x40))
                m.uc.mem_write(STOCK_RECORD+0x31, bytes([tone]))
                m.uc.mem_write(context+0x98, struct.pack('<Q', STOCK_RECORD))
                m.uc.mem_write(context+0xB0, struct.pack('<Q', 0x441A66AC))
                m.uc.mem_write(context+0x28, b'\x5a')
                start, out, sp, nzcv = m.run(entry, 0x4111A198, {19: context}, t, tables_ok)
                lane_name = served(provider, ctx, tables_ok, entry)
                want = TABLES[lane_name]['cpxs'] if lane_name else self.tables[8+t-1] if log_choice(log, t, tone == 1) else 0x441A66AC
                self.assertEqual(struct.unpack('<Q', m.uc.mem_read(context+0xB0, 8))[0], want, (entry, ctx, tone, t))
                self.assertEqual(out[21] & 0xFFFFFFFF, 0x5A)
                self.assertEqual((sp, nzcv), (0xA20008000, 0x60000000))
                for i in range(31):
                    if i != 21:
                        self.assertEqual(out[i], start[i], f'x{i} clobbered')

    def test_compositions_install_the_base_part_alone_or_with_the_lane_hooks(self):
        from z8colour.patcher import emit
        from z8prores.menu import Image
        from z8patch.image import BASE
        for log, provider in ((True, 0), (False, PROVIDER), (True, PROVIDER)):
            with self.subTest(log=log, provider=bool(provider)):
                stock = bytearray(0x01000000)
                for at, word, _ in SITES:
                    stock[at-BASE:at-BASE+4] = bytes.fromhex(word)
                img = Image(bytes(stock))
                detail = emit(img, self.code, self.entries, colour_tables() if log else None, provider)
                base = self.entries['colour_base_end']-CODE
                installed = bytes(img.new[CODE-BASE:CONFIG-BASE])
                self.assertEqual(installed, (self.code if provider else self.code[:base]).ljust(CONFIG-CODE, b'\0'))
                self.assertEqual(set(detail['sites'].values()), {lane(e) if provider else e for _, _, e in SITES})


if __name__ == '__main__':
    unittest.main()
