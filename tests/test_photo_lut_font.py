"""Photo LUT label fonts: ROM facts, the glyph-builder seam and the compiled family choice.

Opt in with Z8_PHOTO_LUT_ROM. Font code-point maps are read from the original
ROM font data; the stock resolver 0x4032e19c is modelled by language with the
class-0 menu font of each language (its table is built at run time), so this
checks our choice logic and font data reads, not drawn glyphs.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
import photo_lut_fonts as fonts  # noqa: E402
from z8photolut.library import menu_font_note  # noqa: E402
from z8photolut.patcher import BASE, CODE, MENU_TEXT, compile_code  # noqa: E402

try:
    import capstone
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    capstone = Uc = None

DATA_BIAS = 0x9bdb95000          # proven initialized-data alias (test_photo_lut_native_output)
RESOLVER, STOP, TEXT = 0x4032e19c, 0x65000000, 0xa04000000
FIRST, LABELS = 3137, 12
# Stock class-0 menu font of these UI languages (font-native class0-fonts.json):
# ja 0, English 1, German 2, zh-Hans 8, zh-Hant 9, ko 10, Thai 12, Arabic 34.
CLASS0 = {0: 13, 1: 16, 2: 16, 8: 2, 9: 0, 10: 22, 12: 11, 34: 4}
FALLBACK = (1, 8, 9, 0, 10)


def card_texts():
    """Fixed card-load status texts from firmware.c (\\1 count, \\2 slots)."""
    source = (ROOT/'z8photolut/src/firmware.c').read_text()
    block = source[source.index('static const char *const card_text'):]
    block = block[:block.index('};')]
    rows = re.findall(r'\{("(?:[^"\\]|\\.)*"),("(?:[^"\\]|\\.)*"),("(?:[^"\\]|\\.)*")\}', block)
    # The literals hold UTF-8 text with only the \\1 and \\2 placeholders as escapes.
    return [[x[1:-1].replace('\\1', '8').replace('\\2', '1+2') for x in row] for row in rows]


class FontFactsTests(unittest.TestCase):
    def test_label_guidance_mirrors_the_fallback_fonts(self):
        for name in ['Warm', '暖色', '人像 Portrait', '褪色', 'jazz 暖色', 'Mono "B&W"', 'A & B', '電影']:
            with self.subTest(name=name):
                self.assertEqual(menu_font_note(name), '')
        self.assertIn('fallback menu fonts lack 冬, 季', menu_font_note('冬季 Winter'))
        self.assertIn('rename the look', menu_font_note('冬季'))

    def test_every_fixed_menu_text_is_drawn_by_its_own_language_font(self):
        facts = json.loads((ROOT/'resources/photo-lut-fonts.json').read_text())['fonts']
        for group, font in enumerate((16, 2, 0)):
            texts = [*MENU_TEXT[group], *(row[group] for row in card_texts())]
            self.assertEqual(len(texts), 4+8)
            for text in texts:
                with self.subTest(group=group, text=text):
                    missing = [c for c in text if ord(c) >= 0x20 and
                               not any(a <= ord(c) < b for a, b in facts[str(font)])]
                    self.assertEqual(missing, [])


@unittest.skipIf(Uc is None, 'Unicorn and Capstone are required for native font checks')
class NativeFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied = os.environ.get('Z8_PHOTO_LUT_ROM')
        if not supplied or not Path(supplied).is_file():
            raise unittest.SkipTest('Set Z8_PHOTO_LUT_ROM to the original C3.11 z8-main-payload.bi')
        cls.rom = Path(supplied).read_bytes()
        if hashlib.sha256(cls.rom).hexdigest() != fonts.ROM_SHA256:
            raise AssertionError('C3.11 payload SHA256 differs')
        cls.maps = fonts.derive(cls.rom)
        cls.code, cls.symbols, _ = compile_code()

    def test_generated_font_facts_are_exactly_the_rom(self):
        self.assertEqual((ROOT/'z8photolut/src/fonts.h').read_text(), fonts.header(self.maps))
        self.assertEqual((ROOT/'resources/photo-lut-fonts.json').read_text(), fonts.encode_ranges(fonts.ranges(self.maps)))

    def test_seam_and_resolver_assumptions(self):
        md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        def at(address):
            i = next(md.disasm(self.rom[address-BASE:address-BASE+4], address))
            return f'{i.mnemonic} {i.op_str}'
        contract = {0x4032b72c: 'bl #0x4032e130', 0x4032b730: 'ldr x8, [sp, #8]', 0x4032b734: 'mov w0, w22',
                    0x4032b738: 'ldr w9, [sp, #0x4c]', 0x4032b73c: 'ldr w1, [sp, #0x30]',
                    0x4032b740: 'str x8, [x20, #0x28]', 0x4032b744: 'stp w9, w9, [x20, #0x30]',
                    0x4032b748: 'bl #0x4032e19c', 0x4032b74c: 'ldr w1, [sp, #0x30]',
                    0x4032b750: 'strb w0, [x20, #0x21]', 0x4032b754: 'mov w0, w22', 0x4032b758: 'bl #0x4032e2a0'}
        for address, text in contract.items():
            with self.subTest(address=hex(address)):
                self.assertEqual(at(address), text)
        # Initialized string count (read before ids are used) and class bytes of our ids.
        count = struct.unpack_from('<I', self.rom, 0xa0281cd5c-DATA_BIAS-BASE)[0]
        self.assertEqual(count, 65535)
        self.assertGreaterEqual(count, FIRST+LABELS)
        self.assertEqual(self.rom[fonts.CLASS_MAP-BASE+FIRST:fonts.CLASS_MAP-BASE+FIRST+LABELS], bytes(LABELS))

    def model(self, language, identifier, text):
        """Independent model of the firmware choice over the ROM font maps."""
        font = CLASS0[language]
        if not FIRST <= identifier < FIRST+LABELS:
            return font
        units = struct.unpack(f'<{len(text)//2}H', text)
        def covers(f):
            return all(u < 0x20 or (not 0xd800 <= u < 0xe000 and fonts.covered(self.maps[f][1], u)) for u in units)
        if covers(font):
            return font
        return next((CLASS0[l] for l in FALLBACK if CLASS0[l] != font and covers(CLASS0[l])), font)

    def machine(self):
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        first = 0x42b00000
        m.mem_map(first, 0x300000)                       # ROM font data
        m.mem_write(first, self.rom[first-BASE:first-BASE+0x300000])
        m.mem_map(CODE, 0x10000); m.mem_write(CODE, self.code)
        m.mem_map(RESOLVER & ~0xfff, 0x1000); m.mem_write(RESOLVER, bytes.fromhex('c0035fd6'))
        m.mem_map(STOP, 0x1000); m.mem_map(TEXT, 0x10000); m.mem_map(0x60000000, 0x10000)
        def resolver(uc, address, size, user):
            uc.reg_write(regs.UC_ARM64_REG_X0, CLASS0[uc.reg_read(regs.UC_ARM64_REG_X0) & 0xffffffff])
        m.hook_add(UC_HOOK_CODE, resolver, begin=RESOLVER, end=RESOLVER)
        return m

    def run_wrapper(self, m, language, identifier, text, *, entry=False, pointer=TEXT, length=None):
        m.mem_write(TEXT, text+bytes(2))
        m.reg_write(regs.UC_ARM64_REG_SP, 0x6000f000)
        m.reg_write(regs.UC_ARM64_REG_X30, STOP)
        m.reg_write(regs.UC_ARM64_REG_X0, language)
        m.reg_write(regs.UC_ARM64_REG_X1, identifier)
        length = len(text) if length is None else length
        if entry:   # the glyph builder's live registers at 0x4032b748
            m.reg_write(regs.UC_ARM64_REG_X8, pointer)
            m.reg_write(regs.UC_ARM64_REG_X9, length)
            start = self.symbols['z8_photo_lut_text_font_entry']
        else:
            m.reg_write(regs.UC_ARM64_REG_X2, pointer)
            m.reg_write(regs.UC_ARM64_REG_X3, length)
            start = self.symbols['z8_photo_lut_text_font']
        m.emu_start(start, STOP, count=5_000_000)
        return m.reg_read(regs.UC_ARM64_REG_X0)

    def test_compiled_choice_matches_the_model(self):
        m = self.machine()
        names = ['1. 暖色', '2. 人像 Portrait', '3. 褪色', '4. jazz 暖色', '5. Mono "B&W"', '6. 電影',
                 '7. 冬季 Winter', 'Plain', '8. 한국', 'Off', '-']
        cases = []
        for language in CLASS0:
            for identifier in (0, 0x8a6, FIRST-1, FIRST, FIRST+3, FIRST+11, FIRST+12):
                for name in names:
                    cases.append((language, identifier, name))
        changed = 0
        for language, identifier, name in cases:
            text = name.encode('utf-16le')
            expected = self.model(language, identifier, text)
            changed += expected != CLASS0[language]
            for entry in (False, True):
                with self.subTest(language=language, identifier=identifier, name=name, entry=entry):
                    self.assertEqual(self.run_wrapper(m, language, identifier, text, entry=entry), expected)
        self.assertGreater(changed, 20)
        # Selected anchors (font-native FONT-SEAM.md).
        for language, name, font in [(1, '暖色', 2), (1, '褪色', 0), (1, '電影', 0), (1, '冬季 Winter', 16),
                                     (8, 'Mono "B&W"', 16), (0, '電影', 13), (34, 'Plain', 4)]:
            with self.subTest(anchor=name, language=language):
                self.assertEqual(self.run_wrapper(m, language, FIRST+4, name.encode('utf-16le')), font)

    def test_untrusted_text_spans_keep_the_stock_font(self):
        m = self.machine()
        text = '暖色'.encode('utf-16le')
        self.assertEqual(self.run_wrapper(m, 1, FIRST+4, text), 2)
        for label, options in [('odd pointer', dict(pointer=TEXT+1)), ('null text', dict(pointer=0)),
                               ('oversized', dict(length=514)), ('lone surrogate', dict())]:
            with self.subTest(label=label):
                sample = '\ud800'.encode('utf-16le', 'surrogatepass') if label == 'lone surrogate' else text
                self.assertEqual(self.run_wrapper(m, 1, FIRST+4, sample, **options), 16)


if __name__ == '__main__':
    unittest.main()
