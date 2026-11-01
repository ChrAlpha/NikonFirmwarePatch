"""Execute the compiled card library path: automatic restore, explicit Load,
publication, bank records and menu text.

The GUI menu action, the settings-task S1 hook and the PhotoCRD worker run as
separate tasks of the priority scheduler. The pinned C3.11 file calls (the
pixel-shift and pre-capture card ABI) are scripted two-card stubs, so this is
a contract test of our firmware logic, not evidence of native FS, menu
drawing or timing. The stored record itself is read and written on the
original settings code in test_photo_lut_persistence.
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
from photo_lut_fixtures import record_bytes
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
OWNED, BANK_INDEX = 0xa03d482ba, 0xa03d48164
REPLY, SECTION, DEFAULTS = 0x410130e0, 0x40d712c8, 0x41016480
GROUP_LOADED = 0x41016aac
VALID, BLOB = 1, 0xa03dabdc0
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
        cls.image, _ = build(stock, cls.builtin.runtime(), '03.92', 'u1')
        cls.warm, cls.cool, cls.swap = (entry.id for entry in cls.builtin.looks)
        cls.mono = look(cube('Mono', lambda r, g, b: (g, g, g)))
        cls.card_library = lib.Library((cls.mono, look(WARM, '暖色 "Warm"'), look(IDENTITY)))
        cls.code, cls.symbols, cls.metadata = compile_code()

    # ---- machine -------------------------------------------------------
    def machine(self, cards, *, banks=None, bank=0, library=0, power_paused=False, fail_read=(), restored=True, blob_loaded=True):
        """banks: the stored record's banks A..D (None Off, id, 'unresolved'); library: 0 built-in,
        1 card; restored: run S1."""
        banks = banks or [self.warm]*4
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
        for at in (0x402da000, 0x402db000, 0x403ae000, 0x403b0000, 0x403b1000, 0x40d2c000, 0x40d2d000, 0x40d71000,
                   0x41012000, 0x41013000, 0x41016000, 0x65000000, 0xa03bc6000, 0xa03bdd000, 0xa03d48000, 0xa03d49000, 0xa03f31000):
            m.mem_map(at, 4096)
        for at in (*FS.values(), ROW_DISABLE, SAVE, REPLY, SECTION, DEFAULTS, GROUP_LOADED, 0x403ae0f8, 0x403b0048, 0x403b00b8):
            m.mem_write(at, bytes.fromhex('c0035fd6'))
        m.mem_map(STATE, 0x2000)
        m.mem_map(0xa03000000, 0x20000)
        for copy in range(5):
            m.mem_write(OWNED+copy*0x2e0, b'\xff\xff')
        m.mem_write(BANK_INDEX, struct.pack('<I', bank))
        self.scheduler = NativeScheduler(m, (GUI_STACK, WORKER_STACK, 0xa0301a000, 0xa0301c800, 0xa0301f000))
        for at in (0x40d2bd04, 0x40d2bd64, 0x40d2bcc8):
            m.mem_write(at, bytes.fromhex('c0035fd6'))
        m.mem_map(0xa11010000, 0x1000000)        # heap continuation for card buffers
        m.mem_write(FLUSH, bytes.fromhex('c0035fd6'))
        self.saves, self.disabled, self.fs_calls, self.files, self.cursor = [], [], [], {}, []
        self.reset_on_read = False
        self.reset_pending = False
        self.settings_waits, self.settings_clears = [], []
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
                if self.reset_on_read:
                    self.reset_on_read = False
                    self.reset_pending = True
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
            elif at in (0x40d2bd04, 0x40d2bd64):
                event = struct.unpack('<i', uc.mem_read(x(0), 4))[0]
                self.settings_waits.append(('timed' if at == 0x40d2bd64 else 'stock', self.scheduler.round))
                if at == 0x40d2bd64:
                    self.scheduler.current['wake'] = self.scheduler.round+x(3)
                    self.scheduler._block('sleep', 0)
                else:
                    self.scheduler.current['event'] = (event, x(1))
                    self.scheduler._block('wait')
            elif at == 0x40d2bcc8:
                self.settings_clears.append(x(1))
            elif at == GROUP_LOADED:
                uc.reg_write(regs.UC_ARM64_REG_X0, int(blob_loaded))
            elif at == SAVE:
                self.saves.append(self.scheduler.round)
            elif at == ROW_DISABLE:
                self.disabled.append((x(1), x(2)))
            elif at in (SET_CURSOR, SET_MARK):
                self.cursor.append(('cursor' if at == SET_CURSOR else 'mark', x(1)-0x35b5))
        for first, end in ((0x402da000, 0x402dbfff), (0x403b0000, 0x403b1fff), (0x41012000, 0x41012fff), (GROUP_LOADED, GROUP_LOADED), (0x40d2bcc8, 0x40d2bd64)):
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
        # The native blob-16 read found this stored record (staged until S1).
        persist = STATE+layout['persist_offset']
        m.mem_write(persist, record_bytes(banks, library))
        m.mem_write(persist+layout['persist_blob_offset'], struct.pack('<I', VALID))
        if restored:
            self.restore()
        return m

    def restore(self, rounds=3000):
        """S1 in the settings task: decide the record, start the card worker; run until it idles."""
        task = self.scheduler.task('Settings', 1, 30, self.symbols['z8_photo_lut_settings_restored'], (0x4c01,))
        self.scheduler.run(lambda: task['finished'], tick=200000, rounds=100)
        self.settle(rounds)

    def pump_reset(self):
        if self.reset_pending:
            self.reset_pending = False
            self.scheduler.task('Reset', 1, 10, self.symbols['z8_photo_lut_defaults'])

    def settle(self, rounds=3000):
        """Let the worker run until it waits with nothing due (restore not reading)."""
        self.scheduler.run(lambda: (self.pump_reset(), self.u32('restore') != 1 and not any(
            self.scheduler.runnable(t) for t in self.scheduler.tasks))[1], tick=200000, rounds=rounds)

    def wake_worker(self):
        worker = self.card()['worker']
        event = struct.unpack('<i', self.m.mem_read(worker+64, 4))[0]
        self.scheduler.events[event] |= 1
        self.settle()

    def u32(self, name):
        layout = self.metadata['runtime_layout']
        return struct.unpack('<I', self.m.mem_read(STATE+layout['persist_offset']+layout[f'persist_{name}_offset'], 4))[0]

    def record(self):
        return bytes(self.m.mem_read(STATE+self.metadata['runtime_layout']['persist_offset'], 0xc0))

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
        self.scheduler.run(lambda: (self.pump_reset(), task['finished'])[1], tick=200000, rounds=6000)
        return struct.unpack('<I', self.m.mem_read(self.context+0xc, 4))[0]

    # ---- observations --------------------------------------------------
    def card(self):
        at = STATE+self.metadata['runtime_layout']['card_offset']
        fields = struct.unpack('<Q9I4xQ7I4x', self.m.mem_read(at, 88))
        return dict(zip(('active', 'readers', 'publishing', 'request', 'result', 'status', 'detail', 'loaded',
                         'retry_now', 'unload', 'worker', 'worker_id', 'worker_state', 'event_ready', 'show_result',
                         'attempts', 'publications', 'refusals'), fields))

    def choice(self):
        return self.call('choice')

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
    def card_file(self, library=None):
        return {'A:\\Z8LUTLIB.BIN': (library or self.card_library).encode()}

    def published(self):
        state = self.card()
        return bytes(self.m.mem_read(state['active'], 64+128*8)) if state['active'] else None

    def test_boot_text_says_settings_are_restoring_in_every_language(self):
        self.machine({}, restored=False)
        for language, status in [(0, 'Restoring LUT settings...'), (8, '正在恢复 LUT 设置...'),
                                 (9, '正在恢復 LUT 設定...'), (35, 'Restoring LUT settings...')]:
            with self.subTest(language=language):
                self.assert_menu(['Warm', 'Cool', 'Swap'], status, language)
                self.assertIn(self.strings(language)[2], ('Off', '关闭', '關閉'))
                self.assertTrue(any(word in self.strings(language)[1] for word in ('automatic', '自动', '自動')))
        # Before the record is decided nothing is applied or admitted.
        self.assertEqual(self.choice(), 0)

    def test_restart_restores_the_card_library_for_the_selected_banks_look(self):
        mono = self.mono.id
        self.machine(self.card_file(), banks=[mono, None, mono, 'unresolved'], library=1)
        state = self.card()
        self.assertNotEqual(state['active'], 0)
        self.assertEqual((state['status'], state['detail'], state['loaded'], state['publications']), (1, 1, 3, 1))
        self.assertEqual(self.choice(), 1)                           # Mono is the card library's look 1
        self.assertEqual(self.record(), record_bytes([mono, None, mono, 'unresolved'], 1))
        self.assertEqual(self.saves, [])                             # a restore stores nothing
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.cursor[-1], ('mark', 2))               # look 1 -> row 2
        self.assert_menu(['Mono', '暖色 "Warm"', 'Identity'], 'Card LUTs: 3 loaded (slot 1)')
        self.assert_menu(['Mono', '暖色 "Warm"', 'Identity'], '存储卡 LUT：已载入 3 个（卡槽 1）', 8)

    def test_a_card_configuration_restores_its_library_while_every_bank_is_off(self):
        self.machine(self.card_file(), banks=[None]*4, library=1)
        self.assertNotEqual(self.card()['active'], 0)
        self.assertEqual(self.choice(), 0)
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.cursor[-1], ('mark', 0))
        self.assert_menu(['Mono', '暖色 "Warm"', 'Identity'], 'Card LUTs: 3 loaded (slot 1)')

    def test_a_built_in_configuration_never_reads_or_adopts_a_card_file(self):
        self.machine(self.card_file(), banks=[self.cool, self.mono.id, None, None])
        self.assertEqual((self.fs_calls, self.card()['active'], self.choice()), ([], 0, 2))
        self.assertEqual((self.card()['worker'], self.card()['worker_state']), (0, 0))
        # Bank B names a look the built-in library lacks: unavailable at once,
        # never Off or another look, and still no card read.
        self.m.mem_write(BANK_INDEX, struct.pack('<I', 1))
        self.call('z8_photo_lut_menu_entry', self.page); self.settle()
        self.assertEqual((self.fs_calls, self.choice()), ([], 0))
        self.assertEqual(self.cursor[-1], ('mark', 1))
        self.assertEqual(self.strings(0)[3], 'Load LUTs from card')

    def test_mixed_banks_keep_their_card_configuration_across_a_restart(self):
        # A chose built-in Cool before the card library was loaded; B chose card Mono after.
        self.machine(self.card_file(), banks=[self.cool, self.mono.id, None, None], library=1)
        self.assertNotEqual(self.card()['active'], 0)
        self.assertEqual(self.choice(), 0)                           # A: Cool is not in the card library
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.strings(0)[3], 'Selected LUT not on card; choose again')
        self.m.mem_write(BANK_INDEX, struct.pack('<I', 1))
        self.assertEqual(self.choice(), 1)                           # B: Mono applied, no menu visit

    def test_an_explicit_load_switches_the_configuration_to_the_card_library(self):
        self.machine(self.card_file(), banks=[self.cool, None, None, None])
        self.select(1)
        self.assertEqual(self.record(), record_bytes([self.cool, None, None, None], 1))
        self.assertEqual(len(self.saves), 1)
        self.assertEqual(self.choice(), 0)                           # Cool replaced: unavailable, not retargeted
        # A whole reset returns to the built-in library and its default.
        self.call('z8_photo_lut_defaults'); self.settle()
        self.assertEqual(self.card()['active'], 0)
        self.assertEqual(self.record(), record_bytes([self.warm]*4, 0))
        self.assertEqual(self.choice(), 1)

    def test_a_new_load_after_reset_restores_the_new_card_library(self):
        self.machine(self.card_file(), banks=[self.cool]*4)
        self.call('z8_photo_lut_defaults')             # unload queued; worker has not run yet
        self.select(1)
        self.assertNotEqual(self.card()['active'], 0)
        self.assertEqual(self.record(), record_bytes([self.warm]*4, 1))

    def test_reset_cancels_a_queued_old_load_but_accepts_a_new_request(self):
        self.machine(self.card_file(), banks=[self.cool]*4, library=1)
        reads = list(self.fs_calls)
        card = STATE+self.metadata['runtime_layout']['card_offset']
        self.m.mem_write(card+16, struct.pack('<I', 1))       # queued, not started
        self.call('z8_photo_lut_defaults')
        self.settle()
        self.assertEqual((self.fs_calls, self.card()['active'], self.card()['result']), (reads, 0, 1))
        self.assertEqual(self.record(), record_bytes([self.warm]*4, 0))
        self.select(1)
        self.assertEqual(self.card()['result'], 2)
        self.assertNotEqual(self.card()['active'], 0)
        self.assertEqual(self.record(), record_bytes([self.warm]*4, 1))

    def test_reset_cutoff_allows_new_requests_after_serial_wrap(self):
        self.machine(self.card_file(), banks=[self.cool]*4)
        card = STATE+self.metadata['runtime_layout']['card_offset']
        self.m.mem_write(card+16, struct.pack('<2I', 0xffffffff, 0xffffffff))
        self.call('z8_photo_lut_defaults')
        self.select(1)                                 # wraps past reserved serial zero to one
        self.assertEqual(self.card()['result'], 1)
        self.assertNotEqual(self.card()['active'], 0)
        self.select(1)
        self.assertEqual(self.card()['result'], 2)
        self.assertEqual(self.record(), record_bytes([self.warm]*4, 1))
        # After accepting a newer request, the old reset cutoff is inactive;
        # even much later generations cannot be rejected by its old age.
        self.m.mem_write(card+16, struct.pack('<2I', 0xfffffffd, 0xfffffffd))
        self.select(1)
        self.assertEqual(self.card()['result'], 0xfffffffe)
        self.assertNotEqual(self.card()['active'], 0)

    def test_reset_during_a_read_discards_its_staged_library_and_import_intent(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                self.machine(self.card_file(), banks=[self.mono.id]*4,
                             library=int(not explicit), restored=explicit)
                self.reset_on_read = True
                if explicit:
                    self.select(1)
                else:
                    self.restore()
                self.assertFalse(self.reset_on_read)
                self.assertEqual(self.card()['active'], 0)
                self.assertEqual(self.record(), record_bytes([self.warm]*4, 0))
                self.assertEqual(self.choice(), 1)

    def test_missing_card_keeps_the_look_unavailable_and_a_menu_visit_retries(self):
        cards = {}
        self.machine(cards, banks=[self.mono.id]*4, library=1)
        self.assertEqual((self.card()['active'], self.choice()), (0, 0))
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.strings(0)[3], 'No Z8LUTLIB.BIN on either card')
        self.assertEqual(self.cursor[-1], ('mark', 1))               # the card row explains it
        self.assertEqual(self.record(), record_bytes([self.mono.id]*4, 1))
        cards.update(self.card_file())
        self.call('z8_photo_lut_menu_entry', self.page); self.settle()
        self.assertEqual(self.choice(), 1)
        self.assertEqual(self.saves, [])

    def test_missing_card_recovers_automatically_without_a_menu_visit(self):
        cards = {}
        self.machine(cards, banks=[self.mono.id]*4, library=1)
        self.assertEqual(self.choice(), 0)
        cards.update(self.card_file())
        for _ in range(4):
            self.wake_worker()                         # native wait returns; retry schedule advances
        self.assertEqual(self.choice(), 1)
        self.assertEqual(self.saves, [])

    def test_an_unused_missing_bank_does_not_reread_an_available_library(self):
        self.machine(self.card_file(), banks=[self.mono.id, self.cool, None, None], library=1)
        reads, retries = list(self.fs_calls), self.u32('retries')
        for _ in range(40):
            self.wake_worker()                         # twenty seconds of bounded idle waits
        self.assertEqual(self.fs_calls, reads)
        self.assertEqual(self.u32('retries'), retries)
        self.assertEqual(self.choice(), 1)

    def test_a_changed_card_without_the_look_never_retargets(self):
        other = lib.Library((look(SWAP, 'Mono'), look(COOL)))       # same name, different transform
        self.machine(self.card_file(other), banks=[self.mono.id]*4, library=1)
        self.assertNotEqual(self.card()['active'], 0)
        self.assertEqual(self.choice(), 0)
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.strings(0)[3], 'Selected LUT not on card; choose again')
        # Renamed and reordered, the same transform is found by identity.
        moved = lib.Library((look(COOL), look(cube('Mono', lambda r, g, b: (g, g, g)), 'Grey')))
        self.machine(self.card_file(moved), banks=[self.mono.id]*4, library=1)
        self.assertEqual(self.choice(), 2)

    def test_an_unread_record_asks_the_native_save_to_read_it(self):
        self.machine({}, banks=[self.warm]*4, restored=False)
        layout = self.metadata['runtime_layout']
        self.m.mem_write(STATE+layout['persist_offset']+layout['persist_blob_offset'], struct.pack('<I', 5))   # unread
        self.restore()
        self.assertEqual((self.u32('ready'), self.u32('authoritative')), (1, 0))
        self.assertGreaterEqual(len(self.saves), 1)
        # Idle scheduling can cross the first backoff; requests remain bounded.
        self.assertLessEqual(len(self.saves), 2)
        self.assertEqual(self.choice(), 0)                           # unresolved: nothing admitted with a look

    def test_initial_card_worker_failures_recover_in_native_settings_loop_without_menu(self):
        for failure in ('alloc', 'event', 'create'):
            with self.subTest(failure=failure):
                self.machine(self.card_file(), banks=[self.mono.id]*4, library=1, restored=False)
                self.scheduler.fail[failure] = 2             # S1 and first native-loop retry fail
                self.restore()
                self.assertEqual(self.card()['worker_state'], 0)
                # Run the actual patched settings event-loop instructions.
                self.m.mem_write(0x41012918, self.image[0x41012918-BASE:0x410129d4-BASE])
                self.m.mem_write(0xa03bddccc, struct.pack('<i', 901))
                self.scheduler.events[901] = 0
                task = self.scheduler.task('NativeSettings', 1, 30, 0x41012918)
                saved = self.m.context_save(); self.m.context_restore(task['context'])
                for reg, value in ((19, 0xa03bddccc), (20, 0x1733), (21, 0xa03bddcc9),
                                   (22, 0xa03bddd18), (23, 0xa03bddd10)):
                    self.m.reg_write(getattr(regs, f'UC_ARM64_REG_X{reg}'), value)
                task['context'] = self.m.context_save(); self.m.context_restore(saved)
                self.scheduler.run(lambda: self.card()['active'] != 0 and task['state'] == 'wait',
                                   tick=200000, rounds=3000)
                self.assertEqual(self.choice(), 1)
                self.assertEqual(self.settings_clears, [0])    # timeout performs no native save/event work
                self.assertEqual([kind for kind, _ in self.settings_waits], ['timed', 'stock'])
                self.assertGreaterEqual(self.settings_waits[1][1]-self.settings_waits[0][1], 500)
                self.assertEqual(self.saves, [])

    def test_unread_recovery_waits_for_native_prefix_load_and_power(self):
        for controls in ({'blob_loaded': False}, {'power_paused': True}):
            with self.subTest(controls=controls):
                self.machine({}, restored=False, **controls)
                layout = self.metadata['runtime_layout']
                self.m.mem_write(STATE+layout['persist_offset']+layout['persist_blob_offset'], struct.pack('<I', 5))
                self.restore()
                self.assertEqual((self.u32('ready'), self.u32('authoritative')), (1, 0))
                self.assertEqual(self.saves, [])

    def test_unrecognized_banks_ask_for_a_choice(self):
        self.machine(self.card_file(), banks=['unresolved']*4)
        self.assertEqual((self.fs_calls, self.choice()), ([], 0))
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.strings(0)[3], 'LUT setting not recognized; choose again')
        self.assertEqual(self.strings(8)[3], 'LUT 设置无法识别，请重新选择')

    def test_choosing_stores_the_full_identity_for_the_selected_bank(self):
        self.machine({}, banks=[self.warm, None, self.swap, None], bank=2)
        self.select(3)                                               # row 3: built-in look 2, Cool
        self.assertEqual(self.record(), record_bytes([self.warm, None, self.cool, None]))
        self.assertEqual(len(self.saves), 1)
        self.select(0)                                               # Off
        self.assertEqual(self.record(), record_bytes([self.warm, None, None, None]))
        self.assertEqual(self.choice(), 0)
        # An unused row keeps the selection and stores nothing.
        self.assertEqual(self.select(6), 0x17)
        self.assertEqual(len(self.saves), 2)

    def test_a_choice_before_the_record_is_read_is_kept_but_not_stored(self):
        self.machine({}, banks=[self.warm]*4, restored=False)
        self.select(0)
        self.assertEqual(self.saves, [])
        self.assertEqual(self.choice(), 0)
        self.assertEqual(self.u32('pending'), 1)
        self.restore()
        self.assertEqual(self.record(), record_bytes([None, self.warm, self.warm, self.warm]))
        self.assertEqual(len(self.saves), 1)

    def test_identical_cards_load_and_report_both_slots(self):
        raw = lib.Library((look(SWAP),)).encode()
        self.machine({'A:\\Z8LUTLIB.BIN': raw, 'B:\\Z8LUTLIB.BIN': raw}, banks=[None, self.warm, None, None])
        self.select(1)
        self.assertEqual(self.card()['status'], 1)
        self.assert_menu(['Swap'], 'Card LUTs: 1 loaded (slot 1+2)')
        self.assertEqual(self.record(), record_bytes([None, self.warm, None, None], 1))   # no bank changes
        # Bank B's Warm is not in the loaded library: unavailable, never Off or Swap.
        self.m.mem_write(BANK_INDEX, struct.pack('<I', 1))
        self.assertEqual(self.choice(), 0)
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.strings(0)[3], 'Selected LUT not on card; choose again')

    def refused(self, cards, status, text, **options):
        self.machine(cards, **options)
        self.select(1)
        state = self.card()
        self.assertEqual((state['active'], state['publications'], state['status']), (0, 0, status))
        self.assertEqual(self.record(), record_bytes([self.warm]*4))
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

    def test_truncated_reserved_missing_and_unreadable_files_are_refused(self):
        good = lib.Library((look(SWAP),)).encode()
        reserved = bytearray(good); reserved[28] = 5
        cases = [({'A:\\Z8LUTLIB.BIN': good[:2000]}, 3, 'Slot 1 Z8LUTLIB.BIN invalid; unchanged', {}),
                 ({'A:\\Z8LUTLIB.BIN': bytes(reserved)}, 3, 'Slot 1 Z8LUTLIB.BIN invalid; unchanged', {}),
                 ({}, 2, 'No Z8LUTLIB.BIN on either card', {}),
                 ({'B:\\Z8LUTLIB.BIN': good}, 5, 'Slot 2 read error; unchanged', dict(fail_read=('B:\\Z8LUTLIB.BIN',)))]
        for cards, status, text, options in cases:
            with self.subTest(text=text, cards=sorted(cards)):
                self.refused(cards, status, text, **options)

    def test_empty_card_library_clears_looks_and_disables_rows(self):
        self.machine({'A:\\Z8LUTLIB.BIN': lib.Library(()).encode()}, banks=[None]*4)
        self.select(1)
        self.assert_menu([], 'Card LUTs: 0 loaded (slot 1)')
        self.assertEqual(self.record(), record_bytes([None]*4, 1))
        self.call('z8_photo_lut_menu_entry', self.page)
        self.assertEqual(self.disabled[-8:], [(look+1, 1) for look in range(1, 9)])   # rows after Off and Load
        # Selecting a disabled look row changes nothing and keeps the stock target.
        self.assertEqual(self.select(2), 0x17)
        self.assertEqual(self.record(), record_bytes([None]*4, 1))

    def test_power_down_touches_no_card_and_publishes_nothing(self):
        self.machine(self.card_file(), banks=[self.mono.id]*4, library=1, power_paused=True)
        self.select(1)
        self.assertEqual(self.fs_calls, [])
        self.assertEqual((self.card()['active'], self.saves), (0, []))
        self.assert_menu(['Warm', 'Cool', 'Swap'], 'Card busy; try again')

    def test_patched_image_keeps_the_identity_and_the_stock_bank_defaults(self):
        # The requested version.tag reaches the runtime identity and the trailer byte-for-byte.
        self.assertEqual(self.image[0x14eaa90:0x14eaa98], b'03.92.u1')
        self.assertEqual(self.image[-11:-3], b'03.92.u1')
        self.assertEqual(self.image[0x3ece4e2:0x3ece4e4], bytes(2))                 # group6 defaults untouched
        self.assertEqual(struct.unpack_from('<I', self.image, LUT-BASE+28)[0], 0)   # reserved field


if __name__ == '__main__':
    unittest.main()
