"""Execute the bank-record lifecycle on the original C3.11 settings code.

Opt in with Z8_PHOTO_LUT_ROM=/path/to/z8-main-payload.bi. The patched image
runs the native mode-4 settings callback (0x410129d4), the group-0x31 part of
the save producer (0x41012538..0x41012560), the section-16 read/write API
(0x40d6c7d8/0x40d6c628) with its real region selector and writer, and our
hooks at their patched call sites. Isolated: OS locks/critical sections, task
and event services, the heap (a bump allocator), and the physical NOR erase
and program primitives (0x40ca6de8/0x40ca6c70), which are modelled as NOR
(erase to 0xff, program only clears bits). Section-15 restore itself is not
run; its completion is signalled through our S1 hook with the native
restore-ok byte set. No physical flash timing or power behaviour is implied.
"""
import binascii
import hashlib
import os
from pathlib import Path
import struct
import unittest

from features import build
from photo_lut_fixtures import LIBRARY_CARD, PRIVATE_BYTES, STATE, native_code, record_bytes
from z8patch.shared import TABLES, TABLES_END
from z8photolut import library as lib
from z8photolut.patcher import ALIAS, BASE, STATE as STATE_PAGE, TEXT
from test_photo_lut_library import COOL, SWAP, WARM, look

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc = None

ROM_SHA256 = '2b43a31c9de838f84b12416de4348a7004e65be07f853ec56ec2b10f3a8e3853'
DATA_BIAS = 0x9bdb95000
STOP = 0x60000000
STACK = 0x500f0000
HEAP = 0xa20000000
PRIMARY, MIRROR, REGION = 0x45e60000, 0x46240000, 0x20000
BLOB, STOCK_BYTES, RECORD_BYTES = 0xa03dabdc0, 0xcdb0, 0xc0
BUFFER = 0xa11010000
OWNED, OWNED_STRIDE = 0xa03d482ba, 0x2e0
BANK_INDEX, RESTORED_OK, LOADED = 0xa03d48164, 0xa03bddcc8, 0xa03d476d0+0x31
SAVE, REPLY, SLEEP, ALLOC, FREE = 0x41012bf4, 0x410130e0, 0x40d2d654, 0x40d2c408, 0x40d2c448
ERASE, PROGRAM = 0x40ca6de8, 0x40ca6c70
PASS = (0x411850a0, 0x40d2c674, 0x40d2c660, 0x40d2bc8c, 0x40d2bc28, 0x40d2d498, 0x40d2d5cc, 0x40d2bdac, 0x40183490, 0x41014524, 0x410143b8, 0x410145bc, 0x40d71254)
SOURCES = ('none', 'stored', 'fresh', 'migrated', 'lost', 'defaults', 'retained', 'unread')
BLOBS = ('pending', 'valid', 'absent', 'corrupt', 'lost', 'unread', 'skipped')


@unittest.skipIf(Uc is None, 'Unicorn is required for the native settings lifecycle')
class BankRecordLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied = os.environ.get('Z8_PHOTO_LUT_ROM')
        if not supplied or not Path(supplied).is_file():
            raise unittest.SkipTest('Set Z8_PHOTO_LUT_ROM to the original C3.11 z8-main-payload.bi')
        rom = Path(supplied).read_bytes()
        if hashlib.sha256(rom).hexdigest() != ROM_SHA256:
            raise AssertionError('C3.11 payload SHA256 differs')
        cls.rom = rom
        image, details = build(rom+binascii.crc_hqx(rom, 0).to_bytes(2, 'big'), '03.92', 'u1', ['photo-lut'])
        cls.image, cls.details = image[:-2], details
        _, cls.symbols, native = native_code()
        cls.layout = native['runtime_layout']
        # Looks of a card library; the firmware default of every bank is Off.
        cls.ids = [entry.id for entry in lib.Library((look(WARM), look(COOL), look(SWAP))).looks]
        cls.chosen = record_bytes([cls.ids[1]]*4, LIBRARY_CARD)
        cls.off = record_bytes([None]*4)
        # A non-default stock blob 16, as stock firmware would have stored it.
        cls.prefix = bytes((i*37+11) & 255 for i in range(STOCK_BYTES))

    # ---- machine -------------------------------------------------------
    def machine(self, nor=None, *, owned=(0,)*5, bank=0, restored_ok=1, state=None, loaded=0):
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        m.mem_map(BASE, (len(self.image)+4095) & ~4095)
        m.mem_write(BASE, self.image)
        m.mem_map(0xa02000000, 0x2000000)
        source = 0x44c6b000
        m.mem_write(source+DATA_BIAS, self.image[source-BASE:])
        m.mem_map(0xa04800000, 0x100000)
        m.mem_map(STATE, PRIVATE_BYTES)
        # Menu text and its language-table entries are rewritten through the writable RAM alias.
        for first, end in ((TABLES, TABLES_END), (TEXT, STATE_PAGE)):
            m.mem_map(first+ALIAS, end-first)
            m.mem_write(first+ALIAS, self.image[first-BASE:end-BASE])
        m.mem_map(0x50000000, 0x100000)
        m.mem_map(STOP, 0x1000)
        m.mem_map(BUFFER, 0x20000)
        m.mem_map(HEAP, 0x400000)
        for base in (PRIMARY, MIRROR):
            m.mem_map(base, REGION)
            m.mem_write(base, (nor or {}).get(base, b'\xff'*REGION))
        for copy, value in enumerate(owned):
            m.mem_write(OWNED+copy*OWNED_STRIDE, struct.pack('<H', value))
        m.mem_write(BANK_INDEX, struct.pack('<I', bank))
        m.mem_write(RESTORED_OK, bytes([restored_ok]))
        m.mem_write(LOADED, bytes([loaded]))
        if state:
            m.mem_write(STATE, state)
        self.m, self.saves, self.ops, self.heap = m, [], [], HEAP
        self.alloc_failures, self.fail_program = 0, False
        self.copy_events, self.wait_events, self.cleared = [], [], []
        self.wait_bits, self.wait_status = 0, -1
        self.pause_wait = False
        self.section15_attempts, self.other_events = [], []
        self.allow_unread_shutdown = False
        m.hook_add(UC_HOOK_CODE, self.native)
        return m

    def native(self, u, pc, size, data):
        x = lambda n: u.reg_read(getattr(regs, f'UC_ARM64_REG_X{n}'))
        value = 0
        if 0x410dda64 <= pc < 0x410de050:
            instruction = int.from_bytes(u.mem_read(pc, 4), 'little')
            if instruction >> 26 == 37:
                offset = instruction & 0x3ffffff
                if offset & (1 << 25): offset -= 1 << 26
                target = pc+offset*4
                if target not in (0x41013a14, self.symbols['z8_photo_lut_bank_copy']):
                    if pc == 0x410ddbfc: self.copy_events.append('name-save')
                    value = int.from_bytes(u.mem_read(BANK_INDEX, 4), 'little') if pc == 0x410ddae4 else int(target == 0x410db194)
                    u.reg_write(regs.UC_ARM64_REG_X0, value)
                    u.reg_write(regs.UC_ARM64_REG_PC, pc+4)
                    return
        if pc in (0x4017bb8c, 0x4017c040):
            if self.pause_wait:
                u.emu_stop(); return
            self.wait_events.append((pc, x(0), x(1), x(2), x(4) if pc == 0x4017c040 else None))
            if self.wait_status == 0: u.mem_write(x(3), struct.pack('<I', self.wait_bits))
            value = self.wait_status & ((1 << 64)-1)
        elif pc == 0x40d6cafc:
            status = self.status()
            self.section15_attempts.append(status)
            if not self.allow_unread_shutdown:
                self.assertEqual(status[2:], (1, 1), 'record must be decided before section15 serialization')
            value = 1
        elif pc == 0x41017c38:
            self.other_events.append(pc)
        elif pc == 0x40d2bcc8:
            self.cleared.append(x(1)); value = 1
        elif pc in PASS or pc in (REPLY, SLEEP, FREE):
            pass
        elif pc == SAVE:
            self.saves.append(pc)
            self.copy_events.append('save-request')
        elif pc == ALLOC:
            if self.alloc_failures:
                self.alloc_failures -= 1
            else:
                value, self.heap = self.heap, self.heap+((x(0)+63) & ~63)
        elif pc == 0x4127a900:
            u.mem_write(x(0), bytes(u.mem_read(x(1), x(2))))
            value = x(0)
        elif pc == ERASE:
            address, count = 0x40000000+(x(0) & 0xffffffff)*REGION, (x(1) & 0xffffffff)*REGION
            self.assertIn(address, (PRIMARY, MIRROR)); self.assertEqual(count, REGION)
            self.ops.append(('erase', address)); u.mem_write(address, b'\xff'*REGION)
        elif pc == PROGRAM:
            address, count = x(0), (x(2) & 0xffffffff)*2
            raw = bytes(u.mem_read(x(1), count))
            self.ops.append(('program', address, count))
            if self.fail_program:
                value = 1
            else:
                before = bytes(u.mem_read(address, count))
                self.assertTrue(all((a & b) == b for a, b in zip(before, raw)), 'NOR program may only clear bits')
                u.mem_write(address, raw)
        else:
            return
        u.reg_write(regs.UC_ARM64_REG_X0, value)
        u.reg_write(regs.UC_ARM64_REG_PC, x(30))

    def call(self, address, *arguments, stop=STOP, stack_fill=None, stack=STACK):
        if stack_fill is not None:
            self.m.mem_write(STACK-0x8000, bytes([stack_fill])*0x8000)
        self.m.reg_write(regs.UC_ARM64_REG_SP, stack)
        self.m.reg_write(regs.UC_ARM64_REG_X30, stop)
        for index, value in enumerate(arguments):
            self.m.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
        hook = self.m.hook_add(UC_HOOK_CODE, lambda u, pc, size, data: u.emu_stop() if pc == stop else None)
        try: self.m.emu_start(address, stop, count=50_000_000)
        finally: self.m.hook_del(hook)
        self.assertEqual(self.m.reg_read(regs.UC_ARM64_REG_PC), stop)
        return self.m.reg_read(regs.UC_ARM64_REG_X0)

    # ---- native steps --------------------------------------------------
    def boot(self, **fill):
        self.call(self.symbols['z8_photo_lut_boot'], **fill)

    def settings_restored(self, **fill):
        # S1 at 0x4101248c: the native section-15 restore has finished.
        self.call(0x4101248c, 0x4c01, 0, 1, 0, 0xbe, stop=0x41012490, **fill)

    def native_mode4(self, **fill):
        self.call(0x410129d4, 4, **fill)

    def restore_tail(self, **fill):
        event, bits = BUFFER+0x19000, BUFFER+0x19010
        self.m.mem_write(event, struct.pack('<i', 27))
        self.call(self.symbols['z8_photo_lut_settings_wait'], event, 0x1733, bits, **fill)

    def prepare_private(self, order='s1-first', **fill):
        for step in ([self.settings_restored, self.native_mode4] if order == 's1-first'
                     else [self.native_mode4, self.settings_restored]):
            step(**fill)
        self.boot(**fill)
        self.saves.clear()       # baseline native wake is separately checked by late-bootstrap tests

    def store(self, **fill):
        # The group-0x31 store of the native save producer, through our call site.
        self.call(0x41012538, stop=0x41012560, **fill)
        return self.m.reg_read(regs.UC_ARM64_REG_X0)

    def stock_write(self, prefix):
        # What unpatched firmware stores: the stock bytes only.
        self.m.mem_write(BUFFER, prefix)
        return self.call(0x40d6c628, 16, BUFFER, STOCK_BYTES)

    def stored(self):
        """Read the committed region with the native reader: stock bytes, record bytes."""
        self.m.mem_write(BUFFER, b'\xcc'*(STOCK_BYTES+RECORD_BYTES))
        self.assertEqual(self.call(0x40d6c7d8, 16, BUFFER, STOCK_BYTES+RECORD_BYTES) & 1, 1)
        raw = bytes(self.m.mem_read(BUFFER, STOCK_BYTES+RECORD_BYTES))
        return raw[:STOCK_BYTES], raw[STOCK_BYTES:]

    def nor(self):
        return {base: bytes(self.m.mem_read(base, REGION)) for base in (PRIMARY, MIRROR)}

    def u32(self, name):
        return struct.unpack('<I', self.m.mem_read(STATE+self.layout['persist_offset']+self.layout[f'persist_{name}_offset'], 4))[0]

    def record(self):
        return bytes(self.m.mem_read(STATE+self.layout['persist_offset'], RECORD_BYTES))

    def status(self):
        return SOURCES[self.u32('source')], BLOBS[self.u32('blob')], self.u32('ready'), self.u32('authoritative')

    def owned(self):
        return [struct.unpack('<H', self.m.mem_read(OWNED+c*OWNED_STRIDE, 2))[0] for c in range(5)]

    def reset_bank(self, kind):
        saved = BUFFER+0x18000
        registers = [0]*32; registers[0] = 1; registers[19] = kind
        self.m.mem_write(saved, struct.pack('<32Q', *registers))
        self.call(self.symbols['z8_photo_lut_bank_reset_result'], saved)

    def copy_bank(self, source, destination):
        return self.call(0x410dda64, source, destination)

    def choose_off(self):
        request, page, listing, rows = (BUFFER+n for n in (0, 0x100, 0x200, 0x300))
        self.m.mem_write(request, struct.pack('<Q', page))
        self.m.mem_write(page+0x50, struct.pack('<Q', listing))
        self.m.mem_write(listing+0x10, struct.pack('<Q', rows))
        self.m.mem_write(rows+24, struct.pack('<I', 0x35b5))
        self.call(self.symbols['z8_photo_lut_menu_ok'], request)

    def first_boot(self, *, order='s1-first', fill=None):
        """Stock-written region (non-default prefix, no record) seen by this firmware."""
        self.machine()
        self.stock_write(self.prefix)
        self.prepare_private(order, **({} if fill is None else dict(stack_fill=fill)))
        self.restore_tail(**({} if fill is None else dict(stack_fill=fill)))

    def configured(self):
        """First boot, then every bank chose Cool from a card library and the record was stored."""
        self.first_boot(); self.store()
        self.m.mem_write(STATE+self.layout['persist_offset'], self.chosen)
        self.assertEqual(self.store() & 1, 1)

    def reboot(self, nor, *, order='s1-first', **options):
        self.machine(nor, **options)
        self.prepare_private(order)
        self.restore_tail()

    def native_dispatch(self, bits, *, start=0x41012918, stop=0x410129c4):
        self.m.mem_write(BUFFER+0x19000, struct.pack('<i', 27))
        self.wait_bits, self.wait_status = bits, 0
        for reg, value in ((19, BUFFER+0x19000), (20, 0x1733), (21, 0xa03bddcc9),
                           (22, 0xa03bddd18), (23, 0xa03bddd10)):
            self.m.reg_write(getattr(regs, f'UC_ARM64_REG_X{reg}'), value)
        if start == stop:
            visited = []
            def completed(u, pc, size, data):
                if pc == start:
                    if visited:
                        u.reg_write(regs.UC_ARM64_REG_PC, STOP); u.emu_stop()
                    visited.append(pc)
            hook = self.m.hook_add(UC_HOOK_CODE, completed)
            try: self.call(start)
            finally: self.m.hook_del(hook)
        else: self.call(start, stop=stop)

    # ---- cases ---------------------------------------------------------
    def test_late_bootstrap_reads_before_native_save_and_preserves_live_prefix(self):
        self.configured()
        self.machine(self.nor(), owned=(0xffff,)*5)
        self.settings_restored(); self.native_mode4(); self.boot()
        live = bytes((i*13+71) & 255 for i in range(STOCK_BYTES))
        self.m.mem_write(BLOB, live)             # native changes after its prefix read
        self.ops.clear()
        self.native_dispatch(0x1000)
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(len(self.section15_attempts), 1)
        self.assertEqual(self.stored(), (live, self.chosen))

    def test_native_defaults_before_late_boot_discard_an_older_valid_tail(self):
        self.first_boot(); self.store()
        self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes([self.ids[0]]*4, 1)); self.store()
        self.machine(self.nor(), owned=(0,)*5, restored_ok=0)
        self.call(0x41016a60, stop=0x41016a64)
        self.settings_restored(); self.native_mode4(); self.boot()
        self.restore_tail()
        self.assertEqual(self.status(), ('defaults', 'valid', 1, 1))
        self.assertEqual(self.record(), self.off)

    def test_wait_blocked_before_private_boot_initializes_in_its_continuation(self):
        self.first_boot(); self.store()
        self.machine(self.nor(), owned=(0xffff,)*5)
        self.settings_restored()
        event, bits = BUFFER+0x19000, BUFFER+0x19010
        self.m.mem_write(event, struct.pack('<i', 27))
        self.pause_wait = True
        self.m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        self.m.reg_write(regs.UC_ARM64_REG_X30, STOP)
        for index, arg in enumerate((event, 0x1733, bits)):
            self.m.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), arg)
        self.m.emu_start(self.symbols['z8_photo_lut_settings_wait'], STOP, count=1000000)
        self.assertEqual(self.m.reg_read(regs.UC_ARM64_REG_PC), 0x4017bb8c)
        waiting = self.m.context_save()
        self.native_mode4(stack=STACK-0x10000); self.boot(stack=STACK-0x10000)
        self.m.context_restore(waiting); self.pause_wait = False
        self.wait_bits, self.wait_status = 0x1000, 0
        self.m.emu_start(self.m.reg_read(regs.UC_ARM64_REG_PC), STOP, count=50000000)
        self.assertEqual(self.m.reg_read(regs.UC_ARM64_REG_PC), STOP)
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.m.mem_read(bits, 4), struct.pack('<I', 0x1000))
        self.assertEqual(self.ops, [])

    def test_early_save_defers_only_save_and_cannot_erase_a_private_tail(self):
        self.configured()
        nor = self.nor()
        self.machine(nor, owned=(0xffff,)*5)
        self.settings_restored(); self.native_mode4()
        live = bytes(reversed(self.prefix)); self.m.mem_write(BLOB, live)
        # Direct original producer continues after guarded blob16 failure.
        self.call(0x41012538, stop=0x41012560)
        self.assertEqual(self.nor(), nor)
        self.native_dispatch(0x1100, stop=0x41012918)
        self.assertEqual(self.other_events, [0x41017c38])
        self.assertEqual(self.section15_attempts, [])
        self.assertEqual(self.nor(), nor)
        self.boot(); self.native_dispatch(0x1000)
        self.assertEqual(self.stored(), (live, self.chosen))

    def test_preboot_native_no_save_completion_is_acknowledged_without_serialization(self):
        self.machine()
        self.m.mem_write(0xa03bddcc9, bytes([1]))
        self.m.mem_write(0xa03bddd18, struct.pack('<Q', 1))
        self.m.mem_write(0xa03bddd10, bytes([1]))
        self.native_dispatch(0x1000, stop=0x410129d0)
        self.assertEqual(self.m.mem_read(0xa03bddd10, 1), bytes(1))
        self.assertEqual(self.section15_attempts, [])
        self.assertEqual(self.ops, [])

    def test_oom_defers_and_replays_save_once_with_other_native_events_intact(self):
        self.configured()
        self.machine(self.nor(), owned=(0xffff,)*5)
        self.prepare_private()
        self.alloc_failures = 10000
        self.native_dispatch(0x1100, stop=0x41012918)
        self.assertEqual(self.other_events, [0x41017c38])
        self.assertEqual(self.section15_attempts, [])
        self.assertEqual(self.ops, [])
        self.m.mem_write(0xa03bddcc9, bytes([1]))
        self.m.mem_write(0xa03bddd18, struct.pack('<Q', 1))
        self.m.mem_write(0xa03bddd10, bytes([1]))
        self.native_dispatch(0x1000, stop=0x410129d0)
        self.assertEqual(self.m.mem_read(0xa03bddd10, 1), bytes(1))
        self.assertEqual(self.section15_attempts, [])
        self.choose_off()
        self.alloc_failures = 0
        # Even after tail recovery, no-save acknowledgments cannot consume
        # the held real save demand or spin on repeated immediate replays.
        self.m.mem_write(0xa03bddd10, bytes([1]))
        self.native_dispatch(0x1000, stop=0x410129d0)
        self.assertEqual(self.section15_attempts, [])
        self.assertEqual(self.wait_events[-1][0], 0x4017c040)
        self.m.mem_write(0xa03bddcc9, bytes([0]))
        self.native_dispatch(0)                  # held SAVE replay, no new incoming event
        self.assertEqual(len(self.section15_attempts), 1)
        self.assertEqual(self.stored()[1], record_bytes([None]+[self.ids[1]]*3, LIBRARY_CARD))
        self.wait_bits = 0
        self.restore_tail()
        self.assertEqual(len(self.section15_attempts), 1)

    def test_persistent_oom_power_down_saves_stock_and_preserves_previous_private_commit(self):
        self.configured()
        previous = self.nor()
        self.machine(previous, owned=(0xffff,)*5)
        self.prepare_private(); self.choose_off()
        self.alloc_failures = 10000
        self.m.mem_write(STATE+self.layout['preview_offset']+self.layout['power_paused_offset'], struct.pack('<I', 1))
        self.allow_unread_shutdown = True
        self.native_dispatch(0x1000)
        self.assertEqual(len(self.section15_attempts), 1)
        self.assertEqual(self.nor(), previous)   # latest unsavable Off is not claimed persisted
        self.assertEqual(self.ops, [])
        self.assertEqual(self.wait_events[-1][0], 0x4017bb8c)  # native shutdown wait, no periodic poll

    def test_delayed_native_loaded_flag_recovers_without_another_menu_or_event(self):
        self.first_boot(); self.store()
        self.machine(self.nor(), owned=(0xffff,)*5)
        self.settings_restored(); self.boot()
        self.restore_tail()
        self.assertEqual(self.u32('blob_reads'), 0)
        self.assertEqual(self.wait_events[-1][0], 0x4017c040)
        self.native_mode4(); self.restore_tail()
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))

    def test_warm_preparation_quarantines_old_handles_and_retains_or_resets_intent(self):
        self.first_boot(); self.store()
        self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes([None]*4, 1))
        prior = bytes(self.m.mem_read(STATE, 0x1000)); nor = self.nor()
        for reset in (False, True):
            with self.subTest(reset=reset):
                self.machine(nor, owned=(0xffff,)*5, state=prior, loaded=1, restored_ok=0)
                # Poison old handles: any accidental wake would hit unmapped memory.
                preview = STATE+self.layout['preview_offset']
                self.m.mem_write(preview+128, struct.pack('<Q', 0xdead00000))
                self.m.mem_write(preview+140, struct.pack('<I', 1))
                card = STATE+self.layout['card_offset']
                self.m.mem_write(card+48, struct.pack('<Q', 0xbeef00000))
                self.m.mem_write(card+60, struct.pack('<I', 1))
                if reset: self.call(0x41016a60, stop=0x41016a64)
                self.native_mode4()             # native-only even before S1 marker
                self.settings_restored()
                self.assertEqual(self.saves, [])
                self.assertEqual(self.heap, HEAP)
                self.boot(); self.restore_tail()
                self.assertEqual(self.record(), self.off if reset else record_bytes([None]*4, 1))

    def test_first_upgrade_in_either_order_stores_the_default_after_the_stock_bytes(self):
        for order in ('s1-first', 'mode4-first'):
            with self.subTest(order=order):
                self.first_boot(order=order)
                expected = self.off
                self.assertEqual(self.status(), ('fresh', 'absent', 1, 1))
                self.assertEqual(self.record(), expected)
                self.assertEqual(self.owned(), [0xffff]*5)
                self.assertEqual(self.saves, [SAVE])
                self.assertEqual(bytes(self.m.mem_read(BLOB, STOCK_BYTES)), self.prefix)
                self.assertEqual(self.store() & 1, 1)
                self.assertEqual(self.stored(), (self.prefix, expected))
                # The stock reader still gets exactly the stock bytes.
                self.m.mem_write(BUFFER, b'\xcc'*(STOCK_BYTES+RECORD_BYTES))
                self.call(0x40d6c7d8, 16, BUFFER, STOCK_BYTES)
                self.assertEqual(bytes(self.m.mem_read(BUFFER, STOCK_BYTES+RECORD_BYTES)), self.prefix+b'\xcc'*RECORD_BYTES)

    def test_cold_boot_reads_the_stored_record_in_either_order(self):
        self.configured()
        nor = self.nor()
        for order in ('mode4-first', 's1-first'):
            with self.subTest(order=order):
                self.reboot(nor, order=order, owned=(0xffff,)*5)
                self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
                self.assertEqual(self.record(), self.chosen)
                self.assertEqual(self.saves, [])

    def test_cold_boot_reads_the_stored_record_and_writes_nothing(self):
        self.configured()
        nor = self.nor()
        self.reboot(nor, owned=(0xffff,)*5)
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.record(), self.chosen)
        self.assertEqual(self.saves, [])
        self.assertEqual(bytes(self.m.mem_read(BLOB, STOCK_BYTES)), self.prefix)
        # An unchanged region is not programmed again.
        self.ops.clear()
        self.store()
        self.assertEqual(self.ops, [])

    def test_changes_before_a_late_read_are_merged_onto_the_stored_record(self):
        self.first_boot(); self.store()
        stored = [self.ids[0], self.ids[2], None, self.ids[1]]
        self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes(stored))
        self.assertEqual(self.store() & 1, 1)
        nor = self.nor()
        self.machine(nor, owned=(0xffff,)*5, bank=0)
        self.prepare_private()
        # The stored region is not read yet: bank B is reset natively; nothing
        # is known about A, C or D and nothing may be stored.
        self.reset_bank(11)
        self.assertEqual(self.status(), ('none', 'pending', 0, 0))
        self.assertEqual(self.saves, [])
        self.restore_tail(stack_fill=0xa5)
        merged = list(stored); merged[1] = None                 # reset to the default, Off
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.record(), record_bytes(merged))
        self.assertEqual(self.saves, [SAVE])
        self.store()
        self.assertEqual(self.stored(), (self.prefix, record_bytes(merged)))

    def test_a_record_not_read_for_lack_of_memory_stores_nothing_until_a_store_reads_it(self):
        self.configured()
        nor = self.nor()
        # Every allocation for the read fails: the stock bytes load, the record is not read.
        self.machine(nor, owned=(0xffff,)*5)
        self.prepare_private()
        self.alloc_failures = 10_000
        self.restore_tail()
        self.alloc_failures = 0
        self.assertEqual(self.status(), ('none', 'unread', 0, 0))
        self.assertEqual(bytes(self.m.mem_read(BLOB, STOCK_BYTES)), self.prefix)
        self.assertEqual(self.record(), bytes(RECORD_BYTES))
        self.assertEqual(self.saves, [])
        # A later store first reads the region; it succeeds now and merges.
        self.reset_bank(12)
        self.store()
        merged = [self.ids[1], self.ids[1], None, self.ids[1]]
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.stored(), (self.prefix, record_bytes(merged, LIBRARY_CARD)))
        # If that read fails too, the store is refused and the region kept.
        self.machine(self.nor(), owned=(0xffff,)*5)
        self.prepare_private()
        self.alloc_failures = 10_000
        self.restore_tail()
        before = self.nor()
        self.assertEqual(self.store() & 1, 0)
        self.assertEqual(self.nor(), before)

    def test_off_during_unread_restore_is_merged_on_actual_recovery(self):
        self.configured()
        self.machine(self.nor(), owned=(0xffff,)*5, bank=2)
        self.prepare_private()
        self.alloc_failures = 10_000
        self.restore_tail()
        self.choose_off()
        self.assertEqual(self.saves, [])
        self.alloc_failures = 0
        self.assertEqual(self.store() & 1, 1)
        expected = [self.ids[1], self.ids[1], None, self.ids[1]]
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.stored(), (self.prefix, record_bytes(expected, LIBRARY_CARD)))

    def test_current_bank_reset_uses_native_selected_bank_and_rejects_invalid_index(self):
        for bank in (1, 2, 3, 4, 0xffffffff):
            with self.subTest(bank=bank):
                self.first_boot(); self.store()
                before = [self.ids[0], None, self.ids[2], self.ids[0]]
                self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes(before))
                self.m.mem_write(BANK_INDEX, struct.pack('<I', bank))
                self.saves.clear()
                self.reset_bank(9)
                expected = list(before)
                if bank < 4:
                    expected[bank] = None
                self.assertEqual(self.record(), record_bytes(expected))
                self.assertEqual(self.saves, [SAVE] if bank < 4 else [])

    def test_native_explicit_copy_preserves_identity_for_active_and_inactive_banks(self):
        for selected in (0, 1, 2):
            with self.subTest(selected=selected):
                self.first_boot(); self.store()
                banks = [self.ids[0], self.ids[2], None, self.ids[1]]
                self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes(banks))
                self.m.mem_write(BANK_INDEX, struct.pack('<I', selected))
                self.saves.clear(); self.copy_events.clear()
                self.assertEqual(self.copy_bank(1, 2), 1)      # actual B -> C producer and item transfer
                banks[2] = banks[1]
                self.assertEqual(self.record(), record_bytes(banks))
                self.assertLess(self.copy_events.index('save-request'), self.copy_events.index('name-save'))
                self.store()
                self.reboot(self.nor(), owned=(0xffff,)*5, bank=selected)
                self.assertEqual(self.record(), record_bytes(banks))
                self.assertEqual(self.status(), ('stored', 'valid', 1, 1))

    def test_explicit_copy_noop_invalid_and_native_failure_keep_private_slots(self):
        self.first_boot(); self.store()
        before = self.record()
        for source, destination in ((1, 1), (4, 1), (1, 4), (0xffffffff, 0)):
            with self.subTest(source=source, destination=destination):
                self.saves.clear()
                self.assertEqual(self.copy_bank(source, destination), 0)
                self.assertEqual(self.record(), before)
                self.assertEqual(self.saves, [])
        # A genuinely invalid first item makes the original per-item resolver
        # fail and the original explicit producer return failure.
        self.m.mem_write(0xa0299e888, struct.pack('<I', 0x7c00))
        self.saves.clear()
        self.assertEqual(self.copy_bank(1, 2), 0)
        self.assertEqual(self.record(), before)
        self.assertEqual(self.saves, [])

    def test_unread_copy_chains_snapshot_original_sources_before_later_edits(self):
        self.first_boot(); self.store()
        original = [self.ids[0], self.ids[2], None, self.ids[1]]
        self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes(original)); self.store()
        nor = self.nor()
        for unread in (False, True):
            with self.subTest(unread=unread):
                self.machine(nor, owned=(0xffff,)*5)
                self.prepare_private()
                if unread:
                    self.alloc_failures = 10000; self.restore_tail(); self.alloc_failures = 0
                self.saves.clear()
                self.assertEqual(self.copy_bank(0, 1), 1)     # B snapshots original A, identity not read yet
                self.choose_off()                           # A now Off, B must retain the old original A
                self.assertEqual(self.copy_bank(1, 2), 1)     # C snapshots B's same original A
                self.m.mem_write(BANK_INDEX, struct.pack('<I', 1)); self.choose_off()
                self.assertEqual(self.copy_bank(1, 3), 1)     # known pending Off copied to D
                self.reset_bank(11)                          # later B default must not retarget D
                self.assertEqual(self.saves, [])
                if unread: self.store()                      # actual read recovers and merges deferred copies
                else: self.restore_tail()
                expected = [None, None, original[0], None]
                self.assertEqual(self.record(), record_bytes(expected))
                self.store()
                self.reboot(self.nor(), owned=(0xffff,)*5)
                self.assertEqual(self.record(), record_bytes(expected))

    def test_settings_wait_uses_exact_native_mode_one_and_preserves_real_events(self):
        self.first_boot()
        persist = STATE+self.layout['persist_offset']
        self.m.mem_write(persist, record_bytes([self.ids[1]]*4, 1))
        event, bits = BUFFER+0x18000, BUFFER+0x18010
        self.m.mem_write(event, struct.pack('<i', 27))
        self.m.mem_write(bits, struct.pack('<I', 0xdeadbeef))
        self.call(self.symbols['z8_photo_lut_settings_wait'], event, 0x1733, bits)
        self.assertEqual(self.wait_events[-1], (0x4017c040, 27, 0x1733, 1, 500))
        self.assertEqual(self.m.mem_read(bits, 4), bytes(4))    # timeout kernel deliberately leaves it alone
        card = STATE+self.layout['card_offset']
        self.m.mem_write(card+60, struct.pack('<I', 2))        # another task is still creating the worker
        self.call(self.symbols['z8_photo_lut_settings_wait'], event, 0x1733, bits)
        self.assertEqual(self.wait_events[-1][0], 0x4017c040)  # keep bounded recovery, avoid an infinite wait
        self.m.mem_write(card+60, struct.pack('<I', 0))
        self.wait_bits, self.wait_status = 0x1000, 0
        self.assertEqual(self.call(self.symbols['z8_photo_lut_settings_wait'], event, 0x1733, bits), 1)
        self.assertEqual(self.m.mem_read(bits, 4), struct.pack('<I', 0x1000))
        # The patched original native loop consumes the real bits with its
        # unchanged clear-mask instruction before dispatch.
        self.m.reg_write(regs.UC_ARM64_REG_X19, event)
        self.m.reg_write(regs.UC_ARM64_REG_X20, 0x1733)
        self.call(0x41012918, stop=0x41012938)
        self.assertEqual(self.cleared[-1], 0x1000)

    def test_settings_wait_stays_native_without_a_card_library_and_power_down(self):
        for paused, library in ((False, 0), (True, 1)):
            with self.subTest(paused=paused):
                self.first_boot()
                self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes([self.ids[1]]*4, library))
                self.m.mem_write(STATE+self.layout['preview_offset']+self.layout['power_paused_offset'], struct.pack('<I', int(paused)))
                self.m.mem_write(BUFFER, struct.pack('<i', 28))
                self.wait_bits, self.wait_status = 0x20, 0
                self.wait_events.clear()
                self.call(self.symbols['z8_photo_lut_settings_wait'], BUFFER, 0x1733, BUFFER+8)
                self.assertEqual(self.wait_events, [(0x4017bb8c, 28, 0x1733, 1, None)])
                self.assertEqual(self.m.mem_read(BUFFER+8, 4), struct.pack('<I', 0x20))

    def test_virgin_regions_take_the_native_blob_defaults_and_are_initialized(self):
        defaults = self.rom[0x4406b5d0-BASE:0x4406b5d0-BASE+STOCK_BYTES]   # 0x410165c4's source
        for owned, source, expected, stores in [((0,)*5, 'fresh', [None]*4, [SAVE]),
                                                ((0xffff,)*5, 'lost', ['unresolved']*4, [])]:
            with self.subTest(source=source):
                self.machine(owned=owned)                 # both vol16 regions erased: no valid region
                self.prepare_private(); self.restore_tail(stack_fill=0x5a)
                self.assertEqual(self.status(), (source, 'lost', 1, 1))
                self.assertEqual(self.record(), record_bytes(expected))
                self.assertEqual(self.saves, stores)
                self.assertEqual(bytes(self.m.mem_read(BLOB, STOCK_BYTES)), defaults)
                self.assertEqual(self.m.mem_read(LOADED, 1), b'\x01')      # native marks blob 16 loaded
                # The native store then initializes blob 16 with the record.
                self.assertEqual(self.store() & 1, 1)
                self.assertEqual(self.stored(), (defaults, record_bytes(expected)))

    def test_records_built_on_a_dirty_stack_are_canonical(self):
        for owned, expected, source in [((0,)*5, [None]*4, 'fresh'),
                                        # B selected: its latest pair is copy 0; A, C, D are copies 1, 3, 4.
                                        ((0x0102, 0, 0x0300, 0, 0x5a01), [None, 'unresolved', None, 'unresolved'], 'migrated')]:
            with self.subTest(source=source):
                self.machine(owned=owned, bank=1)
                self.stock_write(self.prefix)
                self.prepare_private(stack_fill=0xa5); self.restore_tail(stack_fill=0xff)
                self.assertEqual(self.status()[0], source)
                self.assertEqual(self.record(), record_bytes(expected))
                self.store(stack_fill=0xa5)
                nor = self.nor()
                self.reboot(nor, owned=(0xffff,)*5, bank=1)
                self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
                self.assertEqual(self.record(), record_bytes(expected))

    def test_migration_never_names_a_look_and_preserves_tagged_off(self):
        # Copy 0 is selected B's latest pair. Its low byte is Off even when
        # a prior card session left a nonzero high-byte library tag.
        for tagged_off in (0x0200, 0xff00):
            with self.subTest(tagged_off=hex(tagged_off)):
                self.machine(owned=(tagged_off, 0x0101, 0x0000, 0x0000, 0x0302), bank=1)
                self.stock_write(self.prefix)
                self.prepare_private(); self.restore_tail()
                self.assertEqual(self.status(), ('migrated', 'absent', 1, 1))
                self.assertEqual(self.record(), record_bytes(['unresolved', None, None, 'unresolved']))

    def test_stock_round_trip_erases_the_record_and_reads_as_lost(self):
        self.first_boot(); self.store()
        self.stock_write(bytes(reversed(self.prefix)))          # unpatched firmware stores blob 16
        nor = self.nor()
        self.reboot(nor, owned=(0xffff,)*5)
        self.assertEqual(self.status(), ('lost', 'absent', 1, 1))
        self.assertEqual(self.record(), record_bytes(['unresolved']*4))
        self.assertEqual(self.saves, [])
        self.assertEqual(bytes(self.m.mem_read(BLOB, STOCK_BYTES)), bytes(reversed(self.prefix)))

    def test_a_corrupt_record_is_unresolved_and_not_rewritten(self):
        self.configured()
        nor = self.nor()
        for base in (PRIMARY, MIRROR):
            if nor[base][STOCK_BYTES:STOCK_BYTES+4] != b'\xff'*4:
                raw = bytearray(nor[base]); raw[STOCK_BYTES+40] &= 0xfe; nor[base] = bytes(raw)
        self.reboot(nor, owned=(0xffff,)*5)
        self.assertEqual(self.status(), ('lost', 'corrupt', 1, 1))
        self.assertEqual(self.record(), record_bytes(['unresolved']*4))
        self.assertEqual(self.saves, [])

    def test_interrupted_store_keeps_the_previous_committed_record(self):
        self.configured()
        first = self.nor()
        self.machine(first, owned=(0xffff,)*5)
        self.prepare_private(); self.restore_tail()
        self.reset_bank(10)
        self.m.mem_write(STATE+self.layout['persist_offset'], record_bytes([self.ids[2]]*4))
        self.fail_program = True                     # body program never completes: no marker
        self.assertEqual(self.store() & 1, 0)
        self.fail_program = False
        self.reboot(self.nor(), owned=(0xffff,)*5)
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.record(), self.chosen)

    def test_warm_start_with_blob_loaded_keeps_the_retained_record(self):
        self.configured()
        retained = bytes(self.m.mem_read(STATE, 0x1000))
        nor = self.nor()
        self.machine(nor, owned=(0xffff,)*5, state=retained, loaded=1)
        self.prepare_private(); self.restore_tail()
        self.assertEqual(self.status(), ('retained', 'skipped', 1, 1))
        self.assertEqual(self.record(), self.chosen)
        # Without a retained record bootstrap reads the actual tail instead.
        self.machine(nor, owned=(0xffff,)*5, loaded=1)
        self.prepare_private(); self.restore_tail()
        self.assertEqual(self.status(), ('stored', 'valid', 1, 1))
        self.assertEqual(self.saves, [])

    def test_native_defaults_reset_every_bank(self):
        self.configured()
        self.reset_bank(12)
        self.saves.clear()
        self.call(self.symbols['z8_photo_lut_defaults'])
        self.assertEqual(self.status(), ('defaults', 'absent', 1, 1))
        self.assertEqual(self.record(), self.off)
        self.assertEqual(self.owned(), [0xffff]*5)
        self.assertEqual(self.saves, [SAVE])


if __name__ == '__main__':
    unittest.main()
