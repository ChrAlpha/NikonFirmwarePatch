"""Execute the compiled card-session library path: worker, publication, banks and menu text.

The GUI menu action and the PhotoCRD worker run as separate tasks of the
priority scheduler. The pinned C3.11 file calls (the pixel-shift and
pre-capture card ABI) are scripted two-card stubs, so this is a contract test
of our firmware logic, not evidence of native FS, menu drawing or timing.
Menu text and its language tables come from the real patcher output; the
image text and its writable alias share one buffer, as the RAM copy does.
"""
import binascii
import ctypes
import hashlib
import os
from pathlib import Path
import struct
import unittest

from z8photolut import library as lib
from z8photolut.patcher import BASE, CODE, LUT, build, compile_code
from test_photo_lut_hooks import NativeScheduler, Uc
from test_photo_lut_library import COOL, IDENTITY, SWAP, WARM, cube, look

try:
    from unicorn import UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    pass

ROM_SHA256 = '2b43a31c9de838f84b12416de4348a7004e65be07f853ec56ec2b10f3a8e3853'
STATE = 0xa101f0000
ALIAS = 0x9cf600000
TEXT, TEXT_END = 0x408d0000, 0x40950000
DIRECTORY, BLOBS = 0x4327c5a0, 0x439daf58
BANKS = 0xa03d482ba
FS = dict(open=0x402daa4c, close=0x402daaac, read=0x402dab4c, seek=0x402dac24, exists=0x402db1ac)
FLUSH, SAVE, ROW_DISABLE = 0x40d2b9c8, 0x41012bf4, 0x403b1ed0
SET_CURSOR, SET_MARK, MENU_STATE = 0x403b0048, 0x403b00b8, 0x1d
GUI_STACK, WORKER_STACK = 0xa03015000, 0xa03017800
FIRST_STRING = 3137


@unittest.skipIf(Uc is None, 'Unicorn is required for the card-session contract')
class PhotoLutCardSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied = os.environ.get('Z8_PHOTO_LUT_ROM')
        if not supplied or not Path(supplied).is_file():
            raise unittest.SkipTest('Set Z8_PHOTO_LUT_ROM to the original C3.11 z8-main-payload.bi')
        rom = Path(supplied).read_bytes()
        if hashlib.sha256(rom).hexdigest() != ROM_SHA256:
            raise AssertionError('C3.11 payload SHA256 differs')
        stock = rom+binascii.crc_hqx(rom, 0).to_bytes(2, 'big')
        cls.builtin = lib.Library((look(WARM), look(COOL), look(SWAP)), 1)
        cls.tag = 0                                          # built-in library marker
        cls.image, _ = build(stock, cls.builtin.runtime(), '03.92', 'u1')
        cls.code, cls.symbols, cls.metadata = compile_code()

    # ---- machine -------------------------------------------------------
    def machine(self, cards, *, banks=None, power_paused=False, fail_read=()):
        banks = banks or ((1, self.tag),)*5
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        image = self.image
        def page(va, size):
            first = va & ~4095
            m.mem_map(first, ((va+size+4095) & ~4095)-first)
            m.mem_write(first, image[first-BASE:first-BASE+((va+size+4095) & ~4095)-first])
        page(CODE, 0x10000); page(LUT, 0x200000); page(DIRECTORY, 36*8); page(BLOBS, 36*8)
        tables = [struct.unpack_from('<Q', image, DIRECTORY-BASE+8*l)[0] for l in range(36)]
        self.assertTrue(all(TEXT <= t < TEXT_END for t in tables))
        # One host buffer for the low text VA and its writable RAM alias.
        self.text = ctypes.create_string_buffer(image[TEXT-BASE:TEXT_END-BASE], TEXT_END-TEXT)
        m.mem_map_ptr(TEXT, TEXT_END-TEXT, 3, ctypes.addressof(self.text))
        m.mem_map_ptr(TEXT+ALIAS, TEXT_END-TEXT, 3, ctypes.addressof(self.text))
        for at in (0x402da000, 0x402db000, 0x403ae000, 0x403b0000, 0x403b1000, 0x40d2c000, 0x40d2d000, 0x41012000, 0x65000000,
                   0xa03d48000, 0xa03f31000):
            m.mem_map(at, 4096)
        for at in (*FS.values(), ROW_DISABLE, SAVE, 0x403ae0f8, 0x403b0048, 0x403b00b8):
            m.mem_write(at, bytes.fromhex('c0035fd6'))
        m.mem_map(STATE, 0x2000)
        m.mem_map(0xa03000000, 0x20000)
        for copy, (index, tag) in enumerate(banks):
            m.mem_write(BANKS+copy*0x2e0, bytes([index, tag]))
        self.scheduler = NativeScheduler(m, (GUI_STACK, WORKER_STACK, 0xa0301a000, 0xa0301c800, 0xa0301f000))
        m.mem_map(0xa11010000, 0x1000000)        # heap continuation for card buffers
        m.mem_write(FLUSH, bytes.fromhex('c0035fd6'))
        self.saves, self.disabled, self.fs_calls, self.files, self.cursor = [], [], [], {}, []
        def service(uc, at, size, user):
            x = lambda i: uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}'))
            def path(address):
                return bytes(uc.mem_read(address, 32)).split(b'\0', 1)[0].decode()
            if at == FS['exists']:
                self.fs_calls.append(('exists', path(x(0))))
                uc.reg_write(regs.UC_ARM64_REG_X0, 0 if path(x(0)) in cards else (1 << 64)-1)
            elif at == FS['open']:
                name = path(x(0)); fd = 3+len(self.files)
                self.files[fd] = dict(name=name, position=0)
                self.fs_calls.append(('open', name))
                uc.reg_write(regs.UC_ARM64_REG_X0, fd)
            elif at == FS['seek']:
                record = self.files[x(0)]
                record['position'] = len(cards[record['name']]) if x(2) == 2 else x(1)
                uc.mem_write(x(3), struct.pack('<I', record['position']))
            elif at == FS['read']:
                record = self.files[x(0)]
                data = cards[record['name']][record['position']:record['position']+x(2)]
                failed = record['name'] in fail_read
                if not failed:
                    uc.mem_write(x(1), data); record['position'] += len(data)
                uc.mem_write(x(3), struct.pack('<I', 0 if failed else len(data)))
                uc.reg_write(regs.UC_ARM64_REG_X0, 0x80000001 if failed else 0)
            elif at == FS['close']:
                self.fs_calls.append(('close', self.files[x(0)]['name']))
                uc.reg_write(regs.UC_ARM64_REG_X0, 0)
            elif at == SAVE:
                self.saves.append(self.scheduler.round)
            elif at == ROW_DISABLE:
                self.disabled.append((x(1), x(2)))
            elif at in (SET_CURSOR, SET_MARK):
                self.cursor.append(('cursor' if at == SET_CURSOR else 'mark', x(1)-0x35b5))
        for first, end in ((0x402da000, 0x402dbfff), (0x403b0000, 0x403b1fff), (0x41012000, 0x41012fff)):
            m.hook_add(UC_HOOK_CODE, service, begin=first, end=end)
        self.m = m
        # Menu page whose cursor rests on the "Load LUTs from card" row.
        context, page, rows_list, rows = 0xa03001000, 0xa03002000, 0xa03003000, 0xa03004000
        m.mem_write(context, struct.pack('<Q', page))
        m.mem_write(page+0x50, struct.pack('<Q', rows_list))
        m.mem_write(rows_list+0x10, struct.pack('<Q', rows))
        for row in range(10):
            m.mem_write(rows+row*44+24, struct.pack('<I', 0x35b5+row))
        self.context, self.page = context, page
        # The real boot hook validates the built-in library and writes the menu text.
        self.call('z8_photo_lut_boot')
        layout = self.metadata['runtime_layout']
        self.assertEqual(struct.unpack('<6I', m.mem_read(STATE+8, 24))[4:], (1, 0))   # ready, no error
        if power_paused:
            m.mem_write(STATE+layout['preview_offset']+layout['power_paused_offset'], struct.pack('<I', 1))
        return m

    def call(self, name, *arguments):
        address = self.symbols[name]
        for i, value in enumerate(arguments):
            self.m.reg_write(getattr(regs, f'UC_ARM64_REG_X{i}'), value)
        self.m.reg_write(regs.UC_ARM64_REG_SP, GUI_STACK)
        self.m.reg_write(regs.UC_ARM64_REG_X30, NativeScheduler.STOP)
        self.m.emu_start(address, NativeScheduler.STOP, count=400000000)
        return self.m.reg_read(regs.UC_ARM64_REG_X0)

    def select(self, row, target=0x17):
        """OK on an option row; returns the dispatcher's transition target afterwards."""
        self.m.mem_write(self.page+0x58, struct.pack('<H', row))
        self.m.mem_write(self.context+0xc, struct.pack('<I', target))
        task = self.scheduler.task('GUI', 1, 20, self.symbols['z8_photo_lut_menu_ok'], (self.context,))
        # One scheduler tick is 1 ms; 200k instructions is still far below a
        # camera core, so the 3 s GUI wait is not shortened by this model.
        self.scheduler.run(lambda: task['finished'], tick=200000, rounds=6000)
        return struct.unpack('<I', self.m.mem_read(self.context+0xc, 4))[0]

    # ---- observations --------------------------------------------------
    def banks(self):
        return [tuple(self.m.mem_read(BANKS+copy*0x2e0, 2)) for copy in range(5)]

    def card(self):
        layout = self.metadata['runtime_layout']
        at = STATE+layout['jpeg_worker_offset']+72+8*112
        fields = struct.unpack('<2Q8IQ4I3Q', self.m.mem_read(at, 96))
        return dict(zip(('active', 'staged', 'readers', 'publishing', 'request', 'result', 'wanted', 'status',
                         'detail', 'loaded', 'worker', 'worker_id', 'worker_state', 'event_ready', 'reserved',
                         'attempts', 'publications', 'refusals'), fields))

    def strings(self, language):
        """Decode our strings as the stock tables define them: start offsets, length to the next."""
        table = struct.unpack('<Q', self.m.mem_read(DIRECTORY+8*language, 8))[0]
        blob = struct.unpack('<Q', self.m.mem_read(BLOBS+8*language, 8))[0]
        offsets = struct.unpack('<13i', self.m.mem_read(table+4*FIRST_STRING, 52))
        text = []
        for start, end in zip(offsets, offsets[1:]):
            self.assertLessEqual(start, end)
            text.append(bytes(self.m.mem_read(blob+start, end-start)).decode('utf-16le'))
        return text

    def assert_menu(self, names, status, language=0):
        text = self.strings(language)
        self.assertEqual(text[3], status)
        self.assertEqual(text[4:], [*(f'{i}. {n}' for i, n in enumerate(names, 1)), *['-']*(8-len(names))])

    # ---- cases ---------------------------------------------------------
    def test_boot_text_is_the_builtin_library_in_every_language(self):
        self.machine({})
        for language, status in [(0, 'Load LUTs from card'), (8, '从存储卡载入 LUT'), (9, '從記憶卡載入 LUT'), (35, 'Load LUTs from card')]:
            with self.subTest(language=language):
                self.assert_menu(['Warm', 'Cool', 'Swap'], status, language)
                self.assertIn(self.strings(language)[2], ('Off', '关闭', '關閉'))

    def test_one_card_publishes_and_remaps_every_bank_by_transform_identity(self):
        card = lib.Library((look(cube('Mono', lambda r, g, b: (g, g, g))), look(WARM, '暖色 "Warm"'), look(IDENTITY)))
        # current: Warm (kept, moves to 2); A: Cool (absent -> Off); B: Off; C: a stale card
        # session tag from an earlier power-on; D: Swap (absent -> Off).
        t = self.tag
        self.machine({'A:\\Z8LUTLIB.BIN': card.encode()}, banks=((1, t), (2, t), (0, t), (2, 7), (3, t)))
        # The load re-enters this page (state 0x1D) and asks for the cursor on its result row.
        self.assertEqual(self.select(1), MENU_STATE)
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.cursor, [('cursor', 1), ('mark', 3)])        # result row; look 2 -> row 3
        self.cursor.clear()
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.cursor[-1], ('mark', 3))
        state = self.card()
        self.assertNotEqual(state['active'], 0)
        self.assertEqual((state['status'], state['detail'], state['loaded'], state['publications']), (1, 1, 3, 1))
        self.assertEqual(self.banks(), [(2, 1), (0, 1), (0, 1), (0, 1), (0, 1)])
        self.assertEqual(len(self.saves), 1)
        self.assert_menu(['Mono', '暖色 "Warm"', 'Identity'], 'Card LUTs: 3 loaded (slot 1)')
        self.assert_menu(['Mono', '暖色 "Warm"', 'Identity'], '存储卡 LUT：已载入 3 个（卡槽 1）', 8)
        published = bytes(self.m.mem_read(state['active'], len(card.runtime())))
        self.assertEqual(published[:28]+published[32:], card.runtime()[:28]+card.runtime()[32:])
        self.assertEqual(struct.unpack_from('<I', published, 28)[0], 1)      # first session marker
        # A second load maps from the session library and frees its buffer.
        self.select(1)
        self.assertEqual(self.card()['publications'], 2)
        self.assertEqual(self.banks()[0], (2, 2))
        self.assertIn(state['active'], [a for a in self.scheduler.allocations if a])

    def test_identical_cards_load_and_report_both_slots(self):
        raw = lib.Library((look(SWAP),)).encode()
        self.machine({'A:\\Z8LUTLIB.BIN': raw, 'B:\\Z8LUTLIB.BIN': raw})
        self.select(1)
        self.assertEqual(self.card()['status'], 1)
        self.assert_menu(['Swap'], 'Card LUTs: 1 loaded (slot 1+2)')
        self.assertEqual(self.banks(), [(0, 1)]*5)

    def refused(self, cards, status, text, **options):
        t = self.tag
        before_banks = (1, t), (2, t), (0, t), (3, t), (1, t)
        self.machine(cards, banks=before_banks, **options)
        self.select(1)
        state = self.card()
        self.assertEqual((state['active'], state['publications'], state['status']), (0, 0, status))
        self.assertEqual(self.banks(), [tuple(b) for b in before_banks])
        self.assertEqual(self.saves, [])
        self.assert_menu(['Warm', 'Cool', 'Swap'], text)

    def test_conflicting_cards_are_refused_without_change(self):
        self.refused({'A:\\Z8LUTLIB.BIN': lib.Library((look(SWAP),)).encode(),
                      'B:\\Z8LUTLIB.BIN': lib.Library((look(SWAP, 'Other name'),)).encode()},
                     4, 'Slots 1 and 2 differ; unchanged')

    def test_an_invalid_file_blocks_even_with_a_valid_other_card(self):
        good = lib.Library((look(SWAP),)).encode()
        bad = bytearray(good); bad[40] ^= 1      # library digest
        self.refused({'A:\\Z8LUTLIB.BIN': good, 'B:\\Z8LUTLIB.BIN': bytes(bad)}, 3,
                     'Slot 2 Z8LUTLIB.BIN invalid; unchanged')

    def test_truncated_tagged_missing_and_unreadable_files_are_refused(self):
        good = lib.Library((look(SWAP),)).encode()
        tagged = bytearray(good); tagged[28] = 5
        cases = [({'A:\\Z8LUTLIB.BIN': good[:2000]}, 3, 'Slot 1 Z8LUTLIB.BIN invalid; unchanged', {}),
                 ({'A:\\Z8LUTLIB.BIN': bytes(tagged)}, 3, 'Slot 1 Z8LUTLIB.BIN invalid; unchanged', {}),
                 ({}, 2, 'No Z8LUTLIB.BIN on either card', {}),
                 ({'B:\\Z8LUTLIB.BIN': good}, 5, 'Slot 2 read error; unchanged', dict(fail_read=('B:\\Z8LUTLIB.BIN',)))]
        for cards, status, text, options in cases:
            with self.subTest(text=text, cards=sorted(cards)):
                self.refused(cards, status, text, **options)

    def test_empty_card_library_clears_looks_and_disables_rows(self):
        self.machine({'A:\\Z8LUTLIB.BIN': lib.Library(()).encode()})
        self.select(1)
        self.assertEqual(self.banks(), [(0, 1)]*5)
        self.assert_menu([], 'Card LUTs: 0 loaded (slot 1)')
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.disabled, [(look+1, 1) for look in range(1, 9)])   # rows after Off and Load
        # Selecting a disabled look row changes nothing and keeps the stock target.
        self.assertEqual(self.select(2), 0x17)
        self.assertEqual(self.banks()[0], (0, 1))

    def test_power_down_touches_no_card_and_publishes_nothing(self):
        self.machine({'A:\\Z8LUTLIB.BIN': lib.Library((look(SWAP),)).encode()}, power_paused=True)
        self.select(1)
        self.assertEqual(self.fs_calls, [])
        self.assertEqual((self.card()['active'], self.saves), (0, []))
        self.assert_menu(['Warm', 'Cool', 'Swap'], 'Card busy; try again')

    def test_patched_image_keeps_identity_marker_and_default_bytes(self):
        # The requested version.tag reaches the runtime identity and the trailer byte-for-byte.
        self.assertEqual(self.image[0x14eaa90:0x14eaa98], b'03.92.u1')
        self.assertEqual(self.image[-11:-3], b'03.92.u1')
        self.assertEqual(self.image[0x3ece4e2:0x3ece4e4], bytes([1, 0]))           # group6 default: look 1, built-in
        self.assertEqual(struct.unpack_from('<I', self.image, LUT-BASE+28)[0], 0)   # built-in marker in the image

    def test_selection_pairs_never_mix_index_and_tag(self):
        self.machine({'A:\\Z8LUTLIB.BIN': lib.Library((look(SWAP), look(COOL), look(WARM))).encode()})
        self.select(1)
        for pair, expected in [((3, self.tag), 0), ((3, 1), 3), ((4, 1), 0), ((2, 1), 2), ((2, 9), 0)]:
            with self.subTest(pair=pair):
                self.m.mem_write(BANKS, bytes(pair))
                self.assertEqual(self.call('choice'), expected)


if __name__ == '__main__':
    unittest.main()
