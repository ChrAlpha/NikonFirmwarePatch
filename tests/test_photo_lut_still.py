#!/usr/bin/env python3
"""Still develop of Log looks in Unicorn: the compiled BL wrappers around the two native builders, and the
shared colour hooks serving the provider block they publish (needs unicorn; no firmware or compiler)."""
import struct
import unittest

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc = None

from photo_lut_fixtures import CODE_BYTES, LIBRARY, STATE, desire, isp_look, native_code
from z8colour.patcher import CODE as COLOUR, CONFIG
from z8patch.compiled import load
from z8photolut import library as lib
from z8photolut.patcher import CODE, PROVIDER, STILL_CALLS, STILL_VA

CYCLE = ('TITLE "Cycle"\nLUT_3D_SIZE 2\n'+''.join(f'{g} {b} {r}\n' for b in (0, 1) for g in (0, 1) for r in (0, 1))).encode()
PARAMS_M, PARAMS_C = 0xA03F33D08, 0xA03F34000      # native argument blocks of the two builder calls
CTX_M, CTX_C = 0xA0436C568, 0xA044FC580             # the contexts the native code stores at [x0+0x30]
SETTINGS = 0xC427A71D8                              # S = [x0+0x20]: pool image RAM, cached alias (QEMU C3.11)
THUNK_M, THUNK_C = STILL_CALLS[0][1], STILL_CALLS[1][1]
STOP, STACK = 0x65000000, 0xA20008000
STOCK_RGB, STOCK_NLG, STOCK_M2, STOCK_M1, STOCK_CPXS = 0x441A1010, 0x441A2282, 0x441A56A8, 0x441A0A4A, 0x441A66AC
SENTINEL = {i: 0x5150_0000_0000+i for i in range(19, 29)}


@unittest.skipIf(Uc is None, 'Unicorn is required for the still develop checks')
class StillDevelopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.symbols, cls.metadata = native_code()
        colour, profile = load('colour', 'prebuilt')
        cls.colour = colour
        cls.colour_entries = {k: int(v['va'], 16) for k, v in profile['symbols'].items()}

    def machine(self, looks, selected, tone=0, publishing=False):
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for at, size in [(CODE, CODE_BYTES), (0x40980000, 0x10000), (LIBRARY, 0x40000), (STATE, 0x2000),
                         (0xA03D48000, 0x1000), (0xA03F33000, 0x2000), (0xC427A7000, 0x1000), (0xA0436C000, 0x1000),
                         (0xA044FC000, 0x1000), (0x41112000, 0x1000), (0x41116000, 0x5000), (STOP, 0x1000),
                         (0xA20000000, 0x10000)]:
            m.mem_map(at, size)
        m.mem_write(CODE, self.code)
        m.mem_write(COLOUR, self.colour)
        m.mem_write(CONFIG, struct.pack('<Q10QQ', 0, *[0]*10, PROVIDER))
        layout = self.metadata['runtime_layout']
        m.mem_write(STATE, struct.pack('<Q6I', 0x32545354554c385a, 4, layout['state_bytes'], 0, 256, 1, 0))
        m.mem_write(LIBRARY, lib.Library(looks).runtime())
        desire(m, layout, selected)
        if publishing:
            m.mem_write(STATE+layout['card_offset']+12, struct.pack('<I', 1))
        m.mem_write(PARAMS_M+0x20, struct.pack('<Q', SETTINGS))
        m.mem_write(PARAMS_M+0x30, struct.pack('<Q', CTX_M))
        m.mem_write(PARAMS_C+0x30, struct.pack('<Q', CTX_C))
        m.mem_write(SETTINGS+0xBC, struct.pack('<I', tone))
        for thunk in (THUNK_M, THUNK_C):
            m.mem_write(thunk, bytes.fromhex('c0035fd6'))             # ret: the native builder stub
        self.during = {}

        def builder(uc, at, size, user):
            args = [uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(3)]
            self.during[at] = dict(args=args, tone=struct.unpack('<I', uc.mem_read(SETTINGS+0xBC, 4))[0],
                                   provider=struct.unpack('<7Q', uc.mem_read(PROVIDER, 56)))
        m.hook_add(UC_HOOK_CODE, builder, begin=THUNK_M, end=THUNK_M)
        m.hook_add(UC_HOOK_CODE, builder, begin=THUNK_C, end=THUNK_C)
        return m

    def wrapper(self, m, entry, params):
        for i, value in SENTINEL.items():
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        m.reg_write(regs.UC_ARM64_REG_X0, params)
        m.reg_write(regs.UC_ARM64_REG_X1, 0xA03F33D40)
        m.reg_write(regs.UC_ARM64_REG_X2, 0x7777)
        m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        m.reg_write(regs.UC_ARM64_REG_X30, STOP)
        m.emu_start(self.symbols[entry], STOP, count=2_000_000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_SP), STACK)
        for i, value in SENTINEL.items():
            self.assertEqual(m.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')), value, f'x{i} not preserved')

    def develop(self, m):
        """One native still develop: the M1/M2 build, then the curve build."""
        self.wrapper(m, 'z8_photo_lut_still_m_entry', PARAMS_M)
        self.wrapper(m, 'z8_photo_lut_still_curve_entry', PARAMS_C)
        return self.during[THUNK_M], self.during[THUNK_C]

    def hook(self, m, entry, values, until):
        for i in range(31):
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), values.get(i, 0x1111_0000_0000+i))
        m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        m.emu_start(self.colour_entries[entry], until, count=1000)
        return {i: m.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(31)}

    def served_tables(self, m, provider, tone):
        """Run the five colour hooks as the native builds would, with the contexts published during the
        builds; return the table addresses they select."""
        m.mem_write(PROVIDER, struct.pack('<7Q', *provider))
        rgb = self.hook(m, 'colour_rgb_hook', {8: STOCK_RGB, 9: tone, 19: CTX_C}, 0x4111A50C)[8]
        nlg = self.hook(m, 'colour_nlg_hook', {2: 0x1CA, 9: tone, 10: STOCK_NLG, 19: CTX_C}, 0x4111A6D8)[1]
        m2 = self.hook(m, 'colour_m2_hook', {8: tone, 9: STOCK_M2, 11: 0x12, 19: CTX_M}, 0x41116CB8)[11]
        m1 = self.hook(m, 'colour_m1_hook', {11: tone, 13: 2, 14: STOCK_M1, 19: CTX_M}, 0x41116014)[14]+2*0x12
        record = 0xA0436C800
        m.mem_write(record, bytes(0x40))
        m.mem_write(record+0x31, bytes([tone]))
        m.mem_write(CTX_C+0x98, struct.pack('<Q', record))
        m.mem_write(CTX_C+0xB0, struct.pack('<Q', STOCK_CPXS))
        self.hook(m, 'colour_cpxs_hook', {19: CTX_C}, 0x4111A19C)
        cpxs = struct.unpack('<Q', m.mem_read(CTX_C+0xB0, 8))[0]
        return dict(rgb=rgb, nlg=nlg, m2=m2, m1=m1, cpxs=cpxs)

    def test_a_log_look_develops_through_its_own_tables(self):
        look = isp_look('Log', residual=CYCLE, m1=[1000, 30, -6, 12, 990, 22, 0, -20, 1044])
        m = self.machine((isp_look('Other', basis='v-log/v-gamut'), look), 2)
        built, curve = self.develop(m)
        # The builders get their original arguments; only the M1/M2 build sees S+0xbc = 1.
        self.assertEqual(built['args'], [PARAMS_M, 0xA03F33D40, 0x7777])
        self.assertEqual(curve['args'], [PARAMS_C, 0xA03F33D40, 0x7777])
        self.assertEqual((built['tone'], curve['tone']), (1, 0))
        self.assertEqual(built['provider'][:2], (CTX_M, 0))
        self.assertEqual(curve['provider'][:2], (0, CTX_C))
        tables = STILL_VA+0x100
        self.assertEqual(built['provider'][2:], (tables, tables+0x560, tables+0x730, tables+0x750, tables+0x770))
        self.assertEqual(bytes(m.mem_read(tables, lib.ISP_BYTES)), look.isp)
        # After the develop the native settings and both contexts are back.
        self.assertEqual(struct.unpack('<I', m.mem_read(SETTINGS+0xBC, 4))[0], 0)
        self.assertEqual(struct.unpack('<2Q', m.mem_read(PROVIDER, 16)), (0, 0))
        self.assertEqual(bytes(m.mem_read(STILL_VA+0x48, 32)), look.id)
        self.assertEqual(struct.unpack('<5Q', m.mem_read(STILL_VA+0x68, 40)), (1, 1, 1, 1, 0))
        # The colour hooks serve exactly these tables to these builds (R+0x31 = 1 after the first build).
        served = self.served_tables(m, built['provider'][:1]+curve['provider'][1:2]+built['provider'][2:], 1)
        self.assertEqual(served, dict(rgb=tables, nlg=tables+0x560, m2=tables+0x730, m1=tables+0x750,
                                      cpxs=tables+0x770))
        isp = lib.unpack_isp(look.isp)
        self.assertEqual(struct.unpack('<9h', m.mem_read(served['m1'], 18)), isp['m1'])
        self.assertEqual(struct.unpack('<687H', m.mem_read(served['rgb'], 1374)), isp['rgb'])
        self.assertEqual(struct.unpack('<192h', m.mem_read(served['cpxs'], 384)), isp['cpxs'])
        # Any other build (live view, another context) keeps the native N-Log or stock selection.
        native = self.served_tables(m, (0, 0)+built['provider'][2:], 1)
        self.assertEqual(native, dict(rgb=STOCK_RGB, nlg=STOCK_NLG+0x1CA, m2=STOCK_M2+0x12, m1=STOCK_M1+2*0x12,
                                      cpxs=STOCK_CPXS))

    def test_other_selections_keep_the_native_develop(self):
        cases = {
            'cpu-3d look': (lambda: (isp_look('Log'), lib.look_from_cube(CYCLE, source_name='c.cube', grid=17)), 2, {}),
            'off': (lambda: (isp_look('Log'),), 0, {}),
            'publication in progress': (lambda: (isp_look('Log'),), 1, dict(publishing=True)),
        }
        for label, (looks, selected, options) in cases.items():
            with self.subTest(label=label):
                m = self.machine(looks(), selected, tone=7, **options)
                built, curve = self.develop(m)
                self.assertEqual((built['tone'], curve['tone']), (7, 7))
                self.assertEqual(built['provider'][:2]+curve['provider'][:2], (0, 0, 0, 0))
                self.assertEqual(bytes(m.mem_read(STILL_VA+0x48, 32)), bytes(32))
                refused = 1 if options else 0
                self.assertEqual(struct.unpack('<5Q', m.mem_read(STILL_VA+0x68, 40)), (1, 1, 0, 0, refused))

    def test_unreadable_parameters_keep_the_native_develop(self):
        for label, settings in (('S outside RAM', 0x441A0000), ('S misaligned', SETTINGS+1)):
            with self.subTest(label=label):
                m = self.machine((isp_look('Log'),), 1)
                m.mem_write(PARAMS_M+0x20, struct.pack('<Q', settings))
                built, curve = self.develop(m)
                self.assertEqual(built['provider'][:2]+curve['provider'][:2], (0, 0, 0, 0))
                self.assertEqual(struct.unpack('<Q', m.mem_read(STILL_VA+0x88, 8))[0], 1)

    def test_unavailable_look_and_a_curve_build_alone_stay_native(self):
        m = self.machine((isp_look('Log'),), 1)
        layout = self.metadata['runtime_layout']
        m.mem_write(STATE+layout['card_offset'], struct.pack('<Q', 0))     # the card library is gone
        built, curve = self.develop(m)
        self.assertEqual(built['tone'], 0)
        self.assertEqual(built['provider'][0]+curve['provider'][1], 0)
        # A curve build that does not follow a served M1/M2 build (live view, a retry) is never served.
        m = self.machine((isp_look('Log'),), 1)
        self.wrapper(m, 'z8_photo_lut_still_curve_entry', PARAMS_C)
        self.assertEqual(self.during[THUNK_C]['provider'][:2], (0, 0))
        self.wrapper(m, 'z8_photo_lut_still_m_entry', PARAMS_M)
        self.wrapper(m, 'z8_photo_lut_still_curve_entry', PARAMS_C)
        self.wrapper(m, 'z8_photo_lut_still_curve_entry', PARAMS_C)
        self.assertEqual(self.during[THUNK_C]['provider'][:2], (0, 0))
        self.assertEqual(struct.unpack('<4Q', m.mem_read(STILL_VA+0x68, 32)), (1, 3, 1, 1))

    def test_a_native_tone_is_restored_and_the_look_is_recopied_each_develop(self):
        first, second = isp_look('A', residual=CYCLE), isp_look('B', m1=[900, 0, 0, 0, 1024, 0, 0, 0, 1100])
        m = self.machine((first, second), 1, tone=3)
        self.develop(m)
        self.assertEqual(bytes(m.mem_read(STILL_VA+0x100, lib.ISP_BYTES)), first.isp)
        desire(m, self.metadata['runtime_layout'], 2)
        built, _ = self.develop(m)
        self.assertEqual(built['tone'], 1)
        self.assertEqual(struct.unpack('<I', m.mem_read(SETTINGS+0xBC, 4))[0], 3)
        self.assertEqual(bytes(m.mem_read(STILL_VA+0x100, lib.ISP_BYTES)), second.isp)
        self.assertEqual(bytes(m.mem_read(STILL_VA+0x48, 32)), second.id)


if __name__ == '__main__':
    unittest.main()
