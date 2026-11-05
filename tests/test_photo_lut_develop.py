#!/usr/bin/env python3
"""Log looks through the native colour builds in Unicorn: the compiled BL wrappers around the still-develop
and live-view builders, the two provider lanes they publish and the shared colour unit's lane hooks serving
them (needs unicorn; no firmware or compiler)."""
import struct
import unittest

try:
    from unicorn import Uc, UcError, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc = None

from photo_lut_fixtures import CODE_BYTES, LANE_BYTES, LIBRARY, PRIVATE_BYTES, STATE, desire, isp_look, native_code
from z8colour.patcher import CODE as COLOUR, CONFIG, LOG_TYPE
from z8log.patcher import colour_tables
from z8patch.compiled import load
from z8photolut import library as lib
from z8photolut.patcher import BUILD_CALLS, CODE, LANE_RECORDS, PROVIDER, TABLES

CYCLE = ('TITLE "Cycle"\nLUT_3D_SIZE 2\n'+''.join(f'{g} {b} {r}\n' for b in (0, 1) for g in (0, 1) for r in (0, 1))).encode()
STOP, STACK = 0x65000000, 0xA20008000
STOCK = dict(rgb=0x441A1010, nlg=0x441A2282, m2=0x441A56A8, m1=0x441A0A4A, cpxs=0x441A66AC)
SENTINEL = {i: 0x5150_0000_0000+i for i in range(19, 29)}
THUNK = {entry: native for _, native, entry in BUILD_CALLS}


class Pipeline:
    """One native pipeline's two builds as C3.11 lays them out (observed QEMU addresses)."""
    def __init__(self, name, params_m, params_c, settings_at, context_m, context_c, record, caller=None):
        self.name, self.params_m, self.params_c = name, params_m, params_c
        self.settings_at, self.context_m, self.context_c, self.record, self.caller = settings_at, context_m, context_c, record, caller

    @property
    def settings(self):
        return self.settings_at


# Still develop: S = [x0+0x20] in pool image RAM's cached alias, context [x0+0x30]; R = [x0+8] at the curve build.
STILL = Pipeline('still', 0xA03F33D08, 0xA03F34000, 0xC427A71D8, 0xA0436C568, 0xA044FC580, 0xC427A8000)
# Live view: each update message carries S (+0x98) and R (+0x1fe0); messages come from a ring in the main arena's
# cached alias (QEMU C3.11: e3ff04800 + n * 0x14000) and from the heap. Context [x0+0x18]; caller block in x20
# -> the update's live-view object.
LIVE_TASK, HEAP_TASK, VIEW = 0xE3FF04800, 0xA10D05190, 0xA03B60000
LIVE = Pipeline('live', 0xA03F330B8, 0xA03F332E8, LIVE_TASK+0x98, 0xA04088200, 0xA04236258, LIVE_TASK+0x1FE0, 0xA06DA8F00)


@unittest.skipIf(Uc is None, 'Unicorn is required for the colour-build checks')
class DevelopLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.symbols, cls.metadata = native_code()
        colour, profile = load('colour', 'prebuilt')
        cls.colour = colour
        cls.colour_entries = {k: int(v['va'], 16) for k, v in profile['symbols'].items()}

    def machine(self, looks, selected, tone=0, mode=6, publishing=False, log_type=None):
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for at, size in [(CODE, CODE_BYTES), (0x40980000, 0x10000), (LIBRARY, 0x40000), (STATE, PRIVATE_BYTES),
                         (0xA03D48000, 0x1000), (0xA03D5C000, 0x1000), (0xA03F33000, 0x2000), (0xC427A7000, 0x2000),
                         (0xA0436C000, 0x1000), (0xA044FC000, 0x1000), (0xA04088000, 0x1000), (0xA04236000, 0x1000),
                         (LIVE_TASK & ~0xFFF, 0x3000), (HEAP_TASK & ~0xFFF, 0x3000), (VIEW, 0x1000), (0xA06DA8000, 0x1000), (0x41112000, 0x1000),
                         (0x41116000, 0x5000), (STOP, 0x1000), (0xA20000000, 0x10000)]:
            m.mem_map(at, size)
        m.mem_write(CODE, self.code)
        m.mem_write(COLOUR, self.colour)
        m.mem_write(CONFIG, struct.pack('<Q10QQ', LOG_TYPE if log_type is not None else 0,
                                        *(colour_tables() if log_type is not None else [0]*10), PROVIDER))
        if log_type is not None:
            m.mem_write(LOG_TYPE, bytes([log_type]))
        layout = self.metadata['runtime_layout']
        m.mem_write(STATE, struct.pack('<Q6I', 0x32545354554c385a, 4, layout['state_bytes'], 0, 256, 1, 0))
        m.mem_write(LIBRARY, lib.Library(looks).runtime())
        desire(m, layout, selected)
        if publishing:
            m.mem_write(STATE+layout['card_offset']+12, struct.pack('<I', 1))
        for p in (STILL, LIVE):
            m.mem_write(p.settings+0xBC, struct.pack('<I', tone))
            m.mem_write(p.record+0x31, bytes([tone]))
        m.mem_write(STILL.params_m+0x20, struct.pack('<Q', STILL.settings))
        m.mem_write(STILL.params_m+0x30, struct.pack('<Q', STILL.context_m))
        m.mem_write(STILL.params_c+0x08, struct.pack('<Q', STILL.record))
        m.mem_write(STILL.params_c+0x30, struct.pack('<Q', STILL.context_c))
        m.mem_write(LIVE.params_m+0x10, struct.pack('<Q', LIVE.settings))
        m.mem_write(LIVE.params_m+0x18, struct.pack('<Q', LIVE.context_m))
        m.mem_write(LIVE.params_c+0x08, struct.pack('<Q', LIVE.record))
        m.mem_write(LIVE.params_c+0x30, struct.pack('<Q', LIVE.context_c))
        m.mem_write(LIVE.caller, struct.pack('<Q', VIEW))
        self.set_mode(m, mode)
        for thunk in THUNK.values():
            m.mem_write(thunk, bytes.fromhex('c0035fd6'))             # ret: the native builder stub
        self.during = {}
        self.records = {STILL.settings: STILL.record, LIVE.settings: LIVE.record, HEAP_TASK+0x98: HEAP_TASK+0x1FE0}

        def builder(uc, at, size, user):
            """Record what the build sees, then do what the native M1/M2 builders do to R: name it at
            context +0xb8 and pack S+0xbc into R+0x31 (0x411135f4)."""
            args = [uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(4)]
            q = lambda at: struct.unpack('<Q', uc.mem_read(at, 8))[0]
            still = at in (THUNK['z8_photo_lut_still_m_entry'], THUNK['z8_photo_lut_still_curve_entry'])
            p = STILL if still else LIVE
            if at in (THUNK['z8_photo_lut_still_m_entry'], THUNK['z8_photo_lut_live_m_entry']):
                settings = q(args[0]+(0x20 if still else 0x10))
                record = self.records.get(settings, p.record)       # the message's own R
                try:
                    tone = struct.unpack('<I', uc.mem_read(settings+0xBC, 4))[0]
                except UcError:                                     # S outside memory: no native build to model
                    tone = None
                self.during[at] = dict(args=args, tone=tone, provider=struct.unpack('<14Q', uc.mem_read(PROVIDER, 112)))
                if tone is not None:
                    uc.mem_write(p.context_m+0xB8, struct.pack('<Q', record))
                    uc.mem_write(record+0x31, bytes([tone & 0xFF]))
            else:
                self.during[at] = dict(args=args, record_tone=uc.mem_read(q(args[0]+8)+0x31, 1)[0],
                                       provider=struct.unpack('<14Q', uc.mem_read(PROVIDER, 112)))
        for thunk in THUNK.values():
            m.hook_add(UC_HOOK_CODE, builder, begin=thunk, end=thunk)
        return m

    def set_mode(self, m, mode):
        m.mem_write(VIEW+0x10, struct.pack('<I', mode))

    def wrapper(self, m, entry, params, caller=None):
        for i, value in SENTINEL.items():
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        if caller is not None:
            m.reg_write(regs.UC_ARM64_REG_X20, caller)
        m.reg_write(regs.UC_ARM64_REG_X0, params)
        m.reg_write(regs.UC_ARM64_REG_X1, 0xA03F33D40)
        m.reg_write(regs.UC_ARM64_REG_X2, 0x7777)
        m.reg_write(regs.UC_ARM64_REG_X3, 0xA06DA8EE8)                  # the curve builders read [x3]
        m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        m.reg_write(regs.UC_ARM64_REG_X30, STOP)
        m.emu_start(self.symbols[entry], STOP, count=2_000_000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_SP), STACK)
        for i, value in SENTINEL.items():
            if i == 20 and caller is not None:
                value = caller
            self.assertEqual(m.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')), value, f'x{i} not preserved')

    def build(self, m, p):
        """One update of pipeline p: its M1/M2 build, then its curve build."""
        self.wrapper(m, f'z8_photo_lut_{p.name}_m_entry', p.params_m, p.caller)
        self.wrapper(m, f'z8_photo_lut_{p.name}_curve_entry', p.params_c)
        return self.during[THUNK[f'z8_photo_lut_{p.name}_m_entry']], self.during[THUNK[f'z8_photo_lut_{p.name}_curve_entry']]

    def call(self, m, name, *args):
        for i, value in enumerate(args):
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        m.reg_write(regs.UC_ARM64_REG_X30, STOP)
        m.emu_start(self.symbols[name], STOP, count=2_000_000)
        return m.reg_read(regs.UC_ARM64_REG_X0)

    def lane(self, m, lane):
        at = LANE_RECORDS+lane*LANE_BYTES
        sequence, choice, streak, mode = struct.unpack('<4I', m.mem_read(at, 16))
        counters = struct.unpack('<6Q', m.mem_read(at+0x50, 48))
        return dict(choice=choice, streak=streak, mode=mode, id=bytes(m.mem_read(at+0x10, 32)),
                    builds=counters[:4], refused=counters[4], other_modes=counters[5])

    def hook(self, m, entry, values, until):
        for i in range(31):
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), values.get(i, 0x1111_0000_0000+i))
        m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        m.reg_write(regs.UC_ARM64_REG_NZCV, 0x60000000)
        m.emu_start(self.colour_entries[entry], until, count=1000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_SP), STACK)
        return {i: m.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(31)}

    def served_tables(self, m, p, tone=1):
        """Run the five lane hooks (photo-lut compositions enter the sites through them) as pipeline p's
        builds would, with the provider block as it is now; return the tables they select."""
        rgb = self.hook(m, 'colour_lane_rgb_hook', {8: STOCK['rgb'], 9: tone, 19: p.context_c}, 0x4111A50C)[8]
        nlg = self.hook(m, 'colour_lane_nlg_hook', {2: 0x1CA, 9: tone, 10: STOCK['nlg'], 19: p.context_c}, 0x4111A6D8)[1]
        m2 = self.hook(m, 'colour_lane_m2_hook', {8: tone, 9: STOCK['m2'], 11: 0x12, 19: p.context_m}, 0x41116CB8)[11]
        m1 = self.hook(m, 'colour_lane_m1_hook', {11: tone, 13: 2, 14: STOCK['m1'], 19: p.context_m}, 0x41116014)[14]+2*0x12
        m.mem_write(p.context_c+0x98, struct.pack('<Q', p.record))
        m.mem_write(p.context_c+0xB0, struct.pack('<Q', STOCK['cpxs']))
        record_tone = m.mem_read(p.record+0x31, 1)
        m.mem_write(p.record+0x31, bytes([tone]))
        self.hook(m, 'colour_lane_cpxs_hook', {19: p.context_c}, 0x4111A19C)
        m.mem_write(p.record+0x31, bytes(record_tone))
        cpxs = struct.unpack('<Q', m.mem_read(p.context_c+0xB0, 8))[0]
        return dict(rgb=rgb, nlg=nlg, m2=m2, m1=m1, cpxs=cpxs)

    @staticmethod
    def copy(lane):
        t = TABLES+lane*0x900
        return dict(rgb=t, nlg=t+0x560, m2=t+0x730, m1=t+0x750, cpxs=t+0x770)

    NATIVE_NLOG = dict(rgb=STOCK['rgb'], nlg=STOCK['nlg']+0x1CA, m2=STOCK['m2']+0x12, m1=STOCK['m1']+2*0x12, cpxs=STOCK['cpxs'])

    def assert_native(self, m, tone=0):
        for p in (STILL, LIVE):
            self.assertEqual(struct.unpack('<I', m.mem_read(p.settings+0xBC, 4))[0], tone, p.name+' S+0xbc')
            self.assertEqual(m.mem_read(p.record+0x31, 1)[0], tone & 0xFF, p.name+' R+0x31')
        provider = struct.unpack('<14Q', m.mem_read(PROVIDER, 112))
        self.assertEqual((provider[0], provider[1], provider[7], provider[8]), (0, 0, 0, 0), 'no build context named')

    def test_a_log_look_develops_stills_through_lane_zero(self):
        look = isp_look('Log', residual=CYCLE, m1=[1000, 30, -6, 12, 990, 22, 0, -20, 1044])
        m = self.machine((isp_look('Other', basis='v-log/v-gamut'), look), 2)
        built, curve = self.build(m, STILL)
        # The builders get their original arguments; the M1/M2 build sees S+0xbc = 1, the curve build R+0x31 = 1.
        self.assertEqual(built['args'], [STILL.params_m, 0xA03F33D40, 0x7777, 0xA06DA8EE8])
        self.assertEqual(curve['args'], [STILL.params_c, 0xA03F33D40, 0x7777, 0xA06DA8EE8])
        self.assertEqual((built['tone'], curve['record_tone']), (1, 1))
        self.assertEqual(built['provider'][:2]+curve['provider'][:2], (STILL.context_m, 0, 0, STILL.context_c))
        self.assertEqual(built['provider'][7:9]+curve['provider'][7:9], (0, 0, 0, 0))
        copy = self.copy(0)
        self.assertEqual(built['provider'][2:7], tuple(copy.values()))
        self.assertEqual(bytes(m.mem_read(copy['rgb'], lib.ISP_BYTES)), look.isp)
        # Afterwards S+0xbc, the packed R+0x31 and both contexts are native again.
        self.assert_native(m)
        lane = self.lane(m, 0)
        self.assertEqual((lane['id'], lane['choice'], lane['streak'], lane['builds'], lane['refused']), (look.id, 2, 1, (1, 1, 1, 1), 0))
        # The lane hooks serve exactly these tables to these builds while they run.
        m.mem_write(PROVIDER, struct.pack('<7Q', STILL.context_m, STILL.context_c, *copy.values()))
        served = self.served_tables(m, STILL)
        self.assertEqual(served, copy)
        isp = lib.unpack_isp(look.isp)
        self.assertEqual(struct.unpack('<9h', m.mem_read(served['m1'], 18)), isp['m1'])
        self.assertEqual(struct.unpack('<687H', m.mem_read(served['rgb'], 1374)), isp['rgb'])
        self.assertEqual(struct.unpack('<192h', m.mem_read(served['cpxs'], 384)), isp['cpxs'])
        # Live view, another context, keeps the native N-Log selection.
        self.assertEqual(self.served_tables(m, LIVE), self.NATIVE_NLOG)

    def test_photo_live_view_is_served_through_lane_one(self):
        look = isp_look('Log', residual=CYCLE, m1=[990, 0, 30, 0, 1024, 0, 12, 0, 1000])
        for mode in (6, 9, 11):
            with self.subTest(mode=mode):
                m = self.machine((look,), 1, mode=mode)
                built, curve = self.build(m, LIVE)
                self.assertEqual(built['args'], [LIVE.params_m, 0xA03F33D40, 0x7777, 0xA06DA8EE8])
                self.assertEqual(curve['args'], [LIVE.params_c, 0xA03F33D40, 0x7777, 0xA06DA8EE8])
                self.assertEqual((built['tone'], curve['record_tone']), (1, 1))
                self.assertEqual(built['provider'][7:9]+curve['provider'][7:9], (LIVE.context_m, 0, 0, LIVE.context_c))
                self.assertEqual(built['provider'][:2]+curve['provider'][:2], (0, 0, 0, 0))
                copy = self.copy(1)
                self.assertEqual(built['provider'][9:14], tuple(copy.values()))
                self.assertEqual(bytes(m.mem_read(copy['rgb'], lib.ISP_BYTES)), look.isp)
                self.assert_native(m)
                lane = self.lane(m, 1)
                self.assertEqual((lane['id'], lane['choice'], lane['mode'], lane['builds']), (look.id, 1, mode, (1, 1, 1, 1)))
                m.mem_write(PROVIDER+0x38, struct.pack('<7Q', LIVE.context_m, LIVE.context_c, *copy.values()))
                self.assertEqual(self.served_tables(m, LIVE), copy)
                self.assertEqual(self.served_tables(m, STILL), self.NATIVE_NLOG)
                # Every update rebuilds: the streak counts consecutive served builds of this look.
                self.build(m, LIVE)
                self.build(m, LIVE)
                self.assertEqual(self.lane(m, 1)['streak'], 3)
                self.assert_native(m)

    def test_live_update_messages_from_the_heap_are_served_too(self):
        look = isp_look('Log', residual=CYCLE)
        m = self.machine((look,), 1)
        heap = Pipeline('live', LIVE.params_m, LIVE.params_c, HEAP_TASK+0x98, LIVE.context_m, LIVE.context_c, HEAP_TASK+0x1FE0, LIVE.caller)
        for p in (LIVE, heap, LIVE):
            m.mem_write(p.settings+0xBC, bytes(4))
            m.mem_write(p.record+0x31, b'\x00')
            m.mem_write(LIVE.params_m+0x10, struct.pack('<Q', p.settings))
            m.mem_write(LIVE.params_c+0x08, struct.pack('<Q', p.record))
            built, curve = self.build(m, LIVE)
            self.assertEqual((built['tone'], curve['record_tone'], built['provider'][7], curve['provider'][8]),
                             (1, 1, LIVE.context_m, LIVE.context_c))
            self.assertEqual(struct.unpack('<I', m.mem_read(p.settings+0xBC, 4))[0], 0)
            self.assertEqual(m.mem_read(p.record+0x31, 1)[0], 0)
        lane = self.lane(m, 1)
        self.assertEqual((lane['streak'], lane['builds'], lane['refused']), (3, (3, 3, 3, 3), 0))

    def test_movie_and_other_live_view_modes_stay_native(self):
        look = isp_look('Log', residual=CYCLE)
        for mode in (0, 1, 2, 7, 8, 10, 12, 13, 0x40):
            with self.subTest(mode=mode):
                m = self.machine((look,), 1, mode=mode, tone=1, log_type=1)
                built, curve = self.build(m, LIVE)
                # Movie N-Log (S+0xbc = 1 natively) is left alone and served by log-colour's tables, never by photo-lut.
                self.assertEqual((built['tone'], curve['record_tone']), (1, 1))
                self.assertEqual(built['provider']+curve['provider'], (0,)*28)
                self.assert_native(m, tone=1)
                lane = self.lane(m, 1)
                self.assertEqual((lane['choice'], lane['mode'], lane['builds'], lane['other_modes']), (0, mode, (1, 1, 0, 0), 1))
                tables = colour_tables()
                self.assertEqual(self.served_tables(m, LIVE),
                                 dict(rgb=tables[0], nlg=tables[2], m2=tables[4], m1=tables[6], cpxs=tables[8]))

    def test_still_develop_and_live_view_are_served_at_the_same_time(self):
        first = isp_look('A', residual=CYCLE, m1=[1000, 30, -6, 12, 990, 22, 0, -20, 1044])
        second = isp_look('B', m1=[900, 0, 0, 0, 1024, 0, 0, 0, 1100])
        m = self.machine((first, second), 1)
        # The still develop's M1/M2 build begins (look A), the bank changes to look B, and a live M1/M2
        # build begins on another CPU before the still build ends.
        self.assertEqual(self.call(m, 'z8_photo_lut_still_m_begin', STILL.params_m), 1)
        desire(m, self.metadata['runtime_layout'], 2)
        self.assertEqual(self.call(m, 'z8_photo_lut_live_m_begin', LIVE.params_m, LIVE.caller), 1)
        provider = struct.unpack('<14Q', m.mem_read(PROVIDER, 112))
        self.assertEqual((provider[0], provider[7]), (STILL.context_m, LIVE.context_m))
        self.assertEqual(self.served_tables(m, STILL)['m1'], self.copy(0)['m1'])
        self.assertEqual(self.served_tables(m, LIVE)['m1'], self.copy(1)['m1'])
        self.assertEqual(struct.unpack('<9h', m.mem_read(self.copy(0)['m1'], 18)), lib.unpack_isp(first.isp)['m1'])
        self.assertEqual(struct.unpack('<9h', m.mem_read(self.copy(1)['m1'], 18)), lib.unpack_isp(second.isp)['m1'])
        # Native packing during both builds, then the live build ends first: the still lane is untouched.
        for p in (STILL, LIVE):
            m.mem_write(p.context_m+0xB8, struct.pack('<Q', p.record))
            m.mem_write(p.record+0x31, b'\x01')
        self.call(m, 'z8_photo_lut_live_m_end', LIVE.params_m, 1)
        provider = struct.unpack('<14Q', m.mem_read(PROVIDER, 112))
        self.assertEqual((provider[0], provider[7]), (STILL.context_m, 0))
        self.assertEqual(struct.unpack('<I', m.mem_read(STILL.settings+0xBC, 4))[0], 1)
        self.assertEqual(struct.unpack('<I', m.mem_read(LIVE.settings+0xBC, 4))[0], 0)
        self.assertEqual(self.served_tables(m, STILL)['m1'], self.copy(0)['m1'])
        # Both curve builds overlap the same way.
        self.call(m, 'z8_photo_lut_still_m_end', STILL.params_m, 1)
        self.assertEqual(self.call(m, 'z8_photo_lut_live_curve_begin', LIVE.params_c), 1)
        self.assertEqual(self.call(m, 'z8_photo_lut_still_curve_begin', STILL.params_c), 1)
        self.assertEqual(self.served_tables(m, STILL)['cpxs'], self.copy(0)['cpxs'])
        self.assertEqual(self.served_tables(m, LIVE)['cpxs'], self.copy(1)['cpxs'])
        self.call(m, 'z8_photo_lut_still_curve_end', STILL.params_c, 1)
        self.assertEqual(self.served_tables(m, LIVE)['nlg'], self.copy(1)['nlg'])
        self.call(m, 'z8_photo_lut_live_curve_end', LIVE.params_c, 1)
        self.assert_native(m)
        self.assertEqual((self.lane(m, 0)['id'], self.lane(m, 1)['id']), (first.id, second.id))

    def test_turning_the_look_off_returns_live_view_to_native_at_the_next_build(self):
        look = isp_look('Log', residual=CYCLE)
        m = self.machine((look, lib.look_from_cube(CYCLE, source_name='c.cube', grid=17)), 1)
        self.build(m, LIVE)
        self.assertEqual(self.lane(m, 1)['choice'], 1)
        layout = self.metadata['runtime_layout']
        for label, change in (('off', lambda: desire(m, layout, 0)), ('cpu-3d look', lambda: desire(m, layout, 2)),
                              ('library gone', lambda: m.mem_write(STATE+layout['card_offset'], bytes(8)))):
            with self.subTest(label=label):
                desire(m, layout, 1)
                self.build(m, LIVE)
                change()
                # A curve build alone (no M1/M2 build) is already native and ends the lane's look.
                self.wrapper(m, 'z8_photo_lut_live_curve_entry', LIVE.params_c)
                curve = self.during[THUNK['z8_photo_lut_live_curve_entry']]
                self.assertEqual((curve['record_tone'], curve['provider'][8]), (0, 0))
                self.assertEqual((self.lane(m, 1)['choice'], self.lane(m, 1)['id']), (0, bytes(32)))
                built, curve = self.build(m, LIVE)
                self.assertEqual((built['tone'], built['provider'][7], curve['provider'][8]), (0, 0, 0))
                self.assert_native(m)

    def test_a_curve_build_alone_follows_the_served_look(self):
        look = isp_look('Log', residual=CYCLE)
        m = self.machine((look,), 1)
        self.wrapper(m, 'z8_photo_lut_live_curve_entry', LIVE.params_c)
        self.assertEqual(self.during[THUNK['z8_photo_lut_live_curve_entry']]['provider'][8], 0)
        self.build(m, LIVE)
        for _ in range(2):
            self.wrapper(m, 'z8_photo_lut_live_curve_entry', LIVE.params_c)
            curve = self.during[THUNK['z8_photo_lut_live_curve_entry']]
            self.assertEqual((curve['record_tone'], curve['provider'][8]), (1, LIVE.context_c))
        self.assert_native(m)
        self.assertEqual(self.lane(m, 1)['builds'], (1, 4, 1, 3))
        # The still lane's curve build likewise follows its own M1/M2 build only.
        self.wrapper(m, 'z8_photo_lut_still_curve_entry', STILL.params_c)
        self.assertEqual(self.during[THUNK['z8_photo_lut_still_curve_entry']]['provider'][1], 0)

    def test_other_selections_keep_the_native_builds(self):
        cases = {
            'cpu-3d look': (lambda: (isp_look('Log'), lib.look_from_cube(CYCLE, source_name='c.cube', grid=17)), 2, {}),
            'off': (lambda: (isp_look('Log'),), 0, {}),
            'publication in progress': (lambda: (isp_look('Log'),), 1, dict(publishing=True)),
        }
        for label, (looks, selected, options) in cases.items():
            for p in (STILL, LIVE):
                with self.subTest(label=label, pipeline=p.name):
                    m = self.machine(looks(), selected, tone=7, **options)
                    built, curve = self.build(m, p)
                    self.assertEqual((built['tone'], curve['record_tone']), (7, 7))
                    self.assertEqual(built['provider']+curve['provider'], (0,)*28)
                    self.assert_native(m, tone=7)
                    lane = self.lane(m, 0 if p is STILL else 1)
                    self.assertEqual((lane['id'], lane['builds'], lane['refused']), (bytes(32), (1, 1, 0, 0), 1 if options else 0))

    def test_unreadable_parameters_keep_the_native_builds(self):
        for p, field in ((STILL, 0x20), (LIVE, 0x10)):
            for label, settings in (('S outside RAM', 0x441A0000), ('S misaligned', p.settings+1)):
                with self.subTest(pipeline=p.name, label=label):
                    m = self.machine((isp_look('Log'),), 1)
                    m.mem_write(p.params_m+field, struct.pack('<Q', settings))
                    built, curve = self.build(m, p)
                    self.assertEqual(built['provider']+curve['provider'], (0,)*28)
                    self.assertEqual(self.lane(m, 0 if p is STILL else 1)['refused'], 1)
        # A live-view caller block or object outside RAM reads as no photo mode.
        for caller, view in ((0x441A0000, VIEW), (LIVE.caller, 0x441A0000)):
            m = self.machine((isp_look('Log'),), 1)
            m.mem_write(LIVE.caller, struct.pack('<Q', view))
            self.wrapper(m, 'z8_photo_lut_live_m_entry', LIVE.params_m, caller)
            self.assertEqual(self.during[THUNK['z8_photo_lut_live_m_entry']]['provider'][7], 0)
            self.assertEqual(self.lane(m, 1)['other_modes'], 1)

    def test_a_native_tone_is_restored_and_the_look_is_recopied_each_build(self):
        first, second = isp_look('A', residual=CYCLE), isp_look('B', m1=[900, 0, 0, 0, 1024, 0, 0, 0, 1100])
        for p, lane in ((STILL, 0), (LIVE, 1)):
            with self.subTest(pipeline=p.name):
                m = self.machine((first, second), 1, tone=3)
                self.build(m, p)
                self.assertEqual(bytes(m.mem_read(self.copy(lane)['rgb'], lib.ISP_BYTES)), first.isp)
                desire(m, self.metadata['runtime_layout'], 2)
                built, curve = self.build(m, p)
                self.assertEqual((built['tone'], curve['record_tone']), (1, 1))
                self.assert_native(m, tone=3)
                self.assertEqual(bytes(m.mem_read(self.copy(lane)['rgb'], lib.ISP_BYTES)), second.isp)
                self.assertEqual((self.lane(m, lane)['id'], self.lane(m, lane)['streak']), (second.id, 1))
                self.assertEqual(bytes(m.mem_read(self.copy(1-lane)['rgb'], lib.ISP_BYTES)), bytes(lib.ISP_BYTES))


if __name__ == '__main__':
    unittest.main()
