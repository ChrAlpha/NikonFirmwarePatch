#!/usr/bin/env python3
"""Execute the shared colour-chain hooks in Unicorn; no firmware or compiler (needs unicorn)."""
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
from z8colour.patcher import CODE, CONFIG, LOG_TYPE, SITES
from z8log.patcher import colour_tables

REG = [getattr(regs, f'UC_ARM64_REG_X{i}') for i in range(31)]
PROVIDER = 0xA10200000
CTX_M, CTX_C, OTHER_CTX = 0xA10300000, 0xA10300400, 0xA10300800
STOCK_RECORD = 0xA10300C00          # [ctx+0x98] for the Cpxs site: tone byte at +0x31
STILL = dict(rgb=0x40B70000, nlg=0x40B71000, m2=0x40B72000, m1=0x40B73000, cpxs=0x40B74000)
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
        self.uc.mem_write(PROVIDER, struct.pack('<7Q', CTX_M, CTX_C, STILL['rgb'], STILL['nlg'], STILL['m2'], STILL['m1'], STILL['cpxs']))
        self.uc.mem_map(0xA10300000, 0x1000)
        self.uc.mem_map(0xA20000000, 0x10000)             # stack

    def run(self, entry, site, values, log_type, provider_tables=True):
        self.uc.mem_write(LOG_TYPE, bytes([log_type]))
        if not provider_tables:
            self.uc.mem_write(PROVIDER+0x10, bytes(40))
        else:
            self.uc.mem_write(PROVIDER+0x10, struct.pack('<5Q', STILL['rgb'], STILL['nlg'], STILL['m2'], STILL['m1'], STILL['cpxs']))
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


class ColourChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, profile = load('colour', 'prebuilt')
        cls.code = code
        cls.entries = {k: int(v['va'], 16) for k, v in profile['symbols'].items()}
        cls.tables = colour_tables()

    def check(self, entry, site, values, log, provider, ctx_ok, tables_ok, log_type, outputs, expected):
        m = Machine(self.code, self.entries, log, provider)
        start, out, sp, nzcv = m.run(entry, site, values, log_type, tables_ok)
        self.assertEqual(sp, 0xA20008000)
        if entry in FLAGS_KEPT:
            self.assertEqual(nzcv, 0x60000000)
        for i in range(31):
            if i in outputs:
                self.assertEqual(out[i], expected[i], (entry, i, log, provider, ctx_ok, tables_ok, log_type))
            elif i not in (9, 10) or entry not in ('colour_nlg_hook',):
                self.assertEqual(out[i], start[i], (entry, f'x{i} clobbered'))

    def cases(self):
        return itertools.product((False, True), (False, True), (False, True), (False, True), (0, 1, 2, 3, 0xFF))

    def test_rgb(self):
        for log, provider, ctx_ok, tables_ok, t in self.cases():
            for tone in (0, 1, 2):
                still = provider and ctx_ok and tables_ok
                x8 = STILL['rgb'] if still else self.tables[t-1] if log_choice(log, t, tone == 1) else STOCK_X8
                self.check('colour_rgb_hook', 0x4111A508, {8: 0xFFFF0000_00000000 | STOCK_X8, 9: tone, 19: CTX_C if ctx_ok else OTHER_CTX},
                           log, provider, ctx_ok, tables_ok, t, {8}, {8: x8})

    def test_nlg(self):
        for log, provider, ctx_ok, tables_ok, t in self.cases():
            for tone in (0, 1, 2, 3):
                still = provider and ctx_ok and tables_ok
                stock = tone*0x1CA + 0x441A2282
                x1 = STILL['nlg'] if still else self.tables[2+t-1] if log_choice(log, t, tone == 1) else stock
                self.check('colour_nlg_hook', 0x4111A6D4, {2: 0x1CA, 9: tone, 10: 0x441A2282, 19: CTX_C if ctx_ok else OTHER_CTX},
                           log, provider, ctx_ok, tables_ok, t, {1}, {1: x1})

    def test_m2(self):
        for log, provider, ctx_ok, tables_ok, t in self.cases():
            for row in (0, 1, 2):
                still = provider and ctx_ok and tables_ok and row == 1
                base = STILL['m2']-0x12 if still else self.tables[4+t-1]-0x12 if log_choice(log, t, row == 1) else STOCK_X9_M2
                self.check('colour_m2_hook', 0x41116CB4, {8: row, 9: STOCK_X9_M2, 11: 0x12, 19: CTX_M if ctx_ok else OTHER_CTX},
                           log, provider, ctx_ok, tables_ok, t, {9, 11}, {9: base, 11: row*0x12+base})

    def test_m1(self):
        for log, provider, ctx_ok, tables_ok, t in self.cases():
            for selector in (0, 1, 2):
                still = provider and ctx_ok and tables_ok and selector == 1
                x14 = STILL['m1']-0x24 if still else self.tables[6+t-1]-0x24 if log_choice(log, t, selector == 1) else STOCK_X14
                self.check('colour_m1_hook', 0x41116010, {11: selector, 13: 2, 14: 0xFFFF0000_00000000 | STOCK_X14, 19: CTX_M if ctx_ok else OTHER_CTX},
                           log, provider, ctx_ok, tables_ok, t, {14}, {14: x14})

    def test_cpxs(self):
        for log, provider, ctx_ok, tables_ok, t in self.cases():
            for tone in (0, 1, 2):
                ctx = CTX_C if ctx_ok else OTHER_CTX
                m = Machine(self.code, self.entries, log, provider)
                m.uc.mem_write(STOCK_RECORD, bytes(0x40))
                m.uc.mem_write(STOCK_RECORD+0x31, bytes([tone]))
                m.uc.mem_write(ctx+0x98, struct.pack('<Q', STOCK_RECORD))
                m.uc.mem_write(ctx+0xB0, struct.pack('<Q', 0x441A66AC))
                m.uc.mem_write(ctx+0x28, b'\x5a')
                start, out, sp, nzcv = m.run('colour_cpxs_hook', 0x4111A198, {19: ctx}, t, tables_ok)
                still = provider and ctx_ok and tables_ok
                want = STILL['cpxs'] if still else self.tables[8+t-1] if log_choice(log, t, tone == 1) else 0x441A66AC
                self.assertEqual(struct.unpack('<Q', m.uc.mem_read(ctx+0xB0, 8))[0], want)
                self.assertEqual(out[21] & 0xFFFFFFFF, 0x5A)
                self.assertEqual((sp, nzcv), (0xA20008000, 0x60000000))
                for i in range(31):
                    if i != 21:
                        self.assertEqual(out[i], start[i], f'x{i} clobbered')


if __name__ == '__main__':
    unittest.main()
