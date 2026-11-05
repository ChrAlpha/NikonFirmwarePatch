#!/usr/bin/env python3
"""Execute the shared photo-menu refresh dispatcher in Unicorn; no firmware or compiler (needs unicorn)."""
from pathlib import Path
import struct
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
import unicorn.arm64_const as regs
from z8patch.compiled import load
from z8patch.shared import BANK, PHOTO_CONFIG

REG = [getattr(regs, f'UC_ARM64_REG_X{i}') for i in range(31)]
BASE_REFRESH, ROW_REFRESH, RETURN = 0x50000000, 0x50000100, 0x50000200
STACK = 0x51008000


class SharedPhotoRefreshTests(unittest.TestCase):
    def setUp(self):
        code, meta = load('shared')
        self.entries = {name: int(value['va'], 16) for name, value in meta['symbols'].items()}
        self.calls = []
        m = self.m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        m.mem_map(BANK, 0x1000)
        m.mem_write(BANK, code)
        m.mem_write(PHOTO_CONFIG, struct.pack('<2Q', BASE_REFRESH, ROW_REFRESH))
        m.mem_map(0x50000000, 0x1000)
        for at in (BASE_REFRESH, ROW_REFRESH, RETURN):
            m.mem_write(at, bytes.fromhex('c0035fd6'))
        m.mem_map(STACK-0x8000, 0x10000)

        def callback(uc, address, size, user):
            # Callees follow the AAPCS64: x0..x18 are theirs to clobber.
            self.calls.append((address, uc.reg_read(REG[0])))
            for i in range(19):
                uc.reg_write(REG[i], 0xdead0000+i)
            uc.reg_write(REG[0], 0x2a if address == BASE_REFRESH else 0x99)
        m.hook_add(UC_HOOK_CODE, callback, begin=BASE_REFRESH, end=BASE_REFRESH)
        m.hook_add(UC_HOOK_CODE, callback, begin=ROW_REFRESH, end=ROW_REFRESH)

    def run_refresh(self, page):
        values = {i: 0x1111_0000_0000+i for i in range(31)}
        values[0], values[30] = page, RETURN
        for i, value in values.items():
            self.m.reg_write(REG[i], value)
        self.m.reg_write(regs.UC_ARM64_REG_SP, STACK)
        self.m.emu_start(self.entries['shared_photo_refresh'], RETURN)
        return values

    def test_base_refresh_then_photo_lut_row_with_the_base_result(self):
        for page in (0xa03002000, 0):
            with self.subTest(page=page):
                self.calls.clear()
                before = self.run_refresh(page)
                self.assertEqual(self.calls, [(BASE_REFRESH, page), (ROW_REFRESH, page)])
                self.assertEqual(self.m.reg_read(REG[0]), 0x2a)
                self.assertEqual(self.m.reg_read(regs.UC_ARM64_REG_SP), STACK)
                for i in range(19, 30):
                    self.assertEqual(self.m.reg_read(REG[i]), before[i], f'x{i}')

    def test_photo_part_follows_the_video_part(self):
        self.assertLessEqual(self.entries['shared_video_end'], self.entries['shared_photo_refresh'])
        self.assertLess(self.entries['shared_photo_refresh'], PHOTO_CONFIG)


if __name__ == '__main__':
    unittest.main(verbosity=2)
